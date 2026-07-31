"""MCP tools for memory operations.

All tools delegate to Celery tasks via celery_call().
ACL checks and metadata coercion remain at tool level.
"""

import json
import logging
from typing import Any

from fastmcp import Context

from memory_server.config import settings
from memory_server.metrics import (
    SEARCH_RESULTS,
    MEMORY_COUNT,
    DEDUP_SKIPPED_TOTAL,
    DEDUP_INSERTED_TOTAL,
)
from memory_server.server import mcp
from memory_server.tools.task_bridge import celery_call
from memory_server.utils.metrics_decorator import track_tool_metrics

logger = logging.getLogger(__name__)

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
        logger.warning("metadata is a string but not valid JSON", extra={"preview": metadata[:200]})
        return None
    return metadata


# ══════════════════════════════════════════════════════════════════
# Memory tools
# ══════════════════════════════════════════════════════════════════


@mcp.tool()
@track_tool_metrics("memory_store")
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
    logger.info("memory_store: START", extra={
        "content_len": len(content), "namespace": namespace,
        "user_id": user_id, "importance": importance,
    })
    try:
        result = await celery_call(
            TASK_STORE,
            content=content,
            user_id=user_id,
            metadata=metadata,
            namespace=namespace,
            importance=importance,
        )
        # Обновляем метрики дедупликации
        ns = namespace or "default"
        action = result.get("_dedup_action", "insert")
        if action in ("skip", "update"):
            DEDUP_SKIPPED_TOTAL.labels(namespace=ns, reason=action).inc()
        elif action == "insert":
            DEDUP_INSERTED_TOTAL.labels(namespace=ns).inc()
        logger.info("memory_store: done", extra={
            "id": result.get("id"), "dedup_action": action, "namespace": result.get("namespace"),
        })
        return result
    except Exception as e:
        logger.exception("Failed to store memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_search")
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
    logger.info("memory_search: START", extra={
        "query": query[:200], "namespace": namespace,
        "limit": limit, "threshold": threshold, "user_id": user_id,
    })
    try:
        results = await celery_call(
            TASK_SEARCH,
            query=query,
            user_id=user_id,
            limit=limit,
            threshold=threshold,
            namespace=namespace,
        )
        SEARCH_RESULTS.observe(len(results))
        logger.info("memory_search: done", extra={"count": len(results)})
        return results
    except Exception as e:
        logger.exception("Failed to search memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_ingest_batch")
async def memory_ingest_batch(
    entries: list[dict],
    user_id: str,
    ctx: Context | None = None,
) -> dict:
    """Store multiple memory records in batch.

    Entries format: [{content, metadata?, namespace?}, ...]
    Returns summary of inserted/skipped/updated counts.
    """
    logger.info("memory_ingest_batch: START", extra={
        "entries": len(entries), "user_id": user_id,
    })
    try:
        result = await celery_call(
            TASK_INGEST_BATCH,
            entries=entries,
            user_id=user_id,
        )
        # Обновляем метрики дедупликации
        for r in result.get("results", []):
            ns = r.get("namespace", "default")
            action = r.get("action")
            if action in ("skip", "update"):
                DEDUP_SKIPPED_TOTAL.labels(namespace=ns, reason=action).inc()
            elif action == "insert":
                DEDUP_INSERTED_TOTAL.labels(namespace=ns).inc()
        logger.info("memory_ingest_batch: done", extra={"summary": result.get("summary")})
        return result
    except Exception as e:
        logger.exception("Failed to ingest batch")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_stats")
