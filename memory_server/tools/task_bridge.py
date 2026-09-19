"""Bridge between MCP tools (async) and Celery tasks (sync).

Создаётся в контексте MCP сервера (async event loop),
отправляет задачи через Celery send_task и ждёт результат
в отдельном потоке через asyncio.to_thread().

Ожидание event-driven (Фаза 3.3): воркер после выполнения задачи
публикует событие в Redis-список (signals.on_task_postrun), мост
просыпается по BLPOP вместо опроса result.ready() каждые 100мс.
Страховка: если событие потеряно (воркер умер между результатом
и notify, Redis недоступен) — контроль result.ready() циклом
30-секундных чанков; поведение таймаута/ошибок как раньше.
"""

import asyncio
import logging
import time
from typing import Any

import redis as redis_sync
from celery import Celery
from celery.result import AsyncResult

from argenta_logging import request_id_var

from memory_server.config import settings
from memory_server.logger import get_logger

logger = get_logger(__name__)

# Таймаут ожидания результата задачи (5 минут)
TASK_RESULT_TIMEOUT = 300

# Ключ события завершения (общий с tasks/signals.py)
NOTIFY_KEY_PREFIX = "selti:bridge:done:"

# Максимальный чанк блокирующего ожидания: между чанками контроль
# result.ready() — страховка от потерянного notify-события
NOTIFY_CHUNK_SECONDS = 30.0

# Модульный sync-клиент notify-канала (потокобезопасен через пул redis-py);
# BLPOP блокирует тред executor'а, не event loop
_notify_client: redis_sync.Redis | None = None


def _get_notify_client() -> redis_sync.Redis:
    """Ленивый sync-клиент Redis для event-driven ожидания."""
    global _notify_client
    if _notify_client is None:
        # socket_timeout=None: BLPOP держит соединение дольше таймаута сокета
        _notify_client = redis_sync.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=None,
            socket_connect_timeout=2.0,
        )
    return _notify_client


def _blpop_notify(task_id: str, timeout: float) -> bool:
    """Блокирующее ожидание события завершения от воркера.

    True — событие получено; False — таймаут чанка или notify-канал
    недоступен (деградация к контролю ready, не ошибка: результат всё
    равно читается из Celery backend).
    """
    try:
        client = _get_notify_client()
        event = client.blpop(
            [f"{NOTIFY_KEY_PREFIX}{task_id}"], timeout=int(max(1.0, timeout))
        )
        return event is not None
    except redis_sync.RedisError as exc:
        logger.warning("task_bridge: notify channel unavailable", extra={
            "error": str(exc)[:200],
        })
        return False


def _wait_completion(result: AsyncResult, timeout: float, start: float) -> None:
    """Ждать готовности результата: событие Redis + страховка ready-контролем.

    Выход — по result.ready() (готовность/потерянный notify) или по
    таймауту (разбирает вызывающий).
    """
    deadline = start + timeout
    while not result.ready():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if remaining >= 1.0:
            # Блок до события или конца чанка — 0 CPU вместо poll 100мс
            _blpop_notify(result.id, min(NOTIFY_CHUNK_SECONDS, remaining))
        else:
            # Хвост < 1с: Redis BLPOP не принимает субсекундные таймауты
            time.sleep(remaining)


def run_task(
    app: Celery,
    task_name: str,
    timeout: float = TASK_RESULT_TIMEOUT,
    **kwargs: Any,
):
    """Отправить задачу в Celery и дождаться результата (sync context).

    Ожидание event-driven: BLPOP на ключ события завершения
    (см. signals.on_task_postrun), без polling result.ready().
    """
    start = time.monotonic()
    logger.debug("task_bridge: SEND", extra={
        "task_name": task_name, "timeout": timeout,
    })

    headers = {"bridge_wait": True}
    cid = request_id_var.get()
    if cid:
        headers["correlation_id"] = cid

    result: AsyncResult = app.send_task(task_name, kwargs=kwargs, headers=headers)

    try:
        _wait_completion(result, timeout, start)

        # Не дождались готовности — таймаут (порядок как в исходном мосте:
        # таймаут поднимается раньше разбора результата)
        if not result.ready():
            raise TimeoutError()

        # Check for task-level failure
        if result.failed():
            exc = result.result
            raise exc

        value = result.result
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.debug("task_bridge: OK", extra={
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
    Пробрасывает correlation_id из contextvar в thread worker.
    """
    from memory_server.celery_app import app

    loop = asyncio.get_running_loop()

    # Capture correlation_id BEFORE entering thread (contextvars don't propagate)
    current_rid = request_id_var.get("")

    def _run_in_thread():
        if current_rid:
            request_id_var.set(current_rid)
        return run_task(app, task_name, **kwargs)

    return await loop.run_in_executor(None, _run_in_thread)
