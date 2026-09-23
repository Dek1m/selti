"""Celery application for selti.

Создаёт Celery instance с production-ready настройками:
- Queues: memory, batch, hash
- Routing: memory tasks → memory queue, batch tasks → batch queue, hash tasks → hash queue
- Production: task_acks_late, graceful shutdown, memory limits
- Serialization: JSON
- Retry: exponential backoff + jitter (настраивается per-task)

Подключение:
    celery -A memory_server.celery_app worker -l INFO
    celery -A memory_server.celery_app flower
"""

import logging
from time import monotonic

from celery import Celery
from kombu import Exchange, Queue

from memory_server.config import settings
from memory_server.logger import get_logger

logger = get_logger(__name__)

# ── Create Celery instance ──
app = Celery(settings.mcp_server_name)

# ── Broker & Backend ──
app.conf.broker_url = settings.celery_broker_url
app.conf.result_backend = settings.celery_result_backend

# ── Serialization ──
app.conf.task_serializer = settings.celery_task_serializer
app.conf.result_serializer = settings.celery_result_serializer
app.conf.accept_content = settings.celery_accept_content

# ── Timezone ──
app.conf.timezone = settings.celery_timezone

# ── Queues ──
# Определяем exchange и queues для маршрутизации задач
default_exchange = Exchange("default", type="direct")
memory_exchange = Exchange("memory", type="direct")
batch_exchange = Exchange("batch", type="direct")
hash_exchange = Exchange("hash", type="direct")

app.conf.task_queues = (
    Queue("default", default_exchange, routing_key="default"),
    Queue("memory", memory_exchange, routing_key="memory"),
    Queue("batch", batch_exchange, routing_key="batch"),
    Queue("hash", hash_exchange, routing_key="hash"),
)

app.conf.task_default_queue = "default"
app.conf.task_default_exchange = "default"
app.conf.task_default_routing_key = "default"

# ── Routing ──
# Memory tasks → memory queue
# Batch tasks → batch queue
# Hash tasks → hash queue
# Lifecycle tasks (Фаза 2.2: decay/stale/GC/clusters) → memory queue
# Beat-задачи: beat шлёт send_task() без exec-options декоратора —
# без явного route падают в default, которую воркер не слушает (-Q memory,batch,hash).
# Явный queue= в декораторе (apply_async) приоритетнее route (lpmerge: options > route),
# поэтому ingest_batch (queue='batch') route memory_tasks.* не перебивает.
app.conf.task_routes = {
    "memory_server.tasks.memory_tasks.*": {"queue": "memory"},
    "memory_server.tasks.project_tasks.*": {"queue": "memory"},
    "memory_server.tasks.hash_tasks.*": {"queue": "hash"},
    "memory_server.tasks.lifecycle_tasks.*": {"queue": "memory"},
    "memory_server.tasks.context_tasks.*": {"queue": "memory"},
    "memory_server.tasks.linker_tasks.*": {"queue": "memory"},
    "memory_server.tasks.map_tasks.*": {"queue": "memory"},
    "worker_stats.update": {"queue": "memory"},
    "business_metrics.update": {"queue": "memory"},
}

# ── Production Worker Settings ──
# task_acks_late: ACK после выполнения, а не перед (безопасность при crash)
app.conf.task_acks_late = True

# task_reject_on_worker_lost: re-queue при потере worker (autorecovery)
app.conf.task_reject_on_worker_lost = True

# worker_prefetch_multiplier: fairness — worker берёт по 1 задаче за раз
app.conf.worker_prefetch_multiplier = settings.celery_worker_prefetch_multiplier

# worker_max_tasks_per_child: recycling workers для защиты от memory leaks
app.conf.worker_max_tasks_per_child = settings.celery_worker_max_tasks_per_child

# worker_max_memory_per_child: OOM protection (200MB по умолчанию)
app.conf.worker_max_memory_per_child = settings.celery_worker_max_memory_per_child

# worker_soft_shutdown_timeout: graceful shutdown — завершаем текущие задачи
app.conf.worker_soft_shutdown_timeout = 60

# ── Time Limits / Retry / Result (§2.9: дефолты в config.py, единый
# источник с сидингом 027; актуальные значения применяет celeryd_init) ──
app.conf.task_soft_time_limit = settings.task_soft_time_limit
app.conf.task_time_limit = settings.task_time_limit
app.conf.task_default_retry_delay = settings.task_default_retry_delay
app.conf.task_max_retries = settings.task_max_retries
app.conf.result_expires = settings.result_expires

# ── Beat Schedule (Ф2): расписание — runtime-ключи schedule.* (реестр §2.10) ──
# Модульный уровень строит расписание ИЗ ДЕФОЛТОВ реестра (ноль IO, бит-в-бит
# прежние литералы) — fallback, работающий без БД и без кастомного Scheduler.
# Актуальные значения (env > БД > дефолт) применяет RuntimeScheduler: при
# старте beat и перечитыванием каждые 30 с на тике — расписание меняется
# налету, без рестарта контейнера (паттерн django-celery-beat, приказ Ф2).
from celery.beat import Scheduler
from celery.schedules import crontab

