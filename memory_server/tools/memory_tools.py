from datetime import datetime

import json
import logging
import time
from typing import Any

from fastmcp import Context

from memory_server.config import settings
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



@mcp.tool()
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
    assert ctx is not None
    metadata = _coerce_metadata(metadata)
    service = ctx.request_context.lifespan_context["service"]
    logger.info("memory_store: content_len=%d namespace=%s user_id=%s importance=%s",
                len(content), namespace, user_id, importance)
    try:
        record, action = await _track_tool("memory_store", service.store(
            content=content,
            user_id=user_id,
            metadata=metadata,
            namespace=namespace,
            importance=importance,
        ))
        result = record.model_dump(mode="json")
        result["_dedup_action"] = action.value
        logger.info("memory_store: done id=%s dedup_action=%s namespace=%s",
                    record.id, action.value, record.namespace)
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
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    logger.info("memory_search: query=%s namespace=%s limit=%d threshold=%.2f user_id=%s",
                query[:200], namespace, limit, threshold, user_id)
    try:
        results = await _track_tool("memory_search", service.search(
            query=query,
            user_id=user_id,
            limit=limit,
            threshold=threshold,
            namespace=namespace,
        ))
        logger.info("memory_search: done count=%d", len(results))
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

        logger.info("memory_ingest_batch: received %d entries, user_id=%s",
                     len(entries), user_id)
        for i, entry in enumerate(entries):
            title_preview = (entry.get("metadata") or {}).get("title", "N/A")[:60]
            logger.info("  entry[%d]: namespace=%s, importance=%s, title=%s, content_len=%d",
                         i,
                         entry.get("namespace", "default"),
                         entry.get("importance", 3),
                         title_preview,
                         len(entry.get("content", "")))

        # Phase 1: Dedup check for each entry
        to_insert = []  # entries that need insertion
        for entry in entries:
            ns = entry.get("namespace", "default")
            entry_metadata = _coerce_metadata(entry.get("metadata"))

            if service.config.dedup_enabled:
                decision = await service.dedup.check(entry["content"], user_id, ns, metadata=entry_metadata)
                logger.info("  dedup: namespace=%s action=%s existing_id=%s score=%s",
                             ns, decision.action.value, decision.existing_id, decision.existing_score)
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
                "metadata": entry_metadata or {},
                "namespace": ns,
                "importance": entry.get("importance", 3),
                "content_hash": decision.content_hash if service.config.dedup_enabled else None,
                "embedding": decision.embedding if service.config.dedup_enabled else None,
            })

        logger.info("  phase1 complete: %d to_insert, %d skipped, %d updated",
                     len(to_insert), summary["skip"], summary["update"])

        # Phase 2: Batch embed (if any entries to insert)
        if to_insert:
            from memory_server.exceptions import EmbeddingError

            # Separate cached and uncached embeddings
            texts_to_embed = []
            indices_to_embed = []
            for i, item in enumerate(to_insert):
                if item["embedding"] is not None:
                    logger.info("  embed[%d]: cached from dedup (len=%d)", i, len(item["embedding"]))
                    continue
                texts_to_embed.append(item["content"])
                indices_to_embed.append(i)

            logger.info("  phase2: %d texts to embed, %d cached", len(texts_to_embed), len(to_insert) - len(texts_to_embed))

            if texts_to_embed:
                try:
                    embeddings = await service.embedding.embed_many(texts_to_embed)
                    logger.info("  embed_many: got %d embeddings, dim=%d", len(embeddings), len(embeddings[0]) if embeddings else 0)
                    for idx, emb in zip(indices_to_embed, embeddings):
                        to_insert[idx]["embedding"] = emb
                except Exception as e:
                    logger.exception("Embedding failed for %d texts", len(texts_to_embed))
                    raise

            # Phase 3: Resolve namespace_ids
            namespace_ids = []
            for item in to_insert:
                try:
                    ns_record = await service.ns_repo.get_or_create(item["namespace"])
                    namespace_ids.append(ns_record.id)
                    logger.info("  ns_resolve: namespace=%s -> id=%s", item["namespace"], ns_record.id)
                except Exception as e:
                    logger.exception("Failed to resolve namespace: %s", item["namespace"])
                    raise

            # Phase 4: Batch SQL insert
            if to_insert:
                user_ids = [user_id] * len(to_insert)
                contents = [item["content"] for item in to_insert]
                embeddings_list = [item["embedding"] for item in to_insert]
                metadatas_list = [item["metadata"] for item in to_insert]
                namespaces_list = [item["namespace"] for item in to_insert]
                content_hashes_list = [item["content_hash"] for item in to_insert]
                importances_list = [item["importance"] for item in to_insert]

                logger.info("  phase4: insert_batch %d entries, user_id=%s", len(to_insert), user_id)
                logger.info("    embeddings_list type=%s len=%d", type(embeddings_list).__name__, len(embeddings_list))
                if embeddings_list:
                    logger.info("    embeddings_list[0] type=%s len=%d first_3=%s",
                                 type(embeddings_list[0]).__name__,
                                 len(embeddings_list[0]) if hasattr(embeddings_list[0], '__len__') else '?',
                                 str(embeddings_list[0][:3]) if hasattr(embeddings_list[0], '__getitem__') else '?')

                try:
                    ids = await service.repository.insert_batch(
                        user_ids=user_ids,
                        contents=contents,
                        embeddings=embeddings_list,
                        metadatas=metadatas_list,
                        namespaces=namespaces_list,
                        namespace_ids=namespace_ids,
                        content_hashes=content_hashes_list,
                        importances=importances_list,
                    )
                    logger.info("  insert_batch success: %d ids returned", len(ids))
                except Exception as e:
                    logger.exception("insert_batch FAILED: %s", str(e)[:500])
                    raise

                for rid, item in zip(ids, to_insert):
                    summary["insert"] += 1
                    results.append({
                        "id": rid,
                        "action": "insert",
                        "namespace": item["namespace"],
                    })

        logger.info("memory_ingest_batch: complete summary=%s", summary)
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
    logger.info("memory_stats: user_id=%s", user_id)
    result = await _track_tool("memory_stats", service.get_stats(user_id))
    logger.info("memory_stats: done namespaces=%d", len(result))
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
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    logger.info("memory_find_similar: content_len=%d namespace=%s limit=%d threshold=%.2f user_id=%s",
                len(content), namespace, limit, threshold, user_id)
    results = await _track_tool("memory_find_similar", service.search(
        query=content,
        user_id=user_id,
        limit=limit,
        threshold=threshold,
        namespace=namespace,
    ))
    logger.info("memory_find_similar: done count=%d", len(results))
    return [r.model_dump(mode="json") for r in results]


