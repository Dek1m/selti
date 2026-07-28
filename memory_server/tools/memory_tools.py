from datetime import datetime

import json
import logging
import time
from typing import Any

from fastmcp import Context

from memory_server.config import Namespace
from memory_server.exceptions import NotFoundError
from memory_server.memory.dedup import DedupAction
from memory_server.metrics import MCP_TOOL_CALLS_TOTAL, MCP_TOOL_DURATION_SECONDS
from memory_server.server import mcp

logger = logging.getLogger(__name__)


def _coerce_metadata(metadata: Any) -> dict | None:
    """Coerce metadata to dict if it's a JSON string.

    LLM agents sometimes serialize metadata as a string instead of an object.
    This causes double-encoding via asyncpg's jsonb codec.
    """
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
        logger.warning("metadata is a string but not valid JSON: %s", metadata[:200])
        return None
    return metadata


async def _track_tool(tool_name: str, coro):
    """Замерить и записать метрики для MCP tool."""
    start = time.monotonic()
    try:
        result = await coro
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="ok").inc()
        return result
    except Exception:
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="error").inc()
        raise
    finally:
        duration = time.monotonic() - start
        MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)


def _validate_namespace(namespace: str | None) -> None:
    if namespace is not None and namespace not in [ns.value for ns in Namespace]:
        raise ValueError(
            f"Invalid namespace: {namespace}. Allowed: {[ns.value for ns in Namespace]}"
        )