from memory_server.runtime_config import load_effective_values_sync
from memory_server.settings_store import SCHEDULE_KEYS, get_default

# schedule.* ключ → (имя beat-записи, задача); порядок = порядок реестра §2.10
SCHEDULE_TASKS: dict[str, tuple[str, str]] = {
    "schedule.update_worker_stats": ("update-worker-stats", "worker_stats.update"),
    "schedule.update_business_metrics": ("update-business-metrics", "business_metrics.update"),
    "schedule.rebuild_contexts": ("rebuild-contexts", "memory_server.tasks.lifecycle_tasks.rebuild_contexts"),
    "schedule.refresh_clusters": ("refresh-clusters", "memory_server.tasks.lifecycle_tasks.refresh_clusters"),
    "schedule.layout_map": ("layout-map", "memory_server.tasks.map_tasks.galactic_layout"),
    "schedule.confidence_decay": ("confidence-decay", "memory_server.tasks.lifecycle_tasks.confidence_decay"),
    "schedule.edge_prune": ("edge-prune", "memory_server.tasks.lifecycle_tasks.edge_prune"),
    "schedule.mark_stale": ("mark-stale", "memory_server.tasks.lifecycle_tasks.mark_stale"),
    "schedule.gc_superseded": ("gc-superseded", "memory_server.tasks.lifecycle_tasks.gc_superseded"),
    "schedule.orphans_cleanup": ("orphans-cleanup", "memory_server.tasks.lifecycle_tasks.orphans_cleanup"),
    "schedule.linker_name_reconciler": ("linker-name-reconciler", "memory_server.tasks.linker_tasks.name_reconciler"),
    "schedule.linker_co_occurrence": ("linker-co-occurrence", "memory_server.tasks.linker_tasks.co_occurrence"),
    "schedule.linker_l2_verdicts": ("linker-l2-verdicts", "memory_server.tasks.linker_tasks.l2_verdicts"),
}


def _to_celery_schedule(key: str, raw: dict) -> float | crontab:
    """JSON §2.10 → объект расписания celery. Битое значение → дефолт + WARN."""
    try:
        if raw.get("type") == "interval":
            return float(raw["seconds"])
        return crontab(
            minute=raw.get("minute", "*"),
            hour=raw.get("hour", "*"),
            day_of_week=raw.get("day_of_week") or "*",
            day_of_month=raw.get("day_of_month") or "*",
            month_of_year=raw.get("month_of_year") or "*",
        )
    except Exception as exc:
        logger.warning("beat: invalid schedule value, using default", extra={"key": key, "error": str(exc)[:200]})
        return _to_celery_schedule(key, get_default(key))


def build_beat_schedule(values: dict) -> dict[str, dict]:
    """schedule.* значения → формат beat_schedule celery."""
    schedule: dict[str, dict] = {}
    for key, (entry_name, task) in SCHEDULE_TASKS.items():
        raw = values.get(key, get_default(key))
        schedule[entry_name] = {"task": task, "schedule": _to_celery_schedule(key, raw)}
    return schedule


def read_schedule_values() -> dict:
    """Текущие effective-значения schedule-ключей (sync, короткий timeout)."""
    return load_effective_values_sync(set(SCHEDULE_KEYS))


class RuntimeScheduler(Scheduler):
    """Beat-планировщик с перечитыванием расписания из RuntimeConfig.

    Каждые sync_every секунд (в тике) — read_schedule_values(); при
    изменении полная пересборка entries: новые записи стартуют с now
    (due через свой период), удалённые исчезают. БД недоступна →
    последние значения сохраняются (sync-путь не бросает).
    """

    sync_every = 30.0
    _current_raw: dict | None = None

    def setup_schedule(self) -> None:
        self._current_raw = None
        self.sync()

    def sync(self) -> None:
        raw = read_schedule_values()
        if raw == self._current_raw:
            return
        schedule = build_beat_schedule(raw)
        # Полная замена: app.conf + пересборка entries (merge только добавляет)
        self.app.conf.beat_schedule = schedule
        self.data = {}
        self.merge_inplace(schedule)
        self._current_raw = raw
        logger.info(
            "beat: schedule (re)loaded from runtime config",
            extra={"entries": len(schedule), "changed": True},
        )

    def tick(self, *args: object, **kwargs: object) -> float:
        if self.should_sync():
            self.sync()
            self.last_sync = monotonic()
        # celery 5.6: Scheduler.tick(event_t=event_t, min=min, heappop=...,
        # heappush=...) — все параметры это локальные замыкания heapq,
        # позиционного event_timeout у родителя НЕТ. Прошлая передача
        # event_timeout первым аргументом подменяла event_t на None →
        # "'NoneType' object is not callable" на каждом due-тике beat
        # (прод-инцидент 23.09). Проксируем аргументы прозрачно, без подмены.
        return super().tick(*args, **kwargs)


