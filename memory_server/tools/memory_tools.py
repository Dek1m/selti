"""MCP tools for memory operations.

All tools delegate to Celery tasks via celery_call().
ACL checks and metadata coercion remain at tool level.
"""

import json
from typing import Any

from fastmcp import Context

from memory_server.config import settings
from memory_server.metrics import (
    SEARCH_RESULTS,
    MEMORY_COUNT,
)
from memory_server.server import mcp
from memory_server.tools.task_bridge import celery_call
from memory_server.utils.metrics_decorator import tool_handler

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
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Store a new memory record.

    Generates an embedding for the content and persists it to the database.
    Deduplication is applied automatically — returns existing record if a match is found.
    """
    metadata = _coerce_metadata(metadata)
    return await celery_call(
        TASK_STORE,
        content=content,
        user_id=user_id,
        metadata=metadata,
        namespace=namespace,
        importance=importance,
    )


@mcp.tool()
@tool_handler("memory_search")
async def memory_search(
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Search memories by semantic similarity.

    Returns memories matching the query, ordered by relevance score.
    """
    results = await celery_call(
        TASK_SEARCH,
        query=query,
        user_id=user_id,
        limit=limit,
        threshold=threshold,
        namespace=namespace,
    )
    SEARCH_RESULTS.labels(tool="memory_search").observe(len(results))
    return results


@mcp.tool()
@tool_handler("memory_ingest_batch")
async def memory_ingest_batch(
    entries: list[dict],
    user_id: str,
    ctx: Context | None = None,
) -> dict:
    """Store multiple memory records in batch.

    Entries format: [{content, metadata?, namespace?}, ...]
    Returns summary of inserted/skipped/updated counts.
    """
    return await celery_call(
        TASK_INGEST_BATCH,
        entries=entries,
        user_id=user_id,
    )


@mcp.tool()
@tool_handler("memory_stats")
async def memory_stats(
    user_id: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """Get memory statistics for a user — per-namespace counts and last updated."""
    result = await celery_call(TASK_STATS, user_id=user_id)
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
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Update an existing memory record.

    If content is provided, a new embedding is generated.
    """
    metadata = _coerce_metadata(metadata)
    return await celery_call(
        TASK_UPDATE,
        memory_id=id,
        content=content,
        metadata=metadata,
        importance=importance,
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
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List memory records with optional filtering and pagination."""
    return await celery_call(
        TASK_LIST,
        user_id=user_id,
        namespace=namespace,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
@tool_handler("memory_recent")
async def memory_recent(
    namespace: str | None = None,
    limit: int = 20,
    since: str | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Get the most recent memory records.

    Returns records ordered by creation time (newest first).
    Useful for checking what happened recently — "what did we do today", "last 10 records", etc.
    Pass 'since' as an ISO datetime string (e.g. '2026-07-25' or '2026-07-25T10:00:00')
    to filter records created after a specific point in time.
    """
    return await celery_call(
        TASK_RECENT,
        namespace=namespace,
        since=since,
        limit=limit,
    )


@mcp.tool()
@tool_handler("memory_forget")
async def memory_forget(
    user_id: str,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete all memories for a user, optionally filtered by namespace."""
    return await celery_call(
        TASK_FORGET,
        user_id=user_id,
        namespace=namespace,
    )


@mcp.tool()
@tool_handler("memory_archive")
async def memory_archive(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Archive a memory record (soft delete).

    Sets is_archived = true. The record is excluded from search, list, and recent queries
    but remains in the database for potential restoration.
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
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Создать связь между двумя гранулами.

    link_type: depends_on | used_by | extends | implements | contains | contained_by |
               calls | called_by | related_to | solves | tested_by | implements_adr |
               references | follows | precedes | alternative_to | causes | prevents |
               runs_on | exposes | mounts | derived_from | motivates | informs | informed_by |
               connected_to | contradicts
    """
    return await celery_call(
        TASK_ADD_RELATION,
        source_id=source_id,
        target_id=target_id,
        link_type=link_type,
        description=description,
        weight=weight,
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
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Обход графа от начальной гранулы (BFS).

    depth: максимальная глубина обхода (по умолчанию 3)
    link_types: фильтр по типам связей (по умолчанию все)
    """
    return await celery_call(
        TASK_TRAVERSE,
        start_id=start_id,
        depth=depth,
        link_types=link_types,
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
