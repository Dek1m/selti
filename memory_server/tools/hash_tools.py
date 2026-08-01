"""MCP tools for hash operations.

All tools delegate to Celery tasks via celery_call().
ACL checks and validation remain at tool level.
"""

import json
import re
from typing import Any

from fastmcp import Context

from memory_server.server import mcp
from memory_server.tools.task_bridge import celery_call
from memory_server.utils.metrics_decorator import tool_handler

# Валидация content_hash: SHA256 = 64 hex chars
HASH_REGEX = re.compile(r"^[a-f0-9]{64}$")

# Лимит metadata: 64KB
METADATA_MAX_SIZE = 65536

# ACL: authorized agents for write operations
WRITE_AUTHORIZED_AGENTS = {"memory-granulator", "akame", "admin"}

# Имена задач
TASK_UPSERT_HASH = "memory_server.tasks.hash_tasks.upsert_hash"
TASK_GET_HASH = "memory_server.tasks.hash_tasks.get_hash"
TASK_LIST_HASHES = "memory_server.tasks.hash_tasks.list_hashes"
TASK_DELETE_HASH = "memory_server.tasks.hash_tasks.delete_hash"


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
@tool_handler("hash_upsert")
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
    _check_write_auth(ctx)
    _validate_hash(content_hash)
    _validate_metadata_size(metadata)
    return await celery_call(
        TASK_UPSERT_HASH,
        source_type=source_type,
        source_id=source_id,
        content_hash=content_hash,
        size_bytes=size_bytes,
        metadata=metadata,
    )


@mcp.tool()
@tool_handler("hash_get")
async def hash_get(
    source_type: str,
    source_id: str,
    ctx: Context | None = None,
) -> dict[str, Any] | None:
    """Get stored hash for a specific source."""
    return await celery_call(
        TASK_GET_HASH,
        source_type=source_type,
        source_id=source_id,
    )


@mcp.tool()
@tool_handler("hash_list")
async def hash_list(
    source_type: str | None = None,
    updated_since: str | None = None,
    project: str | None = None,
    limit: int = 50,
    offset: int = 0,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """List stored hashes with filters."""
    return await celery_call(
        TASK_LIST_HASHES,
        source_type=source_type,
        updated_since=updated_since,
        project=project,
        limit=min(limit, 500),
        offset=offset,
    )


@mcp.tool()
@tool_handler("hash_delete")
async def hash_delete(
    source_type: str,
    source_id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete a stored hash record."""
    _check_write_auth(ctx)
    return await celery_call(
        TASK_DELETE_HASH,
        source_type=source_type,
        source_id=source_id,
    )