@mcp.tool()
async def memory_store(
    content: str,
    user_id: str,
    metadata: str | dict | None = None,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Store a new memory record.

    Generates an embedding for the content and persists it to the database.
    Deduplication is applied automatically — returns existing record if a match is found.
    """
    _validate_namespace(namespace)
    assert ctx is not None
    metadata = _coerce_metadata(metadata)
    service = ctx.request_context.lifespan_context["service"]
    try:
        record, action = await _track_tool("memory_store", service.store(
            content=content,
            user_id=user_id,
            metadata=metadata,
            namespace=namespace,
        ))
        result = record.model_dump(mode="json")
        result["_dedup_action"] = action.value
        return result
    except Exception as e:
        logger.exception("Failed to store memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
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
    _validate_namespace(namespace)
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        results = await _track_tool("memory_search", service.search(
            query=query,
            user_id=user_id,
            limit=limit,
            threshold=threshold,
            namespace=namespace,
        ))
        return [r.model_dump(mode="json") for r in results]
    except Exception as e:
        logger.exception("Failed to search memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_ingest_batch(
    entries: list[dict],
    user_id: str,
    ctx: Context | None = None,
) -> dict:
    """Store multiple memory records in batch.

    Entries format: [{content, metadata?, namespace?}, ...]
    Returns summary of inserted/skipped/updated counts.
    """
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]

    async def _run():
        summary: dict[str, int] = {"insert": 0, "skip": 0, "update": 0}
        results = []

        # Phase 1: Dedup check for each entry
        to_insert = []  # entries that need insertion
        for entry in entries:
            _validate_namespace(entry.get("namespace"))
            ns = entry.get("namespace", "default")

            if service.config.dedup_enabled:
                decision = await service.dedup.check(entry["content"], user_id, ns)
                if decision.action in (DedupAction.SKIP, DedupAction.UPDATE):
                    summary[decision.action.value] += 1
                    results.append({
                        "id": decision.existing_id,
                        "action": decision.action.value,
                        "namespace": ns,
                    })
                    continue

            to_insert.append({
                "content": entry["content"],
                "metadata": _coerce_metadata(entry.get("metadata")) or {},
                "namespace": ns,
                "content_hash": decision.content_hash if service.config.dedup_enabled else None,
                "embedding": decision.embedding if service.config.dedup_enabled else None,
            })

        # Phase 2: Batch embed (if any entries to insert)
        if to_insert:
            from memory_server.exceptions import EmbeddingError

            # Separate cached and uncached embeddings
            texts_to_embed = []
            indices_to_embed = []
            for i, item in enumerate(to_insert):
                if item["embedding"] is not None:
                    continue  # already cached from dedup
                texts_to_embed.append(item["content"])
                indices_to_embed.append(i)

            if texts_to_embed:
                embeddings = await service.embedding.embed_many(texts_to_embed)
                for idx, emb in zip(indices_to_embed, embeddings):
                    to_insert[idx]["embedding"] = emb

            # Phase 3: Batch SQL insert
            if to_insert:
                user_ids = [user_id] * len(to_insert)
                contents = [item["content"] for item in to_insert]
                embeddings_list = [item["embedding"] for item in to_insert]
                metadatas_list = [item["metadata"] for item in to_insert]
                namespaces_list = [item["namespace"] for item in to_insert]
                content_hashes_list = [item["content_hash"] for item in to_insert]

                ids = await service.repository.insert_batch(
                    user_ids=user_ids,
                    contents=contents,
                    embeddings=embeddings_list,
                    metadatas=metadatas_list,
                    namespaces=namespaces_list,
                    content_hashes=content_hashes_list,
                )

                for rid, item in zip(ids, to_insert):
                    summary["insert"] += 1
                    results.append({
                        "id": rid,
                        "action": "insert",
                        "namespace": item["namespace"],
                    })

        return {"results": results, "summary": summary}

    return await _track_tool("memory_ingest_batch", _run())


@mcp.tool()
async def memory_stats(
    user_id: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """Get memory statistics for a user — per-namespace counts and last updated."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]

    result = await _track_tool("memory_stats", service.get_stats(user_id))
    return [item.model_dump(mode="json") for item in result]


@mcp.tool()
async def memory_find_similar(
    content: str,
    user_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> list[dict]:
    """Find semantically similar memories without storing."""
    _validate_namespace(namespace)
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]

    results = await _track_tool("memory_find_similar", service.search(
        query=content,
        user_id=user_id,
        limit=limit,
        threshold=threshold,
        namespace=namespace,
    ))
    return [r.model_dump(mode="json") for r in results]


@mcp.tool()
async def memory_get(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Retrieve a single memory record by its ID."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        record = await _track_tool("memory_get", service.get(memory_id=id))
        return record.model_dump(mode="json")
    except NotFoundError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to get memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_update(
    id: str,
    content: str | None = None,
    metadata: str | dict | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Update an existing memory record.

    If content is provided, a new embedding is generated.
    """
    assert ctx is not None
    metadata = _coerce_metadata(metadata)
    service = ctx.request_context.lifespan_context["service"]
    try:
        record = await _track_tool("memory_update", service.update(
            memory_id=id,
            content=content,
            metadata=metadata,
        ))
        return record.model_dump(mode="json")
    except NotFoundError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to update memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_delete(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete a memory record by its ID."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        success = await _track_tool("memory_delete", service.delete(memory_id=id))
        return {"success": success}
    except NotFoundError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to delete memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_list(
    user_id: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
    offset: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List memory records with optional filtering and pagination."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        result = await _track_tool("memory_list", service.list(
            user_id=user_id,
            namespace=namespace,
            limit=limit,
            offset=offset,
        ))
        return {
            "items": [r.model_dump(mode="json") for r in result.items],
            "total": result.total,
        }
    except Exception as e:
        logger.exception("Failed to list memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
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
    _validate_namespace(namespace)
    assert ctx is not None
    if since is not None:
        since = datetime.fromisoformat(since)
    service = ctx.request_context.lifespan_context["service"]
    try:
        results = await _track_tool("memory_recent", service.recent(
            namespace=namespace,
            since=since,
            limit=limit,
        ))
        return [r.model_dump(mode="json") for r in results]
    except Exception as e:
        logger.exception("Failed to get recent memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_forget(
    user_id: str,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete all memories for a user, optionally filtered by namespace."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        deleted = await _track_tool("memory_forget", service.forget(user_id=user_id, namespace=namespace))
        return {"deleted_count": deleted}
    except Exception as e:
        logger.exception("Failed to forget memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_archive(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Archive a memory record (soft delete).

    Sets is_archived = true. The record is excluded from search, list, and recent queries
    but remains in the database for potential restoration.
    """
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        success = await _track_tool("memory_archive", service.archive(memory_id=id))
        return {"success": success}
    except NotFoundError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to archive memory")
        raise RuntimeError(str(e)) from e


# ── Graph tools ──

@mcp.tool()
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
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        rel_id = await _track_tool("memory_link", service.add_relation(
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
            description=description,
            weight=weight,
        ))
        return {"ok": True, "relation_id": rel_id}
    except NotFoundError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to create link")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_unlink(
    source_id: str,
    target_id: str,
    link_type: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Удалить связь между двумя гранулами."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        deleted = await _track_tool("memory_unlink", service.delete_relation(
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
        ))
        return {"ok": deleted}
    except Exception as e:
        logger.exception("Failed to delete link")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_get_relations(
    id: str,
    link_type: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Получить входящие и исходящие связи гранулы.

    Возвращает {incoming: [...], outgoing: [...]}
    """
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        result = await _track_tool("memory_get_relations", service.get_relations(
            memory_id=id,
            link_type=link_type,
        ))
        return {
            "incoming": [r.model_dump(mode="json") for r in result.incoming],
            "outgoing": [r.model_dump(mode="json") for r in result.outgoing],
        }
    except NotFoundError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to get relations")
        raise RuntimeError(str(e)) from e


@mcp.tool()
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
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        result = await _track_tool("memory_traverse", service.traverse(
            start_id=start_id,
            depth=depth,
            link_types=link_types,
        ))
        return {
            "nodes": result.nodes,
            "edges": [e.model_dump(mode="json") for e in result.edges],
        }
    except NotFoundError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to traverse graph")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_graph_stats(
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Статистика графа знаний: связность, сироты, кластеры по namespace и типам связей."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        stats = await _track_tool("memory_graph_stats", service.get_graph_stats())
        return stats.model_dump(mode="json")
    except Exception as e:
        logger.exception("Failed to get graph stats")
        raise RuntimeError(str(e)) from e
