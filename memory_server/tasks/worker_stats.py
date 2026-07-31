"""Periodic сбор статистики Celery workers.

Обновляет метрики workers_active и queue_length.
Вызывается через Beat schedule каждые 30 секунд.
"""

import logging

from memory_server.celery_app import app
from memory_server.metrics import CELERY_WORKERS_ACTIVE, CELERY_QUEUE_LENGTH

logger = logging.getLogger(__name__)


@app.task(name="worker_stats.update")
def update_worker_stats():
    """Обновить метрики workers и queues."""
    try:
        inspect = app.control.inspect(timeout=5)

        # Количество активных workers
        active = inspect.active() or {}
        CELERY_WORKERS_ACTIVE.set(len(active))

        # Длина очередей (reserved tasks)
        reserved = inspect.reserved() or {}
        for queue, tasks in reserved.items():
            CELERY_QUEUE_LENGTH.labels(queue=queue).set(len(tasks))

        logger.info(
            "worker_stats: updated",
            extra={"workers": len(active), "queues": len(reserved)},
        )
    except Exception as e:
        logger.warning("worker_stats: failed", extra={"error": str(e)[:200]})