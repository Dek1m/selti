"""Linker V3 tasks (ADR-019 C, фазы V3.2/V3.3) — очередь memory.

link_new_granule — асинхронный автолинкинг новой гранулы после store
(постановка — MemoryService.store через linker_dispatch, store НЕ дорожает).
name_reconciler — beat-кампания резолва висячих target_name (сухой прогон
по умолчанию: бой включается конфигом после ручной проверки отчёта).
co_occurrence — beat-кампания L1c для исторического корпуса.
l2_verdicts — beat-воркер очереди LLM-вердиктов.
linker_stats — данные memory_linker_stats (read-only).

Все кампании идемпотентны: повтор по обработанному состоянию — no-op.
"""

from typing import Any

from celery import shared_task

from memory_server.config import settings
from memory_server.logger import get_logger
from memory_server.state import get_state
from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask

logger = get_logger(__name__)

# Один WARN на процесс: L2 выключен (пустой linker_llm_base_url) — это
# сконфигурированная деградация, а не ошибка каждого прогона.
_llm_disabled_warned = False


def _get_linker():
    """Linker via process-wide SeltiState (composition root)."""
    return run_async(get_state().get_linker)


def _warn_l2_disabled_once() -> None:
    global _llm_disabled_warned
    if not _llm_disabled_warned:
        _llm_disabled_warned = True
        logger.warning(
            "linker: L2 verdicts disabled (linker_llm_base_url is empty); "
            "L1 layers work, orphans will be picked up by V3.4 orphan_linker"
        )


def enqueue_link(granule_id: str) -> None:
    """Диспетчер store → очередь линкера (best-effort, не блокирует запись).

    Вызывается MemoryService после INSERT: send_task с явной очередью
    (route по имени задачи есть в celery_app.task_routes, но exec-options
    надёжнее — паттерн документирован в celery_app.py).
    """
    if not settings.linker_enabled:
        return
    if not settings.linker_llm_base_url:
        _warn_l2_disabled_once()
    try:
        from memory_server.celery_app import app

        app.send_task(
            "memory_server.tasks.linker_tasks.link_new_granule",
            kwargs={"granule_id": granule_id},
            queue="memory",
            routing_key="memory",
        )
    except Exception as exc:
        # Диспетчеризация не роняет store: сироту подберёт beat-кампания
        logger.warning(
            "linker: enqueue failed (non-fatal)",
            extra={"granule_id": granule_id, "error": str(exc)},
        )


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.linker_tasks.link_new_granule",
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=120,
    time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def link_new_granule(self, granule_id: str) -> dict[str, Any]:
    """Автолинкинг новой гранулы: L1a ANN + L2-очередь + L1c co-occurrence."""
    linker = _get_linker()
    return run_async(linker.link_new_granule, granule_id)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.linker_tasks.name_reconciler",
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
def name_reconciler(self, dry_run: bool | None = None) -> dict[str, Any]:
    """Резолв висячих target_name батчами (приоритет: свой проект →
    глобальный → свежейшая asserted). dry_run=None берёт конфиг
    (по умолчанию True — первый прогон только отчёт)."""
    linker = _get_linker()
    return run_async(linker.run_name_reconciler, dry_run=dry_run)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.linker_tasks.co_occurrence",
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
def co_occurrence(self, batch: int | None = None) -> dict[str, Any]:
    """L1c для исторического корпуса: соседи той же сессии → related_to 0.5."""
    linker = _get_linker()
    return run_async(linker.run_co_occurrence, batch=batch)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.linker_tasks.l2_verdicts",
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
def l2_verdicts(self) -> dict[str, Any]:
    """Воркер L2: батч из Redis-очереди → LLM-вердикты → рёбра + кеш.

    LLM выключен ⇒ очередь НЕ разбирается (кандидаты не теряются в никуда:
    ждут включения / V3.4 orphan_linker)."""
    linker = _get_linker()
    if not linker.l2_enabled():
        _warn_l2_disabled_once()
    return run_async(linker.run_l2_verdicts)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.linker_tasks.linker_stats",
    soft_time_limit=60,
    time_limit=90,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def linker_stats(self) -> dict[str, Any]:
    """Данные memory_linker_stats (ADR-019 G). Read-only."""
    linker = _get_linker()
    return run_async(linker.stats)
