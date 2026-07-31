"""Sync→async bridge for Celery workers.

Celery prefork workers — sync. Весь код selti — async.
Эта функция запускает async корутину в sync контексте.

Используем new_event_loop() вместо get_event_loop():
- В sync контексте Celery worker нет running event loop
- get_event_loop() может вернуть закрытый loop с предыдущими state
- new_event_loop() — чистый loop для каждой задачи
"""

import asyncio
from typing import Any, Callable


def run_async(coro_func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Запустить async функцию в sync контексте Celery worker.

    Пример использования в task::

        @app.task
        def store_memory(content: str, namespace: str):
            service = get_memory_service()
            return run_async(service.store, content=content, namespace=namespace)
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_func(*args, **kwargs))
    finally:
        loop.close()
