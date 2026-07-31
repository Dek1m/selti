"""Общий декоратор для трекинга метрик MCP tools.

Заменяет дублированный _track_tool в memory_tools.py и hash_tools.py.
"""

import functools
import logging
import time

from memory_server.metrics import MCP_TOOL_CALLS_TOTAL, MCP_TOOL_DURATION_SECONDS

logger = logging.getLogger(__name__)


def track_tool_metrics(tool_name: str):
    """Декоратор для трекинга метрик MCP tools.

    Использование:
        @track_tool_metrics("memory_store")
        async def memory_store(...):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            logger.info("tool: START", extra={"tool": tool_name})
            try:
                result = await func(*args, **kwargs)
                duration = time.monotonic() - start
                MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="ok").inc()
                MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
                logger.info("tool: DONE", extra={"tool": tool_name, "duration_ms": round(duration * 1000, 1)})
                return result
            except Exception:
                duration = time.monotonic() - start
                MCP_TOOL_CALLS_TOTAL.labels(tool=tool_name, status="error").inc()
                MCP_TOOL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
                logger.error("tool: ERROR", extra={"tool": tool_name, "duration_ms": round(duration * 1000, 1)})
                raise
        return wrapper
    return decorator