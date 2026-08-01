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

    Использует polling через result.ready() вместо result.get(),
    чтобы избежать конфликта async Redis backend с event loop.
    """
    start = time.monotonic()
    logger.info("task_bridge: SEND", extra={
        "task_name": task_name, "timeout": timeout,
    })

    result: AsyncResult = app.send_task(task_name, kwargs=kwargs)

    try:
        # Poll until ready or timeout
        while not result.ready():
            if time.monotonic() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)

        # Check for task-level failure
        if result.failed():
            exc = result.result
            raise exc

        value = result.result
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.info("task_bridge: OK", extra={
            "task_name": task_name, "task_id": result.id,
            "duration_ms": elapsed_ms,
        })
        return value
    except TimeoutError:
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.error("task_bridge: TIMEOUT", extra={
            "task_name": task_name, "task_id": result.id,
            "duration_ms": elapsed_ms, "timeout": timeout,
        })
        raise TimeoutError(f"Task {task_name} timed out after {timeout}s")
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
    Использует loop.run_in_executor для блокирующего result.get().
    """
    import functools
    from memory_server.celery_app import app

    loop = asyncio.get_running_loop()
    func = functools.partial(run_task, app, task_name, **kwargs)
    return await loop.run_in_executor(None, func)
