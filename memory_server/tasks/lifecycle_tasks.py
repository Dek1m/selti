"""Lifecycle tasks for Celery beat — жизнь памяти (Фаза 2.2/2.3 плана).

Ежедневно: refresh_clusters (02:00) → confidence_decay (03:00) →
mark_stale (04:00). Еженедельно (воскресенье): gc_superseded (05:00) →
orphans_cleanup (05:30). Расписание — celery_app.beat_schedule; очередь —
существующая memory (lifetime-операции не конкурентят read-path'у тулов).

Все задачи идемпотентны: повтор по уже обработанному состоянию — no-op.
"""

import logging
from typing import Any

from celery import shared_task

from memory_server.state import get_state
from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask

logger = logging.getLogger(__name__)


def _get_service():
    """Get MemoryService via process-wide SeltiState (composition root)."""
    return run_async(get_state().get_memory_service)


# ── Confidence decay (ежедневно, 03:00 UTC) ─────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.lifecycle_tasks.confidence_decay",
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
def confidence_decay(self) -> dict[str, Any]:
    """Затухание уверенности: батч-SQL per-namespace, frozen не трогаем.

    rates — config.recency_decay_rates; ниже confidence_decay_floor
    не сползаем. Метрика: счёт затронутых по namespace.
    """
    service = _get_service()
    touched = run_async(service.decay_confidence)
    return {"touched": touched, "total": sum(touched.values())}


# ── Mark stale (ежедневно, 04:00 UTC) ───────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.lifecycle_tasks.mark_stale",
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
def mark_stale(self) -> dict[str, Any]:
    """Счётчик устаревших кандидатов + warning-лог. Статус НЕ меняем —
    ревизия ручная (memory_stale_list показывает кандидатов).
    """
    service = _get_service()
    count = run_async(service.mark_stale)
    return {"stale_candidates": count}


# ── GC superseded (еженедельно, воскресенье 05:00 UTC) ──────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.lifecycle_tasks.gc_superseded",
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
def gc_superseded(self) -> dict[str, Any]:
    """Hard delete закрытых версий старше gc_retention_days (dry-run по конфигу).

    Только superseded С наследником: окно валидности живёт в цепочке
    (valid_to унаследованной записи), история не теряется.
    """
    service = _get_service()
    result = run_async(service.gc_superseded)
    return result


# ── Orphans cleanup (еженедельно, воскресенье 05:30 UTC) ────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.lifecycle_tasks.orphans_cleanup",
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
def orphans_cleanup(self) -> dict[str, Any]:
    """Связи без адреса целиком (target_id и target_name оба NULL).

    Кластеры member_count=0 — TODO(022): таблицы clusters ещё нет,
    placeholder до применения миграции Норы.
    """
    service = _get_service()
    removed = run_async(service.orphans_cleanup)
    return {"orphan_relations_removed": removed}


# ── Refresh clusters (ежедневно, 02:00 UTC; Фаза 2.3) ───────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.lifecycle_tasks.refresh_clusters",
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
def refresh_clusters(self, namespace: str | None = None) -> dict[str, Any]:
    """Пересчёт кластеров Level 2 хранимкой assign_clusters (миграция 022).

    Без аргумента — по всем namespace реестра. До применения 022 каждый
    вызов деградирует в ok=False (beat не ломается), после — штатно.
    """
    service = _get_service()
    if namespace is not None:
        return run_async(service.refresh_clusters, namespace=namespace)

    namespaces = run_async(service.ns_repo.list_all)
    summary: dict[str, Any] = {"ok": True, "namespaces": {}}
    for ns in namespaces:
        result = run_async(service.refresh_clusters, namespace=ns.uid)
        summary["namespaces"][ns.uid] = result
        if result.get("ok") is False:
            # Хранимки нет — миграция 022 ещё не применена: не шумим на каждый ns
            return result
    return summary
