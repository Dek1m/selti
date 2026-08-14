"""Backward-compatible re-export.

All logging configuration moved to tasks/logging_config.py.
This file exists only for imports like:
    from memory_server.logging_config import LOGGING_CONFIG
"""

import os


class _LazyFormatter:
    """Lazy formatter — resolves LOG_FORMAT at first use."""

    _inner = None

    @classmethod
    def _get_inner(cls):
        if cls._inner is None:
            from argenta_logging import PosixFormatter, JsonFormatter
            svc = os.environ.get("SERVICE_NAME", "selti")
            fmt = os.environ.get("LOG_FORMAT", "json")
            cls._inner = PosixFormatter(service=svc) if fmt == "posix" else JsonFormatter(service=svc)
        return cls._inner

    def format(self, record):
        return self._get_inner().format(record)


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "argenta": {
            "()": "memory_server.logging_config._LazyFormatter",
        },
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "argenta",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
    },
}
