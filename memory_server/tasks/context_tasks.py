"""Context tasks for Celery workers («облачко знаний», D9).

Обёртки MemoryService.get_project_context / rebuild_project_context.
Routed to the 'memory' queue. Beat-расписание rebuild и Redis-кеш — Фаза 6.
"""

import logging
from typing import Any

from celery import shared_task

from memory_server.state import get_state
from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask
from memory_server.tasks.errors import ValidationError

logger = logging.getLogger(__name__)


def _get_service():
    """Get MemoryService via process-wide SeltiState (composition root)."""
    return run_async(get_state().get_memory_service)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.context_tasks.get_project_context",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def get_project_context(
    self,
    project: str,
    refresh: bool = False,
) -> dict[str, Any]:
    """Snapshot контекста проекта (fast-path по таблице project_contexts)."""
    if not project or not project.strip():
        raise ValidationError("project cannot be empty")

    service = _get_service()
    context = run_async(service.get_project_context, project=project, refresh=refresh)
    return context.model_dump(mode="json")


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.context_tasks.rebuild_project_context",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def rebuild_project_context(self, project: str) -> dict[str, Any]:
    """Принудительный пересчёт снапшота из топ-гранул проекта."""
    if not project or not project.strip():
        raise ValidationError("project cannot be empty")

    service = _get_service()
    context = run_async(service.rebuild_project_context, project=project)
    return context.model_dump(mode="json")