async def memory_stats(
    user_id: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """Get memory statistics for a user — per-namespace counts and last updated."""
    logger.info("memory_stats", extra={"user_id": user_id})
    try:
        result = await celery_call(
            TASK_STATS, user_id=user_id,
        )
        for item in result:
            MEMORY_COUNT.labels(namespace=item["namespace"]).set(item["count"])
        logger.info("memory_stats: done", extra={"namespaces": len(result)})
        return result
    except Exception as e:
        logger.exception("Failed to get stats")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_find_similar")
async def memory_find_similar(
    content: str,
    user_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """Find semantically similar memories without storing."""
    logger.info("memory_find_similar: START", extra={
        "content_len": len(content), "namespace": namespace,
        "limit": limit, "threshold": threshold, "user_id": user_id,
    })
    try:
        results = await celery_call(
            TASK_FIND_SIMILAR,
            content=content,
            user_id=user_id,
            limit=limit,
            threshold=threshold,
            namespace=namespace,
        )
        SEARCH_RESULTS.observe(len(results))
        logger.info("memory_find_similar: done", extra={"count": len(results)})
        return results
    except Exception as e:
        logger.exception("Failed to find similar")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_get")
async def memory_get(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Retrieve a single memory record by its ID."""
    logger.info("memory_get", extra={"id": id})
    try:
        result = await celery_call(
            TASK_GET, memory_id=id,
        )
        logger.info("memory_get: found", extra={"id": result.get("id"), "namespace": result.get("namespace")})
        return result
    except Exception as e:
        logger.exception("Failed to get memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_update")
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
    logger.info("memory_update: START", extra={
        "id": id, "has_content": content is not None,
        "has_metadata": metadata is not None, "importance": importance,
    })
    try:
        result = await celery_call(
            TASK_UPDATE,
            memory_id=id,
            content=content,
            metadata=metadata,
            importance=importance,
        )
        logger.info("memory_update: done", extra={"id": result.get("id"), "namespace": result.get("namespace")})
        return result
    except Exception as e:
        logger.exception("Failed to update memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_delete")
async def memory_delete(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete a memory record by its ID."""
    logger.info("memory_delete", extra={"id": id})
    try:
        result = await celery_call(
            TASK_DELETE, memory_id=id,
        )
        logger.info("memory_delete: done", extra={"id": id, "success": result.get("success")})
        return result
    except Exception as e:
        logger.exception("Failed to delete memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_list")
async def memory_list(
    user_id: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
    offset: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List memory records with optional filtering and pagination."""
    logger.info("memory_list", extra={
        "namespace": namespace, "limit": limit,
        "offset": offset, "user_id": user_id,
    })
    try:
        result = await celery_call(
            TASK_LIST,
            user_id=user_id,
            namespace=namespace,
            limit=limit,
            offset=offset,
        )
        logger.info("memory_list: done", extra={"total": result.get("total"), "items": len(result.get("items", []))})
        return result
    except Exception as e:
        logger.exception("Failed to list memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_recent")
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
    logger.info("memory_recent", extra={"namespace": namespace, "limit": limit, "since": since})
    try:
        results = await celery_call(
            TASK_RECENT,
            namespace=namespace,
            since=since,
            limit=limit,
        )
        logger.info("memory_recent: done", extra={"count": len(results)})
        return results
    except Exception as e:
        logger.exception("Failed to get recent memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_forget")
async def memory_forget(
    user_id: str,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete all memories for a user, optionally filtered by namespace."""
    logger.info("memory_forget", extra={"user_id": user_id, "namespace": namespace})
    try:
        result = await celery_call(
            TASK_FORGET,
            user_id=user_id,
            namespace=namespace,
        )
        logger.info("memory_forget: done", extra={"deleted_count": result.get("deleted_count")})
        return result
    except Exception as e:
        logger.exception("Failed to forget memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_archive")
async def memory_archive(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Archive a memory record (soft delete).

    Sets is_archived = true. The record is excluded from search, list, and recent queries
    but remains in the database for potential restoration.
    """
    logger.info("memory_archive", extra={"id": id})
    try:
        result = await celery_call(
            TASK_ARCHIVE, memory_id=id,
        )
        logger.info("memory_archive: done", extra={"id": id, "success": result.get("success")})
        return result
    except Exception as e:
        logger.exception("Failed to archive memory")
        raise RuntimeError(str(e)) from e


# ── Graph tools ──


@mcp.tool()
@track_tool_metrics("memory_link")
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
    logger.info("memory_link", extra={
        "source": source_id, "target": target_id,
        "type": link_type, "weight": weight,
    })
    try:
        result = await celery_call(
            TASK_ADD_RELATION,
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
            description=description,
            weight=weight,
        )
        logger.info("memory_link: done", extra={"relation_id": result.get("relation_id")})
        return result
    except Exception as e:
        logger.exception("Failed to create link")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_unlink")
async def memory_unlink(
    source_id: str,
    target_id: str,
    link_type: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Удалить связь между двумя гранулами."""
    logger.info("memory_unlink", extra={
        "source": source_id, "target": target_id, "type": link_type,
    })
    try:
        result = await celery_call(
            TASK_DELETE_RELATION,
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
        )
        logger.info("memory_unlink: done", extra={"success": result.get("ok")})
        return result
    except Exception as e:
        logger.exception("Failed to delete link")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_get_relations")
async def memory_get_relations(
    source_id: str,
    link_type: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Получить входящие и исходящие связи гранулы.

    Возвращает {incoming: [...], outgoing: [...]}
    """
    logger.info("memory_get_relations", extra={"source_id": source_id, "link_type": link_type})
    try:
        result = await celery_call(
            TASK_GET_RELATIONS,
            source_id=source_id,
            link_type=link_type,
        )
        logger.info("memory_get_relations: done", extra={
            "incoming": len(result.get("incoming", [])),
            "outgoing": len(result.get("outgoing", [])),
        })
        return result
    except Exception as e:
        logger.exception("Failed to get relations")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_traverse")
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
    logger.info("memory_traverse", extra={
        "start_id": start_id, "depth": depth, "link_types": link_types,
    })
    try:
        result = await celery_call(
            TASK_TRAVERSE,
            start_id=start_id,
            depth=depth,
            link_types=link_types,
        )
        logger.info("memory_traverse: done", extra={
            "nodes": len(result.get("nodes", [])),
            "edges": len(result.get("edges", [])),
        })
        return result
    except Exception as e:
        logger.exception("Failed to traverse graph")
        raise RuntimeError(str(e)) from e


@mcp.tool()
@track_tool_metrics("memory_graph_stats")
async def memory_graph_stats(
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Статистика графа знаний: связность, сироты, кластеры по namespace и типам связей."""
    logger.info("memory_graph_stats")
    try:
        stats = await celery_call(
            TASK_GRAPH_STATS,
        )
        logger.info("memory_graph_stats: done", extra={
            "granules": stats.get("total_granules"),
            "relations": stats.get("total_relations"),
            "orphans": stats.get("orphans"),
        })
        return stats
    except Exception as e:
        logger.exception("Failed to get graph stats")
        raise RuntimeError(str(e)) from e


# ── Version (локальный, без Celery) ──


@mcp.tool()
@track_tool_metrics("memory_version")
async def memory_version(
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Версия athena-memory сервера."""
    from pathlib import Path

    version_file = Path(__file__).parent.parent.parent / "VERSION"
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    logger.info("memory_version", extra={
        "version": version, "server": settings.mcp_server_name,
        "model": settings.embedding_model,
    })
    return {
        "version": version,
        "server": settings.mcp_server_name,
        "model": settings.embedding_model,
    }


# ── Namespaces ──


@mcp.tool()
@track_tool_metrics("memory_namespaces")
async def memory_namespaces(
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Получить список всех namespace из реестра.

    Возвращает uid, name и description каждого namespace.
    Используй для динамического определения допустимых namespace.
    """
    logger.info("memory_namespaces")
    try:
        namespaces = await celery_call(
            TASK_NAMESPACES,
        )
        logger.info("memory_namespaces: done", extra={"count": len(namespaces)})
        return namespaces
    except Exception as e:
        logger.exception("Failed to list namespaces")
        raise RuntimeError(str(e)) from e