# Fallback-расписание из дефолтов реестра (без IO) — актуализируется
# RuntimeScheduler'ом при старте beat
app.conf.beat_schedule = build_beat_schedule({key: get_default(key) for key in SCHEDULE_TASKS})
app.conf.beat_scheduler = "memory_server.celery_app.RuntimeScheduler"

# ── Worker Concurrency ──
# Стартовое значение; актуальное применяется celeryd_init из runtime-слоя
# (env > БД > дефолт), налету — pool_grow/pool_shrink из PUT /api/settings
app.conf.worker_concurrency = settings.celery_worker_concurrency

# ── Ф2: стартовый bootstrap группы celery из БД (requires_restart-ключи) ──
# celeryd_init стреляет в MAIN-процессе до создания пула — conf успевает
# примениться. Sync-путь без event loop (отдельное соединение, не пул
# SeltiState: пул привязан к loop воркера). БД недоступна → env/дефолты.
_CELERY_CONF_KEYS = (
    "celery_worker_concurrency",
    "celery_worker_prefetch_multiplier",
    "celery_worker_max_tasks_per_child",
    "celery_worker_max_memory_per_child",
    "task_soft_time_limit",
    "task_time_limit",
    "task_default_retry_delay",
    "task_max_retries",
    "result_expires",
)
_CELERY_CONF_ATTRS = {
    "celery_worker_concurrency": "worker_concurrency",
    "celery_worker_prefetch_multiplier": "worker_prefetch_multiplier",
    "celery_worker_max_tasks_per_child": "worker_max_tasks_per_child",
    "celery_worker_max_memory_per_child": "worker_max_memory_per_child",
    "task_soft_time_limit": "task_soft_time_limit",
    "task_time_limit": "task_time_limit",
    "task_default_retry_delay": "task_default_retry_delay",
    "task_max_retries": "task_max_retries",
    "result_expires": "result_expires",
}


def _bootstrap_worker_config() -> None:
    """Применить runtime-значения группы celery к app.conf при старте воркера."""
    values = load_effective_values_sync(set(_CELERY_CONF_KEYS))
    for key, attr in _CELERY_CONF_ATTRS.items():
        setattr(app.conf, attr, values[key])
    logger.info(
        "celeryd_init: worker config from runtime layer",
        extra={"concurrency": app.conf.worker_concurrency},
    )


try:
    from celery.signals import celeryd_init

    @celeryd_init.connect(weak=False)
    def on_celeryd_init(**kwargs):
        _bootstrap_worker_config()

    logger.info("celeryd_init bootstrap connected (runtime config)")
except ImportError:
    logger.warning("celeryd_init signal not available")

# ── Worker Logging ──
# Отключаем дефолтный root logger Celery, чтобы setup_worker_logging()
# в worker_process_init signal оставался единственным handler.
# Без этого Celery добавляет свой StreamHandler после signal и затирает ArgentaFormatter.
app.conf.worker_hijack_root_logger = False

# ── Discover Tasks ──
# Автоматически находит tasks в пакете memory_server.tasks
app.autodiscover_tasks(["memory_server.tasks"])

# ── Setup Signals ──
# Подключаем signals для метрик (от Мая)
try:
    from memory_server.tasks.signals import setup_signals
    setup_signals(app)
    logger.info("Celery signals connected")
except ImportError:
    logger.warning("Celery signals not available")

# Подключаем lifecycle воркера: прогрев/aclose SeltiState (composition root)
try:
    from memory_server.state import setup_worker_signals
    setup_worker_signals(app)
    logger.info("Worker lifecycle signals connected (SeltiState)")
except ImportError:
    logger.warning("Worker lifecycle signals not available")

# ── Beat Logging ──
# beat-процесс не проходит worker_process_init (нет fork), а перехват
# setup_logging (state.py) отключает конфигурацию логов Celery — без этого
# хендлера root logger в beat остаётся без handlers и его логи теряются.
# beat_init стреляет в celery/beat.py после setup_logging, до главного цикла.
try:
    from celery.signals import beat_init

    @beat_init.connect(weak=False)
    def on_beat_init(**kwargs):
        from memory_server.tasks.logging_config import setup_worker_logging

        setup_worker_logging()
        # Отправка задач расписания ('Scheduler: Sending due task') — INFO:
        # не глушим вместе с остальным celery.* → WARNING
        logging.getLogger("celery.beat").setLevel(logging.INFO)
        logger.info(
            "beat: schedule started",
            extra={"schedule_entries": len(app.conf.beat_schedule)},
        )

except ImportError:
    logger.warning("beat_init signal not available")

logger.info(
    "Celery app created",
    extra={
        "broker_url": settings.celery_broker_url,
        "result_backend": settings.celery_result_backend,
        "concurrency": settings.celery_worker_concurrency,
    },
)
