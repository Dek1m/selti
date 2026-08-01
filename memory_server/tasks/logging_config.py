"""Structured JSON logging with structlog.

Format (JSON):
{
    "ts": "2026-08-01T12:34:56.789Z",
    "level": "INFO",
    "service": "selti",
    "msg": "memory_store: done",
    "request_id": "abc-123",
    "tool": "memory_store",
    "duration_ms": 234.5,
    "namespace": "code_knowledge"
}

Levels: DEBUG → INFO → WARN → ERROR
SERVICE_NAME: selti-worker (workers), selti (server)
Correlation: request_id via contextvars (argenta_logging.request_id_var)
"""

import logging
import os
import sys
from datetime import datetime, timezone

import structlog

SERVICE_NAME = os.environ.get("SERVICE_NAME", "selti-worker")

_LEVEL_MAP = {
    "WARNING": "WARN",
    "CRITICAL": "ERROR",
}

# Кэш для request_id_var (импортируем один раз)
_request_id_var = None
_request_id_var_loaded = False


def _add_service(logger, method_name, event_dict):
    """Inject service name into every log entry."""
    event_dict["service"] = SERVICE_NAME
    return event_dict


def _merge_request_id(logger, method_name, event_dict):
    """Inject correlation ID from argenta_logging.request_id_var."""
    global _request_id_var, _request_id_var_loaded
    if not _request_id_var_loaded:
        _request_id_var_loaded = True
        try:
            from argenta_logging import request_id_var
            _request_id_var = request_id_var
        except ImportError:
            _request_id_var = False
    if _request_id_var:
        rid = _request_id_var.get("")
        if rid:
            event_dict["request_id"] = rid
    return event_dict


def _map_level(logger, method_name, event_dict):
    """Map WARNING→WARN, CRITICAL→ERROR."""
    level = event_dict.get("level", "")
    event_dict["level"] = _LEVEL_MAP.get(level, level)
    return event_dict


def _add_timestamp(logger, method_name, event_dict):
    """Add ISO 8601 UTC timestamp with millisecond precision."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    event_dict["ts"] = ts
    return event_dict


def _rename_event_to_msg(logger, method_name, event_dict):
    """Rename structlog's 'event' key to 'msg' for target format."""
    if "event" in event_dict:
        event_dict["msg"] = event_dict.pop("event")
    return event_dict


# Shared processors — используются и в structlog, и в ProcessorFormatter
_shared_processors = [
    structlog.contextvars.merge_contextvars,
    _merge_request_id,
    structlog.stdlib.add_log_level,
    _map_level,
    _add_timestamp,
    _add_service,
]


def configure_structlog(service: str | None = None) -> None:
    """Configure structlog with JSON output via ProcessorFormatter.

    Handles both structlog and stdlib loggers uniformly:
    - structlog.get_logger().info("msg", extra={...}) — full JSON
    - logging.getLogger().info("msg", extra={...}) — full JSON (foreign_pre_chain)
    """
    global SERVICE_NAME
    if service:
        SERVICE_NAME = service

    structlog.configure(
        processors=_shared_processors + [
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _create_handler() -> logging.StreamHandler:
    """Create a handler with ProcessorFormatter for JSON output.

    LOG_FORMAT=json (default) → JSONRenderer
    LOG_FORMAT=console → ConsoleRenderer (dev mode)
    """
    log_format = os.environ.get("LOG_FORMAT", "json")

    if log_format == "console":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # Эти процессы работают ТОЛЬКО для foreign (stdlib) записей
            foreign_pre_chain=_shared_processors,
            # Эти процессы работают для ВСЕХ записей (structlog + foreign)
            processors=[
                structlog.stdlib.ExtraAdder(),
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _rename_event_to_msg,
                renderer,
            ],
        )
    )
    return handler


def setup_worker_logging(level: str = "INFO") -> None:
    """Configure root logger with structlog for Celery workers.

    Call this from worker startup:
        from memory_server.tasks.logging_config import setup_worker_logging
        setup_worker_logging()

    Args:
        level: Log level (DEBUG, INFO, WARN, ERROR)
    """
    configure_structlog()

    handler = _create_handler()
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Silence noisy libraries
    for name in ("celery", "kombu", "billiard", "amqp"):
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_server_logging(level: str = "INFO", service: str | None = None) -> None:
    """Configure root logger for the MCP server (non-worker).

    Args:
        level: Log level (DEBUG, INFO, WARN, ERROR)
        service: Service name override (default: from env SERVICE_NAME)
    """
    configure_structlog(service=service)

    handler = _create_handler()
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
