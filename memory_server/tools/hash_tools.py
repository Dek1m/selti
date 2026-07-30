import asyncio
import json
import logging
import re
import time
from typing import Any

from fastmcp import Context

from memory_server.metrics import MCP_TOOL_CALLS_TOTAL, MCP_TOOL_DURATION_SECONDS
from memory_server.server import mcp

logger = logging.getLogger(__name__)

# Валидация content_hash: SHA256 = 64 hex chars
HASH_REGEX = re.compile(r"^[a-f0-9]{64}$")

# Лимит metadata: 64KB
METADATA_MAX_SIZE = 65536

# Таймаут на каждый тул — 60 секунд
TOOL_TIMEOUT_SECONDS = 60

# ACL: authorized agents for write operations
WRITE_AUTHORIZED_AGENTS = {"memory-granulator", "akame", "admin"}


async def _track_tool(tool_name: str, coro, *, timeout: float | None = TOOL_TIMEOUT_SECONDS):
    """Замерить и записать метрики для MCP tool с таймаутом."""
    start = time.monotonic()
    logger.info("_track_tool: START tool=%s timeout=%s", tool_name, timeout)
    try:
        if timeout is not None:
            result = await asyncio.wait_for(coro, timeout=timeout)
        else:
            result = await coro
        duration = time.monotonic() - start
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="ok").inc()
        MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
        logger.info("_track_tool: DONE tool=%s duration=%.3fs", tool_name, duration)
        return result
    except asyncio.TimeoutError:
        duration = time.monotonic() - start
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="timeout").inc()
        MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
        logger.error("_track_tool: TIMEOUT tool=%s duration=%.3fs timeout=%.1fs",
                     tool_name, duration, timeout)
        raise TimeoutError(f"Tool '{tool_name}' timed out after {timeout}s") from None
    except Exception:
        duration = time.monotonic() - start
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="error").inc()
        MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
        logger.error("_track_tool: ERROR tool=%s duration=%.3fs", tool_name, duration)
        raise
    finally:
        duration = time.monotonic() - start
        logger.info("_track_tool: FINALLY tool=%s total_duration=%.3fs", tool_name, duration)


def _validate_hash(content_hash: str) -> None:
    """Валидация формата content_hash."""
    if not HASH_REGEX.match(content_hash):
        raise ValueError(f"Invalid content_hash format: expected 64 hex chars, got '{content_hash[:20]}...'")


def _validate_metadata_size(metadata: dict | None) -> None:
    """Валидация размера metadata (64KB limit)."""
    if metadata and len(json.dumps(metadata)) > METADATA_MAX_SIZE:
        raise ValueError(f"metadata too large: {len(json.dumps(metadata))} bytes (max {METADATA_MAX_SIZE})")


def _check_write_auth(ctx: Context | None) -> None:
    """Проверка ACL для write-операций."""
    if ctx and hasattr(ctx, "session") and ctx.session:
        client_info = getattr(ctx.session, "client_info", None)
        agent = getattr(client_info, "name", None) if client_info else None
        if agent and agent not in WRITE_AUTHORIZED_AGENTS:
            raise PermissionError(f"Agent '{agent}' not authorized for hash write operations")


@mcp.tool()
async def hash_upsert(
    source_type: str,
    source_id: str,
    content_hash: str,
    size_bytes: int | None = None,
    metadata: dict | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Store or update a content hash for change detection.
    Used by akame verifier to skip unchanged sessions/files."""
    logger.info("hash_upsert: source_type=%s source_id=%s", source_type, source_id)

    # ACL + валидация
    _check_write_auth(ctx)
    _validate_hash(content_hash)
    _validate_metadata_size(metadata)

    from memory_server.memory.hash_repository import HashRepository
    from memory_server.server import request_id_var

    service = ctx.request_context.lifespan_context["service"]
    repo = HashRepository(service.repository.pool)

    try:
        result = await _track_tool("hash_upsert", repo.upsert(
            source_type=source_type,
            source_id=source_id,
            content_hash=content_hash,
            size_bytes=size_bytes,
            metadata=metadata,
        ))
        logger.info("hash_upsert: done id=%s created_at=%s updated_at=%s",
                     result["id"], result["created_at"], result["updated_at"])
        return result
    except Exception as e:
        logger.exception("Failed to upsert hash")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def hash_get(
    source_type: str,
    source_id: str,
    ctx: Context | None = None,
) -> dict[str, Any] | None:
    """Get stored hash for a specific source."""
    logger.info("hash_get: source_type=%s source_id=%s", source_type, source_id)

    from memory_server.memory.hash_repository import HashRepository

    service = ctx.request_context.lifespan_context["service"]
    repo = HashRepository(service.repository.pool)

    try:
        result = await _track_tool("hash_get", repo.get(source_type, source_id))
        if result:
            logger.info("hash_get: found id=%s hash=%s", result["id"], result["content_hash"][:16])
        else:
            logger.info("hash_get: not found")
        return result
    except Exception as e:
        logger.exception("Failed to get hash")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def hash_list(
    source_type: str | None = None,
    updated_since: str | None = None,
    project: str | None = None,
    limit: int = 50,
    offset: int = 0,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """List stored hashes with filters."""
    logger.info("hash_list: source_type=%s updated_since=%s project=%s limit=%d",
                source_type, updated_since, project, limit)

    from datetime import datetime
    from memory_server.memory.hash_repository import HashRepository

    service = ctx.request_context.lifespan_context["service"]
    repo = HashRepository(service.repository.pool)

    # Ограничение limit
    limit = min(limit, 500)
    since = datetime.fromisoformat(updated_since) if updated_since else None

    try:
        results = await _track_tool("hash_list", repo.list(
            source_type=source_type,
            updated_since=since,
            project=project,
            limit=limit,
            offset=offset,
        ))
        logger.info("hash_list: done count=%d", len(results))
        return results
    except Exception as e:
        logger.exception("Failed to list hashes")
        raise RuntimeError(str(e)) from e


@mcp.tool()
async def hash_delete(
    source_type: str,
    source_id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete a stored hash record."""
    logger.warning("hash_delete: source_type=%s source_id=%s agent=%s",
                   source_type, source_id,
                   getattr(getattr(getattr(ctx, "session", None), "client_info", None), "name", "unknown") if ctx else "unknown")

    # ACL
    _check_write_auth(ctx)

    from memory_server.memory.hash_repository import HashRepository

    service = ctx.request_context.lifespan_context["service"]
    repo = HashRepository(service.repository.pool)

    try:
        deleted_id = await _track_tool("hash_delete", repo.delete(source_type, source_id))
        logger.warning("hash_delete: done deleted_id=%s", deleted_id)
        return {"id": deleted_id, "success": deleted_id is not None}
    except Exception as e:
        logger.exception("Failed to delete hash")
        raise RuntimeError(str(e)) from e
