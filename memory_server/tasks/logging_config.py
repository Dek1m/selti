"""Unified logging configuration for selti.

Uses argenta-logging (Argenta Team standard).
Formats: json (default), posix (dev via LOG_FORMAT=posix)

Correlation ID: request_id_var from argenta_logging
Levels: DEBUG → INFO → WARN → ERROR

Public API:
    setup_worker_logging(level)  — for Celery workers
    setup_server_logging(level, service)  — for FastMCP server
    LOGGING_CONFIG  — dict for uvicorn.run(log_config=...)
"""

import logging
import os
import sys

from argenta_logging import setup_logging, PosixFormatter, JsonFormatter, RequestContextFilter

SERVICE_NAME = os.environ.get("SERVICE_NAME", "selti-worker")


def _create_formatter(service: str):
    """Create formatter based on LOG_FORMAT env var."""
    fmt = os.environ.get("LOG_FORMAT", "json")
    if fmt == "posix":
        return PosixFormatter(service=service)
    return JsonFormatter(service=service)


def setup_worker_logging(level: str = "INFO") -> None:
    """Configure root logger for Celery workers.

    Call from worker_process_init signal:
        from memory_server.tasks.logging_config import setup_worker_logging
        setup_worker_logging()
    """
    setup_logging(level=level, fmt=os.environ.get("LOG_FORMAT", "json"))

    # Silence noisy Celery internals
    for name in ("celery", "kombu", "billiard", "amqp", "celery.app.trace"):
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_server_logging(level: str = "INFO", service: str | None = None) -> None:
    """Configure root logger for FastMCP server.

    Called at module import in server.py.
    """
    svc = service or os.environ.get("SERVICE_NAME", "selti")
    setup_logging(service=svc, level=level, fmt=os.environ.get("LOG_FORMAT", "json"))

    # Redirect uvicorn loggers through our formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_create_formatter(svc))
    handler.addFilter(RequestContextFilter())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.addHandler(handler)
        uv_logger.propagate = False


# ── Uvicorn dictConfig ──
# Minimal config for uvicorn.run(log_config=...).
# Only configures uvicorn's own loggers — root logger is set up by setup_server_logging().
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "argenta": {
            "()": "memory_server.tasks.logging_config._UvicornFormatter",
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


class _UvicornFormatter(logging.Formatter):
    """Lazy formatter — resolves LOG_FORMAT at first use."""

    _inner = None

    @classmethod
    def _get_inner(cls):
        if cls._inner is None:
            svc = os.environ.get("SERVICE_NAME", "selti")
            fmt = os.environ.get("LOG_FORMAT", "json")
            cls._inner = PosixFormatter(service=svc) if fmt == "posix" else JsonFormatter(service=svc)
        return cls._inner

    def format(self, record):
        return self._get_inner().format(record)
