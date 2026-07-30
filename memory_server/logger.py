"""Единый модуль логирования Argenta Team.

Формат: [ISO8601-UTC] [LEVEL] [service-name] message {"key": "value"}
POSIX-совместим: grep, jq, awk работают без проблем.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

SERVICE_NAME = os.environ.get("SERVICE_NAME", "selti")

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}

# Стандартные атрибуты LogRecord — их НЕ включаем в JSON-мету
_LOG_RECORD_BUILTIN_ATTRS = {
    "name", "msg", "args", "created", "relativeCreated", "thread",
    "threadName", "process", "processName", "module", "funcName",
    "lineno", "filename", "pathname", "levelname", "levelno",
    "exc_info", "exc_text", "stack_info", "message", "taskName",
    "levelname", "msecs", "levelname", "levelname",
}


class PosixFormatter(logging.Formatter):
    """Формат: [ISO8601] [LEVEL] [service] message {"key": "value"}."""

    def __init__(self, service: str | None = None):
        super().__init__()
        self.service = service or SERVICE_NAME

    # Маппинг Python levelname → стандарт Argenta
    _LEVEL_MAP = {
        "WARNING": "WARN",
        "INFO": "INFO",
        "DEBUG": "DEBUG",
        "ERROR": "ERROR",
        "CRITICAL": "ERROR",
    }

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        level = self._LEVEL_MAP.get(record.levelname, record.levelname)
        service = getattr(record, "service", self.service)
        message = record.getMessage()

        # JSON-мета: все extra-поля, кроме встроенных атрибутов LogRecord
        meta = {}
        for key, val in record.__dict__.items():
            if key not in _LOG_RECORD_BUILTIN_ATTRS and key not in ("service",):
                if not key.startswith("_"):
                    meta[key] = val

        meta_str = (" " + json.dumps(meta, default=str)) if meta else ""

        if record.exc_info and record.exc_info[0]:
            exc_text = self.formatException(record.exc_info)
            return f"[{timestamp}] [{level}] [{service}] {message}{meta_str}\n{exc_text}"

        return f"[{timestamp}] [{level}] [{service}] {message}{meta_str}"


def setup_logging(
    level: str | None = None,
    service: str | None = None,
) -> None:
    """Настроить глобальный логгер с POSIX-форматом.

    Args:
        level: Уровень логирования (DEBUG/INFO/WARN/ERROR). Из env LOG_LEVEL.
        service: Имя сервиса в логах. Из env SERVICE_NAME.
    """
    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    log_level = LOG_LEVELS.get(level_name, logging.INFO)

    svc = service or SERVICE_NAME
    formatter = PosixFormatter(service=svc)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Получить именованный логгер.

    Используй: logger = get_logger(__name__)
    """
    return logging.getLogger(name)
