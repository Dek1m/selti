import asyncio
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

# Таймаут на каждый тул — 60 секунд
TOOL_TIMEOUT_SECONDS = 60


class _StepTimer:
    """Контекстный менеджер для замера под-операций внутри тулов."""

    def __init__(self, step_name: str):
        self.step_name = step_name
        self.start: float = 0

    async def __aenter__(self):
        self.start = time.monotonic()
        logger.info("step: START", extra={"step": self.step_name})
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        duration = time.monotonic() - self.start
        status = "ERROR" if exc_type else "OK"
        logger.info("step: %s" % status, extra={
            "step": self.step_name, "duration_ms": round(duration * 1000, 1),
        })
        return False


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
        logger.warning("metadata is a string but not valid JSON", extra={"preview": metadata[:200]})
        return None
    return metadata


async def _track_tool(tool_name: str, coro, *, timeout: float | None = TOOL_TIMEOUT_SECONDS):
    """Замерить и записать метрики для MCP tool с таймаутом."""
    start = time.monotonic()
    logger.info("tool: START", extra={"tool": tool_name, "timeout": timeout})
    try:
        if timeout is not None:
            result = await asyncio.wait_for(coro, timeout=timeout)
        else:
            result = await coro
        duration = time.monotonic() - start
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="ok").inc()
        MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
        logger.info("tool: DONE", extra={"tool": tool_name, "duration_ms": round(duration * 1000, 1)})
        return result
    except asyncio.TimeoutError:
        duration = time.monotonic() - start
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="timeout").inc()
        MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
        logger.error("tool: TIMEOUT", extra={
            "tool": tool_name, "duration_ms": round(duration * 1000, 1), "timeout": timeout,
        })
        raise TimeoutError(f"Tool '{tool_name}' timed out after {timeout}s") from None
    except Exception:
        duration = time.monotonic() - start
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="error").inc()
        MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
        logger.error("tool: ERROR", extra={"tool": tool_name, "duration_ms": round(duration * 1000, 1)})
        raise
    finally:
        duration = time.monotonic() - start
        logger.info("tool: FINALLY", extra={"tool": tool_name, "total_duration_ms": round(duration * 1000, 1)})



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
    logger.info("memory_store: START", extra={
        "content_len": len(content), "namespace": namespace,
        "user_id": user_id, "importance": importance,
    })
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
        logger.info("memory_store: done", extra={
            "id": record.id, "dedup_action": action.value, "namespace": record.namespace,
        })
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
    logger.info("memory_search: START", extra={
        "query": query[:200], "namespace": namespace,
        "limit": limit, "threshold": threshold, "user_id": user_id,
    })

    async def _search_with_steps():
        async with _StepTimer("memory_search/step1/embed"):
            query_embedding = await service.embedding.embed(query)
        async with _StepTimer("memory_search/step2/sql_search"):
            results = await service.repository.search(
                query_embedding=query_embedding,
                user_id=user_id,
                limit=limit,
                threshold=threshold,
                namespace=namespace,
            )
        return results

    try:
        results = await _track_tool("memory_search", _search_with_steps())
        logger.info("memory_search: done", extra={"count": len(results)})
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

        logger.info("memory_ingest_batch: START", extra={
            "entries": len(entries), "user_id": user_id,
        })
        for i, entry in enumerate(entries):
            title_preview = (entry.get("metadata") or {}).get("title", "N/A")[:60]
            logger.info("memory_ingest_batch: entry", extra={
                "index": i,
                "namespace": entry.get("namespace", "default"),
                "importance": entry.get("importance", 3),
                "title": title_preview,
                "content_len": len(entry.get("content", "")),
            })

        # Phase 1: Dedup check for each entry
        to_insert = []  # entries that need insertion
        for entry in entries:
            ns = entry.get("namespace", "default")
            entry_metadata = _coerce_metadata(entry.get("metadata"))

            if service.config.dedup_enabled:
                decision = await service.dedup.check(entry["content"], user_id, ns, metadata=entry_metadata)
                logger.info("memory_ingest_batch: dedup", extra={
                    "namespace": ns, "action": decision.action.value,
                    "existing_id": decision.existing_id, "score": decision.existing_score,
                })
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

        logger.info("memory_ingest_batch: phase1 complete", extra={
            "to_insert": len(to_insert), "skipped": summary["skip"], "updated": summary["update"],
        })

        # Phase 2: Batch embed (if any entries to insert)
        if to_insert:
            from memory_server.exceptions import EmbeddingError

            # Separate cached and uncached embeddings
            texts_to_embed = []
            indices_to_embed = []
            for i, item in enumerate(to_insert):
                if item["embedding"] is not None:
                    logger.info("memory_ingest_batch: embed cached", extra={
                        "index": i, "len": len(item["embedding"]),
                    })
                    continue
                texts_to_embed.append(item["content"])
                indices_to_embed.append(i)

            logger.info("memory_ingest_batch: phase2", extra={
                "to_embed": len(texts_to_embed),
                "cached": len(to_insert) - len(texts_to_embed),
            })

            if texts_to_embed:
                try:
                    embeddings = await service.embedding.embed_many(texts_to_embed)
                    logger.info("memory_ingest_batch: embed_many", extra={
                        "count": len(embeddings),
                        "dim": len(embeddings[0]) if embeddings else 0,
                    })
                    for idx, emb in zip(indices_to_embed, embeddings):
                        to_insert[idx]["embedding"] = emb
                except Exception as e:
                    logger.exception("Embedding failed", extra={"count": len(texts_to_embed)})
                    raise

            # Phase 3: Resolve namespace_ids
            namespace_ids = []
            for item in to_insert:
                try:
                    ns_record = await service.ns_repo.get_or_create(item["namespace"])
                    namespace_ids.append(ns_record.id)
                    logger.info("memory_ingest_batch: ns_resolve", extra={
                        "namespace": item["namespace"], "id": ns_record.id,
                    })
                except Exception as e:
                    logger.exception("Failed to resolve namespace", extra={"namespace": item["namespace"]})
                    raise

            # Phase 4: Batch SQL insert
            if to_insert:
                user_ids = [user_id] * len(to_insert)
                contents = [item["content"] for item in to_insert]
                embeddings_list = [str(item["embedding"]) for item in to_insert]  # text[] -> ::vector
                metadatas_list = [item["metadata"] for item in to_insert]
                namespaces_list = [item["namespace"] for item in to_insert]
                content_hashes_list = [item["content_hash"] for item in to_insert]
                importances_list = [item["importance"] for item in to_insert]

                logger.info("memory_ingest_batch: phase4 insert_batch", extra={
                    "count": len(to_insert), "user_id": user_id,
                })

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
                    logger.info("memory_ingest_batch: insert_batch success", extra={"count": len(ids)})
                except Exception as e:
                    logger.exception("insert_batch FAILED", extra={"preview": str(e)[:500]})
                    raise

                for rid, item in zip(ids, to_insert):
                    summary["insert"] += 1
                    results.append({
                        "id": rid,
                        "action": "insert",
                        "namespace": item["namespace"],
                    })

        # Phase 5: Sync metadata.links → relations для всех обработанных гранул
        all_ids = [r["id"] for r in results if r["id"]]
        if all_ids:
            try:
                synced = await service.repository.sync_links_batch(all_ids)
                logger.info("memory_ingest_batch: phase5 sync_links", extra={
                    "synced": synced, "granules": len(all_ids),
                })
            except Exception as e:
                logger.exception("phase5: sync_links_batch FAILED (non-fatal)")

        logger.info("memory_ingest_batch: complete", extra={"summary": summary})
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
    logger.info("memory_stats", extra={"user_id": user_id})

    async def _stats_with_steps():
        async with _StepTimer("memory_stats/sql_stats"):
            result = await service.repository.get_stats(user_id)
        return result

    result = await _track_tool("memory_stats", _stats_with_steps())
    logger.info("memory_stats: done", extra={"namespaces": len(result)})
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
    logger.info("memory_find_similar: START", extra={
        "content_len": len(content), "namespace": namespace,
        "limit": limit, "threshold": threshold, "user_id": user_id,
    })

    async def _find_with_steps():
        async with _StepTimer("find_similar/step1/embed"):
            query_embedding = await service.embedding.embed(content)
        async with _StepTimer("find_similar/step2/sql_search"):
            results = await service.repository.search(
                query_embedding=query_embedding,
                user_id=user_id,
                limit=limit,
                threshold=threshold,
                namespace=namespace,
            )
        return results

    results = await _track_tool("memory_find_similar", _find_with_steps())
    logger.info("memory_find_similar: done", extra={"count": len(results)})
    return [r.model_dump(mode="json") for r in results]


