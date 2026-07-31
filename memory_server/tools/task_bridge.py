"""Bridge between MCP tools (async) and Celery tasks (sync).

Создаётся в контексте MCP сервера (async event loop),
отправляет задачи через Celery send_task и ждёт результат
в отдельном потоке через asyncio.to_thread().
"""

import asyncio
import logging
import time

from celery import Celery
from celery.result import AsyncResult

logger = logging.getLogger(__name__)

# Таймаут ожидания результата задачи (5 минут)
TASK_RESULT_TIMEOUT = 300


def run_task(
    app: Celery,
    task_name: str,
    timeout: float = TASK_RESULT_TIMEOUT,
    **kwargs,
):
    """Отправить задачу в Celery и дождаться результата (sync context).

    Вызывается из asyncio.to_thread() в MCP tools.
    """
    start = time.monotonic()
    logger.info("task_bridge: SEND", extra={
        "task_name": task_name, "timeout": timeout,
    })

    result: AsyncResult = app.send_task(task_name, kwargs=kwargs)

    try:
        value = result.get(timeout=timeout)
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.info("task_bridge: OK", extra={
            "task_name": task_name, "task_id": result.id,
            "duration_ms": elapsed_ms,
        })
        return value
    except Exception as e:
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.error("task_bridge: ERROR", extra={
            "task_name": task_name, "task_id": result.id,
            "duration_ms": elapsed_ms, "error": str(e)[:500],
        })
        raise


async def celery_call(task_name: str, **kwargs):
    """Async обёртка: отправить задачу в Celery и ждать результат.

    Используется в MCP tools вместо прямых вызовов MemoryService.
    """
    from memory_server.celery_app import app
    return await asyncio.to_thread(run_task, app, task_name, **kwargs)
