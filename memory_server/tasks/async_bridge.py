"""Sync→async bridge for Celery workers.

Celery prefork workers — sync. Весь код selti — async.
Эта функция запускает async корутину в sync контексте.

Один event loop на весь lifetime worker process:
- asyncpg pool и httpx client привязаны к event loop
- При новом loop (new_event_loop) — старые ресурсы ломаются
- Persistent loop решает проблему: pool и client живут на одном loop
"""

import asyncio
import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Persistent event loop для worker process (per-process singleton)
_worker_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_worker_loop() -> asyncio.AbstractEventLoop:
    """Получить или создать persistent event loop для worker process.

    Loop живёт весь lifetime process и НЕ закрывается между задачами.
    Это позволяет asyncpg pool и httpx client переиспользоваться.
    """
    global _worker_loop
    if _worker_loop is not None and not _worker_loop.is_closed():
        return _worker_loop

    with _loop_lock:
        if _worker_loop is not None and not _worker_loop.is_closed():
            return _worker_loop
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
        logger.info("worker_event_loop: created")
        return _worker_loop


def run_async(coro_func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Запустить async функцию в sync контексте Celery worker.

    Использует persistent event loop — ресурсы (pool, httpx) живут на нём
    и переиспользуются между задачами.

    Пример использования в task::

        @app.task
        def store_memory(content: str, namespace: str):
            service = get_memory_service()
            return run_async(service.store, content=content, namespace=namespace)
    """
    loop = _get_worker_loop()
    return loop.run_until_complete(coro_func(*args, **kwargs))


def close_worker_loop() -> None:
    """Закрыть persistent event loop при shutdown worker process.

    Вызывается из connections.close_all() после закрытия всех ресурсов.
    """
    global _worker_loop
    if _worker_loop is not None and not _worker_loop.is_closed():
        _worker_loop.close()
        logger.info("worker_event_loop: closed")
    _worker_loop = None