@mcp.tool()
async def memory_get(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Retrieve a single memory record by its ID."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    logger.info("memory_get: id=%s", id)
    try:
        record = await _track_tool("memory_get", service.get(memory_id=id))
        logger.info("memory_get: done found=true id=%s namespace=%s", record.id, record.namespace)
        return record.model_dump(mode="json")
    except NotFoundError as e:
        logger.info("memory_get: done found=false id=%s", id)
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.exception("Failed to get memory")
        raise RuntimeError(str(e)) from e


@mcp.tool()
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
    assert ctx is not None
    metadata = _coerce_metadata(metadata)
    service = ctx.request_context.lifespan_context["service"]
    logger.info("memory_update: id=%s has_content=%s has_metadata=%s importance=%s",
                id, content is not None, metadata is not None, importance)
    try:
        record = await _track_tool("memory_update", service.update(
            memory_id=id,
            content=content,
            metadata=metadata,
            importance=importance,
        ))
        logger.info("memory_update: done id=%s namespace=%s", record.id, record.namespace)
        return record.model_dump(mode="json")
    except NotFoundError as e:
        logger.info("memory_update: not found id=%s", id)
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
    logger.info("memory_delete: id=%s", id)
    try:
        success = await _track_tool("memory_delete", service.delete(memory_id=id))
        logger.info("memory_delete: done id=%s success=%s", id, success)
        return {"success": success}
    except NotFoundError as e:
        logger.info("memory_delete: not found id=%s", id)
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
    logger.info("memory_list: namespace=%s limit=%d offset=%d user_id=%s",
                namespace, limit, offset, user_id)
    try:
        result = await _track_tool("memory_list", service.list(
            user_id=user_id,
            namespace=namespace,
            limit=limit,
            offset=offset,
        ))
        logger.info("memory_list: done total=%d items=%d", result.total, len(result.items))
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
    assert ctx is not None
    if since is not None:
        since = datetime.fromisoformat(since)
    service = ctx.request_context.lifespan_context["service"]
    logger.info("memory_recent: namespace=%s limit=%d since=%s", namespace, limit, since)
    try:
        results = await _track_tool("memory_recent", service.recent(
            namespace=namespace,
            since=since,
            limit=limit,
        ))
        logger.info("memory_recent: done count=%d", len(results))
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
    logger.info("memory_forget: user_id=%s namespace=%s", user_id, namespace)
    try:
        deleted = await _track_tool("memory_forget", service.forget(user_id=user_id, namespace=namespace))
        logger.info("memory_forget: done deleted_count=%d", deleted)
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
    logger.info("memory_archive: id=%s", id)
    try:
        success = await _track_tool("memory_archive", service.archive(memory_id=id))
        logger.info("memory_archive: done id=%s success=%s", id, success)
        return {"success": success}
    except NotFoundError as e:
        logger.info("memory_archive: not found id=%s", id)
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
    logger.info("memory_link: source=%s target=%s type=%s weight=%.2f",
                source_id, target_id, link_type, weight)
    try:
        rel_id = await _track_tool("memory_link", service.add_relation(
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
            description=description,
            weight=weight,
        ))
        logger.info("memory_link: done relation_id=%s", rel_id)
        return {"ok": True, "relation_id": rel_id}
    except NotFoundError as e:
        logger.info("memory_link: not found granule: %s", e)
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
    logger.info("memory_unlink: source=%s target=%s type=%s", source_id, target_id, link_type)
    try:
        deleted = await _track_tool("memory_unlink", service.delete_relation(
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
        ))
        logger.info("memory_unlink: done success=%s", deleted)
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
    logger.info("memory_get_relations: id=%s link_type=%s", id, link_type)
    try:
        result = await _track_tool("memory_get_relations", service.get_relations(
            memory_id=id,
            link_type=link_type,
        ))
        logger.info("memory_get_relations: done incoming=%d outgoing=%d",
                    len(result.incoming), len(result.outgoing))
        return {
            "incoming": [r.model_dump(mode="json") for r in result.incoming],
            "outgoing": [r.model_dump(mode="json") for r in result.outgoing],
        }
    except NotFoundError as e:
        logger.info("memory_get_relations: not found id=%s", id)
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
    logger.info("memory_traverse: start_id=%s depth=%d link_types=%s",
                start_id, depth, link_types)
    try:
        result = await _track_tool("memory_traverse", service.traverse(
            start_id=start_id,
            depth=depth,
            link_types=link_types,
        ))
        logger.info("memory_traverse: done nodes=%d edges=%d",
                    len(result.nodes), len(result.edges))
        return {
            "nodes": result.nodes,
            "edges": [e.model_dump(mode="json") for e in result.edges],
        }
    except NotFoundError as e:
        logger.info("memory_traverse: start not found id=%s", start_id)
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
    logger.info("memory_graph_stats: called")
    try:
        stats = await _track_tool("memory_graph_stats", service.get_graph_stats())
        logger.info("memory_graph_stats: done nodes=%d edges=%d orphans=%d",
                    stats.total_nodes, stats.total_edges, stats.orphans)
        return stats.model_dump(mode="json")
    except Exception as e:
        logger.exception("Failed to get graph stats")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_version(
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Версия athena-memory сервера."""
    from pathlib import Path

    version_file = Path(__file__).parent.parent.parent / "VERSION"
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    logger.info("memory_version: version=%s server=%s model=%s",
                version, settings.mcp_server_name, settings.embedding_model)
    return {
        "version": version,
        "server": settings.mcp_server_name,
        "model": settings.embedding_model,
    }


@mcp.tool()
async def memory_namespaces(
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Получить список всех namespace из реестра.

    Возвращает uid, name и description каждого namespace.
    Используй для динамического определения допустимых namespace.
    """
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    logger.info("memory_namespaces: called")
    try:
        namespaces = await _track_tool("memory_namespaces", service.ns_repo.list_all())
        logger.info("memory_namespaces: done count=%d", len(namespaces))
        return [
            {"uid": ns.uid, "name": ns.name, "description": ns.description}
            for ns in namespaces
        ]
    except Exception as e:
        logger.exception("Failed to list namespaces")
        raise RuntimeError(str(e)) from e
