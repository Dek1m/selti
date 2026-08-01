"""Единый модуль логирования Argenta Team.

Формат: JSON {"ts": "...", "level": "...", "service": "...", "msg": "...", ...}
Для dev режима: LOG_FORMAT=console
Для production: LOG_FORMAT=json (default)
"""

import structlog

from argenta_logging import measure_duration
from memory_server.tasks.logging_config import (
    configure_structlog,
    setup_server_logging,
)

__all__ = ["setup_logging", "get_logger", "measure_duration"]


def get_logger(name: str = "") -> structlog.stdlib.BoundLogger:
    """Получить именованный логгер.

    Использование: logger = get_logger(__name__)
    """
    return structlog.get_logger(name)


def setup_logging(
    service: str | None = None,
    level: str | None = None,
    **kwargs,
) -> None:
    """Настроить глобальный логгер.

    Обёртка над setup_server_logging для обратной совместимости.
    """
    setup_server_logging(level=level or "INFO", service=service)
