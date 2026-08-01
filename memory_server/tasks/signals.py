"""Celery signals → Prometheus metrics.

Подключается в celery_app.py:
    from memory_server.tasks.signals import setup_signals
    setup_signals(app)

Сигналы:
- task_prerun: начало задачи (запоминаем start time)
- task_postrun: конец задачи (считаем duration + latency)
- task_failure: ошибка задачи
- task_retry: повторная попытка

Метрики обновляются в <PREFIX>_celery_* (memory_server/metrics.py).
"""

import time
import logging

from celery.signals import task_prerun, task_postrun, task_failure, task_retry

from memory_server.metrics import (
    CELERY_TASKS_TOTAL,
    CELERY_TASK_DURATION_SECONDS,
    CELERY_TASK_LATENCY_SECONDS,
    CELERY_TASK_RETRIES_TOTAL,
    CELERY_TASK_TIMEOUTS_TOTAL,
    CELERY_TASK_ERRORS_TOTAL,
)

logger = logging.getLogger(__name__)

# Thread-local для хранения start time по task_id
# В prefork worker каждый процесс — свой event loop, thread-local безопасен
_task_start_times: dict[str, float] = {}
_task_send_times: dict[str, float] = {}


def setup_signals(app):
    """Подключить все сигналы к Celery app."""

    @task_prerun.connect(weak=False)
    def on_task_prerun(sender, task_id, task, args, kwargs, **estkw):
        """Задача начала выполняться."""
        now = time.monotonic()
        _task_start_times[task_id] = now

        # Пытаемся достать send_time из kwargs (передаётся через task_bridge)
        send_time = (kwargs or {}).get("_send_time")
        if send_time:
            _task_send_times[task_id] = send_time

        logger.info("task: PRERUN", extra={
            "task_id": task_id,
            "task_name": task.name,
        })

    @task_postrun.connect(weak=False)
    def on_task_postrun(sender, task_id, task, retval, state, **kw):
        """Задача завершилась (успех или ошибка)."""
        now = time.monotonic()
        task_name = task.name or "unknown"

        # Duration
        start = _task_start_times.pop(task_id, now)
        duration = now - start
        CELERY_TASK_DURATION_SECONDS.labels(task=task_name).observe(duration)

        # Latency (send → start)
        send_time = _task_send_times.pop(task_id, None)
        if send_time:
            latency = start - send_time
            CELERY_TASK_LATENCY_SECONDS.labels(task=task_name).observe(latency)

        # Статус
        if state == "SUCCESS":
            CELERY_TASKS_TOTAL.labels(task=task_name, status="success").inc()
            logger.info("task: SUCCESS", extra={
                "task_id": task_id,
                "task_name": task_name,
                "duration_ms": round(duration * 1000, 1),
            })
        elif state == "FAILURE":
            CELERY_TASKS_TOTAL.labels(task=task_name, status="failure").inc()
            logger.error("task: FAILURE", extra={
                "task_id": task_id,
                "task_name": task_name,
                "duration_ms": round(duration * 1000, 1),
            })

    @task_failure.connect(weak=False)
    def on_task_failure(sender, task_id, exception, traceback, **kw):
        """Задача упала с исключением."""
        task_name = sender.name or "unknown"
        exception_type = type(exception).__name__

        CELERY_TASK_ERRORS_TOTAL.labels(
            task=task_name,
            exception_type=exception_type,
        ).inc()

        # Таймаут — отдельный счётчик
        if exception_type in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
            CELERY_TASK_TIMEOUTS_TOTAL.labels(task=task_name).inc()

        logger.error("task: FAILURE_DETAIL", extra={
            "task_id": task_id,
            "task_name": task_name,
            "exception_type": exception_type,
            "error": str(exception)[:500],
        })

    @task_retry.connect(weak=False)
    def on_task_retry(sender, request, reason, **kw):
        """Задача повторяется."""
        task_name = sender.name or "unknown"
        CELERY_TASK_RETRIES_TOTAL.labels(task=task_name).inc()

        logger.warning("task: RETRY", extra={
            "task_id": request.id,
            "task_name": task_name,
            "reason": str(reason)[:200],
        })

    logger.info("Celery signals connected")
