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
    "memory_server.tasks.hash_tasks.*": {"queue": "hash"},
    "memory_server.tasks.lifecycle_tasks.*": {"queue": "memory"},
    "memory_server.tasks.context_tasks.*": {"queue": "memory"},
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

# ── Time Limits (per task type) ──
# Определяются в @shared_task decorator, но дефолты здесь
app.conf.task_soft_time_limit = 240  # soft timeout (raises SoftTimeLimitExceeded)
app.conf.task_time_limit = 300  # hard timeout (kills worker)

# ── Retry Defaults ──
# Базовые настройки retry — переопределяются в @shared_task
app.conf.task_default_retry_delay = 30  # seconds
app.conf.task_max_retries = 5

# ── Result Settings ──
app.conf.result_expires = 3600  # 1 hour — результаты автоматически чистятся

# ── Beat Schedule: periodic worker stats + business metrics + memory lifecycle ──
# Жизненный цикл гранул (Фаза 2.2/2.3): кластеры → decay → stale ежедневно;
# GC и чистка сирот — еженедельно в воскресенье (низкая нагрузка, UTC).
from celery.schedules import crontab

app.conf.beat_schedule = {
    "update-worker-stats": {
        "task": "worker_stats.update",
        "schedule": 30.0,  # каждые 30 секунд
    },
    "update-business-metrics": {
        "task": "business_metrics.update",
        "schedule": 3600.0,  # раз в час
    },
    # Облачко знаний (Фаза 6.1): пересборка грязных снапшотов. Период = TTL
    # кеша ctx:{slug} (context_cache_ttl) — флаг живёт не дольше пересборки.
    "rebuild-contexts": {
        "task": "memory_server.tasks.lifecycle_tasks.rebuild_contexts",
        "schedule": 3600.0,  # раз в час
    },
    "refresh-clusters": {
        "task": "memory_server.tasks.lifecycle_tasks.refresh_clusters",
        "schedule": crontab(hour=2, minute=0),  # ежедневно 02:00 UTC
    },
    "confidence-decay": {
        "task": "memory_server.tasks.lifecycle_tasks.confidence_decay",
        "schedule": crontab(hour=3, minute=0),  # ежедневно 03:00 UTC
    },
    "mark-stale": {
        "task": "memory_server.tasks.lifecycle_tasks.mark_stale",
        "schedule": crontab(hour=4, minute=0),  # ежедневно 04:00 UTC
    },
    "gc-superseded": {
        "task": "memory_server.tasks.lifecycle_tasks.gc_superseded",
        "schedule": crontab(day_of_week="sun", hour=5, minute=0),  # воскр. 05:00 UTC
    },
    "orphans-cleanup": {
        "task": "memory_server.tasks.lifecycle_tasks.orphans_cleanup",
        "schedule": crontab(day_of_week="sun", hour=5, minute=30),  # воскр. 05:30 UTC
    },
}

# ── Worker Concurrency ──
app.conf.worker_concurrency = settings.celery_worker_concurrency

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
