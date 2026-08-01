"""Общий декоратор для трекинга метрик MCP tools.

Заменяет дублированный try/except + logging + metrics во всех tools.
"""

import functools
import logging
import time

from memory_server.metrics import MCP_TOOL_CALLS_TOTAL, MCP_TOOL_DURATION_SECONDS

logger = logging.getLogger(__name__)


def tool_handler(tool_name: str):
    """Декоратор: timing, метрики, логирование, обёртка ошибок → RuntimeError.

    Использование:
        @tool_handler("memory_store")
        async def memory_store(...):
            return await celery_call(TASK_STORE, ...)
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                duration = time.monotonic() - start
                logger.info(f"{tool_name}: done", extra={"duration_ms": round(duration * 1000, 1)})
                MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="ok").inc()
                MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
                return result
            except Exception as e:
                duration = time.monotonic() - start
                logger.error(f"{tool_name}: error", extra={"error": str(e), "duration_ms": round(duration * 1000, 1)})
                MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="error").inc()
                MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
                raise RuntimeError(str(e)) from e
        return wrapper
    return decorator