@mcp.tool()
async def memory_get(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Retrieve a single memory record by its ID."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    logger.info("memory_get", extra={"id": id})

    async def _get_with_steps():
        async with _StepTimer("memory_get/sql_get"):
            record = await service.repository.get_by_id(id)
        if record is None:
            raise NotFoundError(id)
        return record

    try:
        record = await _track_tool("memory_get", _get_with_steps())
        logger.info("memory_get: found", extra={"id": record.id, "namespace": record.namespace})
        return record.model_dump(mode="json")
    except NotFoundError as e:
        logger.info("memory_get: not found", extra={"id": id})
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
    logger.info("memory_update: START", extra={
        "id": id, "has_content": content is not None,
        "has_metadata": metadata is not None, "importance": importance,
    })
    try:
        record = await _track_tool("memory_update", service.update(
            memory_id=id,
            content=content,
            metadata=metadata,
            importance=importance,
        ))
        logger.info("memory_update: done", extra={"id": record.id, "namespace": record.namespace})
        return record.model_dump(mode="json")
    except NotFoundError as e:
        logger.info("memory_update: not found", extra={"id": id})
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
    logger.info("memory_delete", extra={"id": id})
    try:
        success = await _track_tool("memory_delete", service.delete(memory_id=id))
        logger.info("memory_delete: done", extra={"id": id, "success": success})
        return {"success": success}
    except NotFoundError as e:
        logger.info("memory_delete: not found", extra={"id": id})
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
    logger.info("memory_list", extra={
        "namespace": namespace, "limit": limit,
        "offset": offset, "user_id": user_id,
    })

    async def _list_with_steps():
        async with _StepTimer("memory_list/sql_list"):
            result = await service.repository.list(
                user_id=user_id,
                namespace=namespace,
                limit=limit,
                offset=offset,
            )
        return result

    try:
        result = await _track_tool("memory_list", _list_with_steps())
        logger.info("memory_list: done", extra={"total": result.total, "items": len(result.items)})
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
    logger.info("memory_recent", extra={"namespace": namespace, "limit": limit, "since": str(since)})

    async def _recent_with_steps():
        async with _StepTimer("memory_recent/sql_recent"):
            results = await service.repository.recent(
                namespace=namespace,
                since=since,
                limit=limit,
            )
        return results

    try:
        results = await _track_tool("memory_recent", _recent_with_steps())
        logger.info("memory_recent: done", extra={"count": len(results)})
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
    logger.info("memory_forget", extra={"user_id": user_id, "namespace": namespace})
    try:
        deleted = await _track_tool("memory_forget", service.forget(user_id=user_id, namespace=namespace))
        logger.info("memory_forget: done", extra={"deleted_count": deleted})
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
    logger.info("memory_archive", extra={"id": id})
    try:
        success = await _track_tool("memory_archive", service.archive(memory_id=id))
        logger.info("memory_archive: done", extra={"id": id, "success": success})
        return {"success": success}
    except NotFoundError as e:
        logger.info("memory_archive: not found", extra={"id": id})
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
    logger.info("memory_link", extra={
        "source": source_id, "target": target_id,
        "type": link_type, "weight": weight,
    })
    try:
        rel_id = await _track_tool("memory_link", service.add_relation(
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
            description=description,
            weight=weight,
        ))
        logger.info("memory_link: done", extra={"relation_id": rel_id})
        return {"ok": True, "relation_id": rel_id}
    except NotFoundError as e:
        logger.info("memory_link: not found", extra={"error": str(e)})
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
    logger.info("memory_unlink", extra={
        "source": source_id, "target": target_id, "type": link_type,
    })
    try:
        deleted = await _track_tool("memory_unlink", service.delete_relation(
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
        ))
        logger.info("memory_unlink: done", extra={"success": deleted})
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
    logger.info("memory_get_relations", extra={"id": id, "link_type": link_type})

    async def _relations_with_steps():
        async with _StepTimer("get_relations/sql_outgoing"):
            outgoing = await service.repository.get_relations_by_source(id, link_type)
        async with _StepTimer("get_relations/sql_incoming"):
            incoming = await service.repository.get_relations_by_target(id, link_type)
        return {"incoming": incoming, "outgoing": outgoing}

    try:
        result = await _track_tool("memory_get_relations", _relations_with_steps())
        logger.info("memory_get_relations: done", extra={
            "incoming": len(result["incoming"]), "outgoing": len(result["outgoing"]),
        })
        return {
            "incoming": [r.model_dump(mode="json") for r in result["incoming"]],
            "outgoing": [r.model_dump(mode="json") for r in result["outgoing"]],
        }
    except NotFoundError as e:
        logger.info("memory_get_relations: not found", extra={"id": id})
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
    logger.info("memory_traverse", extra={
        "start_id": start_id, "depth": depth, "link_types": link_types,
    })

    async def _traverse_with_steps():
        async with _StepTimer("traverse/step1/validate"):
            start = await service.repository.get_by_id(start_id)
            if start is None:
                raise NotFoundError(f"Start granule: {start_id}")
        async with _StepTimer("traverse/step2/cte_walk"):
            nodes_raw = await service.repository.traverse(start_id, depth, link_types)
        async with _StepTimer("traverse/step3/load_nodes_and_edges"):
            nodes = []
            all_edges = []
            node_ids = {nd["node_id"] for nd in nodes_raw}
            for n in nodes_raw:
                record = await service.repository.get_by_id(n["node_id"])
                if record:
                    nodes.append({
                        "id": record.id,
                        "content": record.content[:200],
                        "namespace": record.namespace,
                        "depth": n["depth"],
                    })
                    edges = await service.repository.get_relations_by_source(
                        n["node_id"], link_type=None,
                    )
                    all_edges.extend(
                        e for e in edges if e.target_id and e.target_id in node_ids
                    )
        return {"nodes": nodes, "edges": all_edges}

    try:
        result = await _track_tool("memory_traverse", _traverse_with_steps())
        logger.info("memory_traverse: done", extra={
            "nodes": len(result["nodes"]), "edges": len(result["edges"]),
        })
        return {
            "nodes": result["nodes"],
            "edges": [e.model_dump(mode="json") for e in result["edges"]],
        }
    except NotFoundError as e:
        logger.info("memory_traverse: not found", extra={"start_id": start_id})
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
    logger.info("memory_graph_stats")

    async def _graph_stats_with_steps():
        async with _StepTimer("graph_stats/sql_stats"):
            result = await service.repository.get_graph_stats()
        return result

    try:
        stats = await _track_tool("memory_graph_stats", _graph_stats_with_steps())
        logger.info("memory_graph_stats: done", extra={
            "granules": stats.total_granules,
            "relations": stats.total_relations,
            "orphans": stats.orphans,
        })
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
    logger.info("memory_version", extra={
        "version": version, "server": settings.mcp_server_name,
        "model": settings.embedding_model,
    })
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
    logger.info("memory_namespaces")

    async def _namespaces_with_steps():
        async with _StepTimer("namespaces/sql_list"):
            namespaces = await service.ns_repo.list_all()
        return namespaces

    try:
        namespaces = await _track_tool("memory_namespaces", _namespaces_with_steps())
        logger.info("memory_namespaces: done", extra={"count": len(namespaces)})
        return [
            {"uid": ns.uid, "name": ns.name, "description": ns.description}
            for ns in namespaces
        ]
    except Exception as e:
        logger.exception("Failed to list namespaces")
        raise RuntimeError(str(e)) from e
