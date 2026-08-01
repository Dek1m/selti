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

logger = logging.getLogger(__name__)

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
app.conf.task_routes = {
    "memory_server.tasks.memory_tasks.*": {"queue": "memory"},
    "memory_server.tasks.hash_tasks.*": {"queue": "hash"},
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

# ── Beat Schedule: periodic worker stats + business metrics ──
app.conf.beat_schedule = {
    "update-worker-stats": {
        "task": "worker_stats.update",
        "schedule": 30.0,  # каждые 30 секунд
    },
    "update-business-metrics": {
        "task": "business_metrics.update",
        "schedule": 3600.0,  # раз в час
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

# Подключаем signals lifecycle для connection singletons (от Норы)
try:
    from memory_server.tasks.connections import setup_connection_signals
    setup_connection_signals(app)
    logger.info("Connection lifecycle signals connected")
except ImportError:
    logger.warning("Connection signals not available")

logger.info(
    "Celery app created",
    extra={
        "broker_url": settings.celery_broker_url,
        "result_backend": settings.celery_result_backend,
        "concurrency": settings.celery_worker_concurrency,
    },
)
