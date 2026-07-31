"""Hash tasks for Celery workers.

Wraps HashRepository operations using run_async.
Routed to the 'hash' queue.

Timeouts per plan v3:
- hash_ops: soft=120s, hard=180s
"""

import logging
from datetime import datetime
from typing import Any

from celery import shared_task

from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask
from memory_server.tasks.connections import get_pool
from memory_server.tasks.errors import HashTaskError, ValidationError

logger = logging.getLogger(__name__)


def _get_hash_repo():
    """Get HashRepository with worker-scoped pool."""
    from memory_server.memory.hash_repository import HashRepository

    pool = get_pool()
    return HashRepository(pool)


# ── Upsert ──────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.hash_tasks.upsert_hash",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=120,
    time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="hash",
    routing_key="hash",
)
def upsert_hash(
    self,
    source_type: str,
    source_id: str,
    content_hash: str,
    size_bytes: int | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Store or update a content hash for change detection."""
    if not source_type or not source_type.strip():
        raise ValidationError("source_type cannot be empty")
    if not source_id or not source_id.strip():
        raise ValidationError("source_id cannot be empty")
    if not content_hash or len(content_hash) != 64:
        raise ValidationError("content_hash must be 64 hex chars")

    repo = _get_hash_repo()
    result = run_async(
        repo.upsert,
        source_type=source_type,
        source_id=source_id,
        content_hash=content_hash,
        size_bytes=size_bytes,
        metadata=metadata,
    )
    return result


# ── Get ─────────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.hash_tasks.get_hash",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=120,
    time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="hash",
    routing_key="hash",
)
def get_hash(
    self,
    source_type: str,
    source_id: str,
) -> dict[str, Any] | None:
    """Get stored hash for a specific source."""
    if not source_type or not source_type.strip():
        raise ValidationError("source_type cannot be empty")
    if not source_id or not source_id.strip():
        raise ValidationError("source_id cannot be empty")

    repo = _get_hash_repo()
    return run_async(repo.get, source_type, source_id)


# ── Delete ──────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.hash_tasks.delete_hash",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=120,
    time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="hash",
    routing_key="hash",
)
def delete_hash(
    self,
    source_type: str,
    source_id: str,
) -> dict[str, Any]:
    """Delete a stored hash record."""
    if not source_type or not source_type.strip():
        raise ValidationError("source_type cannot be empty")
    if not source_id or not source_id.strip():
        raise ValidationError("source_id cannot be empty")

    repo = _get_hash_repo()
    deleted_id = run_async(repo.delete, source_type, source_id)
    return {"id": deleted_id, "success": deleted_id is not None}


# ── List ────────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.hash_tasks.list_hashes",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=120,
    time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="hash",
    routing_key="hash",
)
def list_hashes(
    self,
    source_type: str | None = None,
    updated_since: str | None = None,
    project: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List stored hashes with filters."""
    since = datetime.fromisoformat(updated_since) if updated_since else None
    limit = min(limit, 500)

    repo = _get_hash_repo()
    return run_async(
        repo.list,
        source_type=source_type,
        updated_since=since,
        project=project,
        limit=limit,
        offset=offset,
    )
