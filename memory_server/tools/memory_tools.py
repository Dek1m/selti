"""MCP tools for memory operations.

All tools delegate to Celery tasks via celery_call().
ACL checks and metadata coercion remain at tool level.
"""

import json
from typing import Any, get_args

from fastmcp import Context
from pydantic import TypeAdapter, ValidationError

from memory_server.config import settings
from memory_server.metrics import (
    SEARCH_RESULTS,
    MEMORY_COUNT,
)
from memory_server.models import LinkType
from memory_server.server import mcp
from memory_server.tools.task_bridge import celery_call
from memory_server.utils.metrics_decorator import tool_handler

# Валидатор link_type без сборки полного RelationCreate
_LINK_TYPE_ADAPTER = TypeAdapter(LinkType)


def _validate_link_type(link_type: str) -> None:
    """Валидация link_type на входе: понятная ошибка вместо PG CHECK violation."""
    try:
        _LINK_TYPE_ADAPTER.validate_python(link_type)
    except ValidationError:
        allowed = ", ".join(get_args(LinkType))
        raise ValueError(
            f"link_type '{link_type}' is not allowed. Allowed: {allowed}"
        ) from None

# Имена задач
TASK_STORE = "memory_server.tasks.memory_tasks.store_memory"
TASK_GET = "memory_server.tasks.memory_tasks.get_memory"
TASK_UPDATE = "memory_server.tasks.memory_tasks.update_memory"
TASK_DELETE = "memory_server.tasks.memory_tasks.delete_memory"
TASK_SEARCH = "memory_server.tasks.memory_tasks.search_memories"
TASK_LIST = "memory_server.tasks.memory_tasks.list_memories"
TASK_RECENT = "memory_server.tasks.memory_tasks.get_recent"
TASK_STATS = "memory_server.tasks.memory_tasks.get_stats"
TASK_NAMESPACES = "memory_server.tasks.memory_tasks.get_namespaces"
TASK_FIND_SIMILAR = "memory_server.tasks.memory_tasks.find_similar"
TASK_GET_RELATIONS = "memory_server.tasks.memory_tasks.get_relations"
TASK_GRAPH_STATS = "memory_server.tasks.memory_tasks.graph_stats"
TASK_TRAVERSE = "memory_server.tasks.memory_tasks.traverse_graph"
TASK_INGEST_BATCH = "memory_server.tasks.memory_tasks.ingest_batch"
TASK_FORGET = "memory_server.tasks.memory_tasks.forget_memories"
TASK_ARCHIVE = "memory_server.tasks.memory_tasks.archive_memory"
TASK_ADD_RELATION = "memory_server.tasks.memory_tasks.add_relation"
TASK_DELETE_RELATION = "memory_server.tasks.memory_tasks.delete_relation"
TASK_SUPERSEDE = "memory_server.tasks.memory_tasks.supersede_memory"
TASK_GET_HISTORY = "memory_server.tasks.memory_tasks.get_memory_history"
TASK_FREEZE = "memory_server.tasks.memory_tasks.freeze_memory"
TASK_STALE_LIST = "memory_server.tasks.memory_tasks.stale_list"
TASK_CLUSTER_LIST = "memory_server.tasks.memory_tasks.cluster_list"


def _coerce_metadata(metadata) -> dict | None:
    """Coerce metadata to dict if it's a JSON string."""
    if metadata is None:
        return None
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return None
    return metadata


# ══════════════════════════════════════════════════════════════════
# Memory tools
# ══════════════════════════════════════════════════════════════════


