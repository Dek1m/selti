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
    metadata: dict | None = None,
    namespace: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Store a new memory record.

    Generates an embedding for the content and persists it to the database.
    Deduplication is applied automatically — returns existing record if a match is found.
    """
    _validate_namespace(namespace)
    assert ctx is not None
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
    user_id: str,
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
        results = []
        for entry in entries:
            _validate_namespace(entry.get("namespace"))
            record, action = await service.store(
                content=entry["content"],
                user_id=user_id,
                metadata=entry.get("metadata"),
                namespace=entry.get("namespace"),
            )
            results.append({
                "id": record.id,
                "action": action.value,
                "namespace": record.namespace,
            })

        summary: dict[str, int] = {"insert": 0, "skip": 0, "update": 0}
        for r in results:
            summary[r["action"]] += 1

        return {"results": results, "summary": summary}

    return await _track_tool("memory_ingest_batch", _run())


@mcp.tool()
async def memory_stats(
    user_id: str,
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
    user_id: str,
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
    metadata: dict | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Update an existing memory record.

    If content is provided, a new embedding is generated.
    """
    assert ctx is not None
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
