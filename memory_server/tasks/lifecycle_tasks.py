"""Lifecycle tasks for Celery beat — жизнь памяти (Фазы 2.2/2.3/6.1 плана).

Ежечасно: rebuild_contexts (грязные снапшоты облачка). Ежедневно:
refresh_clusters (02:00) → confidence_decay (03:00) → mark_stale (04:00).
Еженедельно (воскресенье): gc_superseded (05:00) → orphans_cleanup (05:30).
Расписание — celery_app.beat_schedule; очередь — существующая memory
(lifetime-операции не конкурентят read-path'у тулов).

Все задачи идемпотентны: повтор по уже обработанному состоянию — no-op.
"""

import time
from typing import Any

from celery import shared_task

from memory_server.logger import get_logger
from memory_server.metrics import (
    EDGE_PRUNE_CANDIDATES_TOTAL,
    EDGE_PRUNE_DURATION_SECONDS,
    EDGE_PRUNED_TOTAL,
)
from memory_server.state import get_state
from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask
from memory_server.tasks.map_tasks import bump_map_dirty

logger = get_logger(__name__)


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


# ── Edge prune (ежедневно, 03:30 UTC; V3.5 «Жизнь графа знаний») ─


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.lifecycle_tasks.edge_prune",
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
def edge_prune(self, dry_run: bool | None = None) -> dict[str, Any]:
    """Отсечение ослабленных рёбер (ленивая w_eff, формулы Эны 22.09).

    Кандидат: живое резолвнутое ребро старше edge_prune_min_age_days, без
    иммунитета (ручные/l2/inherited/frozen-инцидентные) и не мост; raw
    w_eff ≤ edge_decay_floor. Пишется ТОЛЬКО pruned_at (не DELETE, не вес).
    dry_run=None берёт конфиг edge_prune_dry_run (дефолт True — только отчёт);
    мастер-выключатель edge_lifecycle_enabled=False — кампания пропускается.
    """
    service = _get_service()
    started = time.monotonic()
    report = run_async(service.edge_prune, dry_run=dry_run)
    # мастер-выключатель — кампания не выполнялась, метрик нет
    if "skipped" in report:
        return report
    mode = "dry" if report.get("dry_run") else "live"
    EDGE_PRUNE_DURATION_SECONDS.labels(mode=mode).observe(time.monotonic() - started)
    EDGE_PRUNE_CANDIDATES_TOTAL.labels(mode=mode).inc(
        int(report.get("candidates", 0))
    )
    if not report.get("dry_run", True):
        EDGE_PRUNED_TOTAL.labels(mode="live").inc(int(report.get("pruned", 0)))
    return report


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
    """GC закрытых версий старше gc_retention_days — под стоп-краном V3.1.

    Дефолт (gc_purge_enabled=False / gc_mode='disabled') — только счётчик
    кандидатов: полная история версий сохраняется всегда (ADR-019 F).
    Удаление возможна лишь в gc_mode='hard' при gc_purge_enabled=True;
    только superseded С наследником — окно валидности живёт в цепочке.
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


# ── Rebuild contexts (ежечасно; Фаза 6.1 — «облачко знаний») ─────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.lifecycle_tasks.rebuild_contexts",
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
def rebuild_contexts(self) -> dict[str, Any]:
    """Пересборка снапшотов проектов с dirty-флагом ctx:{slug}:dirty.

    dirty ставят store/update/create_version/retract с project_id;
    beat снимает их почасовым пересчётом (только грязные, не весь реестр).
    """
    service = _get_service()
    return run_async(service.rebuild_dirty_contexts)


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
    """Пересчёт кластеров Level 2: Qdrant ANN → пары → assign_clusters_from_pairs (022 v2).

    Кандидатов ищет Qdrant (пачки 256 гранул, batch query_points), группирует
    хранимка по залитым парам — триграммный SQL-поиск v1 удалён (квадратично
    деградировал на проде: 620 с на project_meta). Без аргумента — по всем
    namespace реестра; первый же ok=False (миграция pending или
    qdrant_unavailable) останавливает обход — retry подхватит целиком.
    """
    service = _get_service()
    if namespace is not None:
        result = run_async(service.refresh_clusters, namespace=namespace)
        if result.get("ok") is not False:
            bump_map_dirty()
        return result

    namespaces = run_async(service.ns_repo.list_all)
    summary: dict[str, Any] = {"ok": True, "namespaces": {}}
    for ns in namespaces:
        result = run_async(service.refresh_clusters, namespace=ns.uid)
        summary["namespaces"][ns.uid] = result
        if result.get("ok") is False:
            # 022 не применена или Qdrant недоступен — по одному ns не шумим,
            # выходим сразу (retry подхватит весь обход)
            return result
    # Кластерный состав в снапшоте меняется БЕЗ следов в version-хэше
    # (разметка cluster_id не трогает memories.updated_at — триггер 022) →
    # единственный инвалидатор — dirty-bump (PLAN_FULL_MAP_3D M2)
    bump_map_dirty()
    return summary
