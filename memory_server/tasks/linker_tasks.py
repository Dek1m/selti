"""Linker V3 tasks (ADR-019 C, фазы V3.2/V3.3) — очередь memory.

link_new_granule — асинхронный автолинкинг новой гранулы после store
(постановка — MemoryService.store через linker_dispatch, store НЕ дорожает).
name_reconciler — beat-кампания резолва висячих target_name (сухой прогон
по умолчанию: бой включается конфигом после ручной проверки отчёта).
co_occurrence — beat-кампания L1c для исторического корпуса (Фаза 3:
косинус-гейт + маркер l1c_done).
prune_cooccurrence_history — one-off кампания Фазы 3 (ручной celery-call):
ретроспективный гейт исторических l1c, dry_run по умолчанию.
l2_verdicts — beat-воркер очереди LLM-вердиктов (в manual-режиме очередь
не трогает — её разбирает человек-агент тулами review/verdict).
linker_review / linker_manual_verdict — ручной разбор очереди L2
(manual mode, приказ Мастера 20.09).
linker_stats — данные memory_linker_stats (read-only).

Все кампании идемпотентны: повтор по обработанному состоянию — no-op.
"""

from typing import Any

from celery import shared_task

from memory_server.logger import get_logger
from memory_server.state import get_state
from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask
from memory_server.tasks.map_tasks import bump_map_dirty

logger = get_logger(__name__)

# Один WARN на процесс: режим L2 без LLM — это сконфигурированная
# деградация (manual или off), а не ошибка каждого прогона.
_l2_mode_warned = False


def _get_linker():
    """Linker via process-wide SeltiState (composition root)."""
    return run_async(get_state().get_linker)


def _warn_l2_no_llm_once() -> None:
    global _l2_mode_warned
    if _l2_mode_warned:
        return
    _l2_mode_warned = True
    if get_state().get_runtime_config_sync().get("linker_l2_manual"):
        logger.warning(
            "linker: L2 in MANUAL mode (linker_llm_base_url is empty) — "
            "queue fills for memory_linker_review/memory_linker_verdict "
            "(memory-granulator)"
        )
    else:
        logger.warning(
            "linker: L2 verdicts disabled (linker_llm_base_url is empty, "
            "linker_l2_manual=false); L1 layers work, orphans will be "
            "picked up by V3.4 orphan_linker"
        )


def enqueue_link(granule_id: str) -> None:
    """Диспетчер store → очередь линкера (best-effort, не блокирует запись).

    Вызывается MemoryService после INSERT: send_task с явной очередью
    (route по имени задачи есть в celery_app.task_routes, но exec-options
    надёжнее — паттерн документирован в celery_app.py).
    """
    runtime = get_state().get_runtime_config_sync()
    if not runtime.get("linker_enabled"):
        return
    if not runtime.get("linker_llm_base_url"):
        _warn_l2_no_llm_once()
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
    result = run_async(linker.run_name_reconciler, dry_run=dry_run)
    # Бой-резолв меняет рёбра карты: version-хэш ловит новые target_id,
    # но переписи существующих — нет → снос кешей карты (PLAN_FULL_MAP_3D M2)
    if not result.get("dry_run") and result.get("resolved", 0):
        bump_map_dirty()
    return result


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
    """L1c для исторического корпуса: соседи той же сессии → related_to 0.5
    сквозь косинус-гейт; обработанные гранулы помечаются l1c_done."""
    linker = _get_linker()
    return run_async(linker.run_co_occurrence, batch=batch)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.linker_tasks.prune_cooccurrence_history",
    max_retries=0,  # one-off ручной запуск: ретрай спрячет от человека отказ
    soft_time_limit=1800,
    time_limit=2400,
    queue="memory",
    routing_key="memory",
)
def prune_cooccurrence_history(
    self, dry_run: bool = True, batch: int | None = None
) -> dict[str, Any]:
    """One-off кампания Фазы 3: ретроспективный косинус-гейт исторических
    l1c-рёбер — непрошедшие получают pruned_at (НЕ DELETE); мосты между
    кластерами иммунны. dry_run=True (дефолт) — только отчёт (выживет/
    погибнет, распределение по кластерам, топ примеров); бой — явным
    celery-call с dry_run=False. Идемпотентна: повтор по прогнанному — no-op."""
    linker = _get_linker()
    return run_async(
        linker.run_prune_cooccurrence_history, dry_run=dry_run, batch=batch
    )


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

    Без живого LLM очередь НЕ разбирается: manual-режим — её разбирает
    человек-агент (memory_linker_review/verdict); выключенный L2 —
    кандидаты ждут включения / V3.4 orphan_linker."""
    linker = _get_linker()
    if not linker.l2_enabled():
        _warn_l2_no_llm_once()
    return run_async(linker.run_l2_verdicts)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.linker_tasks.linker_review",
    soft_time_limit=60,
    time_limit=90,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def linker_review(self) -> dict[str, Any]:
    """Peek старейшего элемента очереди L2 (manual mode). Read-only по очереди."""
    linker = _get_linker()
    return run_async(linker.peek_l2)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.linker_tasks.linker_manual_verdict",
    soft_time_limit=60,
    time_limit=90,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def linker_manual_verdict(
    self,
    source_id: str,
    candidate_id: str,
    verdict: str,
    link_type: str | None = None,
    confidence: float = 0.9,
) -> dict[str, Any]:
    """Ручной вердикт Тиши по паре из очереди L2 (manual mode).

    Те же правила, что у LLM-пути: duplicate → WARN без supersede,
    CNLM-невалидный link_type → related_to. Разобранная пара извлекается
    из очереди, вердикт фиксируется в verdict-cache."""
    linker = _get_linker()
    return run_async(
        linker.apply_manual_verdict,
        source_id=source_id,
        candidate_id=candidate_id,
        verdict=verdict,
        link_type=link_type,
        confidence=confidence,
    )


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