@mcp.tool()
@tool_handler("memory_store")
async def memory_store(
    content: str,
    user_id: str,
    metadata: str | dict | None = None,
    namespace: str | None = None,
    importance: int | None = None,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Store a new memory record.

    Generates an embedding for the content and persists it to the database.
    Deduplication is applied automatically — returns existing record if a match is found.

    project_id: optional project slug (e.g. 'akame') or UUID; binds the granule
    to the project registry. Omit for global (cross-project) knowledge.
    """
    metadata = _coerce_metadata(metadata)
    return await celery_call(
        TASK_STORE,
        content=content,
        user_id=user_id,
        metadata=metadata,
        namespace=namespace,
        importance=importance,
        project_id=project_id,
    )


@mcp.tool()
@tool_handler("memory_search")
async def memory_search(
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
    namespace: str | None = None,
    project_id: str | None = None,
    include_historical: bool = False,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Search memories by semantic similarity (hybrid: dense + full-text).

    Returns memories matching the query, ordered by relevance score
    (rrf × recency_decay × importance).
    Only currently asserted memories are returned (status='asserted').

    project_id: optional project slug or UUID to scope the search;
    omit to search everywhere (global layer included).
    include_historical: set True for time-travel — superseded/retracted
    versions are included (validity filter disabled).
    """
    results = await celery_call(
        TASK_SEARCH,
        query=query,
        user_id=user_id,
        limit=limit,
        threshold=threshold,
        namespace=namespace,
        project_id=project_id,
        include_historical=include_historical,
    )
    SEARCH_RESULTS.labels(tool="memory_search").observe(len(results))
    return results


@mcp.tool()
@tool_handler("memory_ingest_batch")
async def memory_ingest_batch(
    entries: list[dict],
    user_id: str,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Store multiple memory records in batch.

    Entries format: [{content, metadata?, namespace?}, ...]
    project_id binds the whole batch to one project (slug or UUID).
    Returns summary of inserted/skipped/updated counts.
    """
    return await celery_call(
        TASK_INGEST_BATCH,
        entries=entries,
        user_id=user_id,
        project_id=project_id,
    )


@mcp.tool()
@tool_handler("memory_stats")
async def memory_stats(
    user_id: str | None = None,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """Get memory statistics for a user — per-namespace counts and last updated.

    project_id: optional project slug or UUID to scope stats to one project.
    """
    result = await celery_call(TASK_STATS, user_id=user_id, project_id=project_id)
    for item in result:
        MEMORY_COUNT.labels(namespace=item["namespace"]).set(item["count"])
    return result


@mcp.tool()
@tool_handler("memory_find_similar")
async def memory_find_similar(
    content: str,
    user_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
    namespace: str | None = None,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """Find semantically similar memories without storing."""
    results = await celery_call(
        TASK_FIND_SIMILAR,
        content=content,
        user_id=user_id,
        limit=limit,
        threshold=threshold,
        namespace=namespace,
        project_id=project_id,
    )
    SEARCH_RESULTS.labels(tool="memory_find_similar").observe(len(results))
    return results


@mcp.tool()
@tool_handler("memory_get")
async def memory_get(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Retrieve a single memory record by its ID."""
    return await celery_call(TASK_GET, memory_id=id)


@mcp.tool()
@tool_handler("memory_update")
async def memory_update(
    id: str,
    content: str | None = None,
    metadata: str | dict | None = None,
    importance: int | None = None,
    project_id: str | None = None,
    supersedes: str | None = None,
    clear_project_id: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Update an existing memory record — обвязка факта (V3.0, ADR-019).

    content: правка факта НЕ на месте — создаёт новую версию гранулы
    (внутренне supersede с reason='edit'): ответ содержит id НОВОЙ версии
    и поле versioned=true, старая остаётся в истории (memory_get_history).
    Изменить факт «на месте» невозможно в принципе. Metadata is merged
    (existing keys are kept, new ones overwrite matching keys).

    project_id: optional project slug or UUID to (re)bind the granule.
    clear_project_id: true — отвязать гранулу от проекта (NULL = глобальный слой, D2).
    supersedes: optional ID of a previous version this granule replaces —
    the old granule is closed (status='superseded', valid window ends now).
    """
    metadata = _coerce_metadata(metadata)
    return await celery_call(
        TASK_UPDATE,
        clear_project_id=clear_project_id,
        memory_id=id,
        content=content,
        metadata=metadata,
        importance=importance,
        project_id=project_id,
        supersedes=supersedes,
    )


@mcp.tool()
@tool_handler("memory_delete")
async def memory_delete(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete a memory record by its ID."""
    return await celery_call(TASK_DELETE, memory_id=id)


@mcp.tool()
@tool_handler("memory_list")
async def memory_list(
    user_id: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
    offset: int = 0,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List memory records with optional filtering and pagination."""
    return await celery_call(
        TASK_LIST,
        user_id=user_id,
        namespace=namespace,
        limit=limit,
        offset=offset,
        project_id=project_id,
    )


@mcp.tool()
@tool_handler("memory_recent")
async def memory_recent(
    namespace: str | None = None,
    limit: int = 20,
    since: str | None = None,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Get the most recent memory records.

    Returns records ordered by creation time (newest first).
    Useful for checking what happened recently — "what did we do today", "last 10 records", etc.
    Pass 'since' as an ISO datetime string (e.g. '2026-07-25' or '2026-07-25T10:00:00')
    to filter records created after a specific point in time.
    project_id: optional project slug or UUID to scope the records.
    """
    return await celery_call(
        TASK_RECENT,
        namespace=namespace,
        since=since,
        limit=limit,
        project_id=project_id,
    )


@mcp.tool()
@tool_handler("memory_forget")
async def memory_forget(
    user_id: str,
    namespace: str | None = None,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Retract all memories for a user (status='retracted'), optionally filtered.

    namespace/project_id narrow the scope: e.g. forget a user's knowledge
    of one project (project_id: slug or UUID) without touching the global layer.
    """
    return await celery_call(
        TASK_FORGET,
        user_id=user_id,
        namespace=namespace,
        project_id=project_id,
    )


@mcp.tool()
@tool_handler("memory_archive")
async def memory_archive(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Retract a memory record (soft delete).

    Sets status='retracted' and closes its validity window (valid_to=now()).
    The record is excluded from search, list, and recent queries but remains
    in the database (and Qdrant, marked retracted) for potential restoration.
    """
    return await celery_call(TASK_ARCHIVE, memory_id=id)


# ── Graph tools ──


@mcp.tool()
@tool_handler("memory_link")
async def memory_link(
    source_id: str,
    target_id: str,
    link_type: str = "related_to",
    description: str | None = None,
    weight: float = 1.0,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Создать связь между двумя гранулами.

    link_type: depends_on | used_by | extends | implements | contains | contained_by |
               calls | called_by | related_to | contradicts | solves | tested_by |
               implements_adr | references | follows | precedes | alternative_to |
               causes | prevents | runs_on | exposes | mounts | derived_from |
               motivates | informs | informed_by | connected_to | supersedes |
               supports | member_of | part_of | describes_cluster
    project_id: опционально — валидация проекта (slug/UUID) ранней понятной ошибкой.
    """
    _validate_link_type(link_type)
    return await celery_call(
        TASK_ADD_RELATION,
        source_id=source_id,
        target_id=target_id,
        link_type=link_type,
        description=description,
        weight=weight,
        project_id=project_id,
    )


@mcp.tool()
@tool_handler("memory_unlink")
async def memory_unlink(
    source_id: str,
    target_id: str,
    link_type: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Удалить связь между двумя гранулами."""
    return await celery_call(
        TASK_DELETE_RELATION,
        source_id=source_id,
        target_id=target_id,
        link_type=link_type,
    )


@mcp.tool()
@tool_handler("memory_get_relations")
async def memory_get_relations(
    source_id: str,
    link_type: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Получить входящие и исходящие связи гранулы.

    Возвращает {incoming: [...], outgoing: [...]}
    """
    return await celery_call(
        TASK_GET_RELATIONS,
        source_id=source_id,
        link_type=link_type,
    )


@mcp.tool()
@tool_handler("memory_traverse")
async def memory_traverse(
    start_id: str,
    depth: int = 3,
    link_types: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Обход графа от начальной гранулы (BFS).

    depth: максимальная глубина обхода (по умолчанию 3)
    link_types: фильтр по типам связей (по умолчанию все)
    limit/offset: курсорная пагинация узлов (стабильный порядок —
    сортировка по id; total_nodes в ответе — для навигации);
    hard-cap узлов — 500 (конфиг traverse_max_nodes)
    project_id: опционально — валидация проекта (slug/UUID) ранней понятной
    ошибкой; граф связей глобальный, фильтра узлов по проекту нет
    """
    return await celery_call(
        TASK_TRAVERSE,
        start_id=start_id,
        depth=depth,
        link_types=link_types,
        limit=limit,
        offset=offset,
        project_id=project_id,
    )


@mcp.tool()
@tool_handler("memory_graph_stats")
async def memory_graph_stats(
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Статистика графа знаний: связность, сироты, кластеры по namespace и типам связей."""
    return await celery_call(TASK_GRAPH_STATS)


# ── Version (локальный, без Celery) ──


@mcp.tool()
@tool_handler("memory_version")
async def memory_version(
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Версия selti сервера."""
    from pathlib import Path

    version_file = Path(__file__).parent.parent.parent / "VERSION"
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    return {
        "version": version,
        "server": settings.mcp_server_name,
        "model": settings.embedding_model,
    }


# ── Namespaces ──


@mcp.tool()
@tool_handler("memory_namespaces")
async def memory_namespaces(
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Получить список всех namespace из реестра.

    Возвращает uid, name и description каждого namespace.
    Используй для динамического определения допустимых namespace.
    """
    return await celery_call(TASK_NAMESPACES)


# ── Lifecycle tools (Фаза 2 плана редизайна: судьба гранулы) ──


@mcp.tool()
@tool_handler("memory_supersede")
async def memory_supersede(
    granule_id: str,
    content: str,
    metadata: str | dict | None = None,
    importance: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Создать новую версию гранулы — ЕДИНСТВЕННЫЙ путь изменения факта
    (V3.0, ADR-019: контент неизменяем, update правит только обвязку).

    Старая закрывается по правилу Graphiti: status='superseded',
    valid_to = valid_from новой (окно старой заканчивается моментом
    появления новой). Новая наследует user/namespace/project_id/metadata
    (dict-merge), version = старая+1, confidence = старая ×0.9 (cap 0..1),
    frozen=false, cluster_id и рёбра графа (REWIRE). Используй для
    ФАКТОВ-КОНФЛИКТОВ (утверждение заменило опровергнутое) и любых правок
    контента; для metadata/importance/проекта — memory_update.

    granule_id: ID замещаемой гранулы (должна быть asserted).
    """
    metadata = _coerce_metadata(metadata)
    return await celery_call(
        TASK_SUPERSEDE,
        granule_id=granule_id,
        content=content,
        metadata=metadata,
        importance=importance,
    )


@mcp.tool()
@tool_handler("memory_get_history")
async def memory_get_history(
    granule_id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Supersession-цепочка гранулы (рекурсивный обход supersedes/superseded_by).

    Возвращает {items: [от старейшей к новейшей версии], current_id} —
    current_id помечает актуальную версию (status='asserted');
    None — если вся цепочка закрыта (superseded/retracted).
    """
    return await celery_call(TASK_GET_HISTORY, granule_id=granule_id)


@mcp.tool()
@tool_handler("memory_freeze")
async def memory_freeze(
    granule_id: str,
    frozen: bool,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Заморозить/разморозить гранулу — вечный факт (D4).

    Замороженные не затухают (confidence decay их не трогает) и не
    попадают под чистку — вечные факты не требуют подтверждения.
    """
    return await celery_call(TASK_FREEZE, granule_id=granule_id, frozen=frozen)


@mcp.tool()
@tool_handler("memory_stale_list")
async def memory_stale_list(
    user_id: str | None = None,
    namespace: str | None = None,
    project_id: str | None = None,
    limit: int = 100,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Кандидаты на ревизию: устаревшие знания (Фаза 2.2).

    Критерий (динамический, без колонки-флага): status='asserted',
    confidence < stale_threshold (config, default 0.3) и нет доступа
    дольше stale_days (config, default 30). Статус НЕ меняется —
    решение за вызывающим (supersede/retract/freeze).
    """
    return await celery_call(
        TASK_STALE_LIST,
        user_id=user_id,
        namespace=namespace,
        project_id=project_id,
        limit=limit,
    )


@mcp.tool()
@tool_handler("memory_cluster_list")
async def memory_cluster_list(
    namespace: str | None = None,
    project_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Обзор кластеров Level 2 — тематические группы схожих гранул.

    Кластеры пересчитываются ночью хранимкой assign_clusters (миграция 022).
    До применения миграции возвращает {ok: false, reason: 'migration 022 pending'}.
    """
    return await celery_call(
        TASK_CLUSTER_LIST,
        namespace=namespace,
        project_id=project_id,
    )
