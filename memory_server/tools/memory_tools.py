import logging
from typing import Any

from fastmcp import Context

from memory_server.exceptions import NotFoundError
from memory_server.server import mcp

logger = logging.getLogger(__name__)


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
    """
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        record = await service.store(
            content=content,
            user_id=user_id,
            metadata=metadata,
            namespace=namespace,
        )
        return record.model_dump(mode="json")
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
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        results = await service.search(
            query=query,
            user_id=user_id,
            limit=limit,
            threshold=threshold,
            namespace=namespace,
        )
        return [r.model_dump(mode="json") for r in results]
    except Exception as e:
        logger.exception("Failed to search memories")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def memory_get(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Retrieve a single memory record by its ID."""
    assert ctx is not None
    service = ctx.request_context.lifespan_context["service"]
    try:
        record = await service.get(memory_id=id)
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
        record = await service.update(
            memory_id=id,
            content=content,
            metadata=metadata,
        )
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
        success = await service.delete(memory_id=id)
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
        result = await service.list(
            user_id=user_id,
            namespace=namespace,
            limit=limit,
            offset=offset,
        )
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
        deleted = await service.forget(user_id=user_id, namespace=namespace)
        return {"deleted_count": deleted}
    except Exception as e:
        logger.exception("Failed to forget memories")
        raise RuntimeError(str(e)) from e
