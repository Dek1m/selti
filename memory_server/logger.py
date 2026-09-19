"""Unified logging facade for selti.

Uses argenta-logging (Argenta Team standard).

Public API:
    get_logger(name)          — get a named logger
    measure_duration          — sync context manager for timing
    async_measure_duration    — async context manager for timing
    request_id_var            — correlation ID context var
"""

import logging
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from argenta_logging import get_logger, measure_duration, request_id_var


@asynccontextmanager
async def async_measure_duration(
    logger: logging.Logger,
    message: str = "Operation completed",
    level: int = logging.DEBUG,
    **extra: Any,
):
    """Async context manager for measuring operation duration.

    Default DEBUG (Фаза 3.3): единственный INFO-маркер операции —
    tool_handler в web-процессе; service-трассировка не дублирует его.

    Usage:
        async with async_measure_duration(logger, "store", namespace="code"):
            await do_something()
        # → {"message": "store: ok (42.3ms)", "duration_ms": 42.3, "namespace": "code"}
    """
    start = time.monotonic()
    try:
        yield
    finally:
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        if duration_ms > 500:
            logger.log(
                logging.WARNING,
                "%s: slow (%.1fms)",
                message,
                duration_ms,
                extra={"duration_ms": duration_ms, **extra},
            )
        else:
            logger.log(
                level,
                "%s: ok (%.1fms)",
                message,
                duration_ms,
                extra={"duration_ms": duration_ms, **extra},
            )


__all__ = [
    "get_logger",
    "measure_duration",
    "async_measure_duration",
    "request_id_var",
]
