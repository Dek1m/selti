"""Argenta formatter for Celery worker logging.

Format: [ISO8601-UTC] [LEVEL] [SERVICE_NAME] message {"json": "meta"}

Levels: DEBUG → INFO → WARN (not WARNING!) → ERROR
SERVICE_NAME: selti-worker (for workers), selti (for server)
Correlation: task_id via structlog contextvars
"""

import json
import logging
import os
from datetime import datetime, timezone

SERVICE_NAME = os.environ.get("SERVICE_NAME", "selti-worker")

# Level mapping: Python's WARNING → Argenta's WARN
LEVEL_MAP = {
    "WARNING": "WARN",
    "CRITICAL": "ERROR",
}


class ArgentaFormatter(logging.Formatter):
    """Custom formatter: [ISO8601] [LEVEL] [SERVICE] message {json_meta}"""

    def format(self, record: logging.LogRecord) -> str:
        # Timestamp: ISO8601-UTC with milliseconds
        timestamp = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )

        # Level: map WARNING → WARN, CRITICAL → ERROR
        level = LEVEL_MAP.get(record.levelname, record.levelname)

        # Service name
        service = SERVICE_NAME

        # Message
        message = record.getMessage()

        # JSON metadata: only include known keys
        meta = {}
        for key in ("task_id", "task_name", "queue", "duration_ms", "error"):
            val = getattr(record, key, None)
            if val is not None:
                meta[key] = val

        meta_str = (" " + json.dumps(meta, default=str)) if meta else ""

        return f"[{timestamp}] [{level}] [{service}] {message}{meta_str}"


def setup_worker_logging(level: str = "INFO") -> None:
    """Configure root logger with ArgentaFormatter for Celery workers.

    Call this from worker startup:
        from memory_server.tasks.logging_config import setup_worker_logging
        setup_worker_logging()

    Args:
        level: Log level (DEBUG, INFO, WARN, ERROR)
    """
    handler = logging.StreamHandler()
    handler.setFormatter(ArgentaFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Silence noisy libraries
    logging.getLogger("celery").setLevel(logging.WARNING)
    logging.getLogger("kombu").setLevel(logging.WARNING)
    logging.getLogger("billiard").setLevel(logging.WARNING)
    logging.getLogger("amqp").setLevel(logging.WARNING)


def setup_server_logging(level: str = "INFO") -> None:
    """Configure root logger for the MCP server (non-worker)."""
    handler = logging.StreamHandler()
    formatter = ArgentaFormatter()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
