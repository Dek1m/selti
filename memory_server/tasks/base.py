"""Base task class for selti Celery workers.

Provides common settings: retry, timeout, logging, metrics.
All selti tasks inherit from SeltiTask.
"""

import logging
from typing import Any

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded

from memory_server.tasks.errors import (
    SeltiConnectionError,
    SeltiTaskError,
    TaskTimeoutError,
    ValidationError,
)

logger = logging.getLogger(__name__)


class SeltiTask(Task):
    """Base Celery task with production settings.

    - Retry: exponential backoff + jitter, max 5 retries
    - Timeouts: configurable per task type
    - Logging: structured via structlog
    - Metrics: via Celery signals (signals.py)

    Usage:
        @app.task(base=SeltiTask, soft_time_limit=240, time_limit=300)
        def my_task(self, arg):
            ...
    """

    # Retry settings
    max_retries = 5
    retry_backoff = True
    retry_backoff_max = 60
    retry_jitter = True
    default_retry_delay = 30

    # Queue settings
    acks_late = True
    reject_on_worker_lost = True

    # Task type — override in subclass or via decorator
    task_type: str = "memory"

    def on_failure(
        self, exc: Exception, task_id: str, args: tuple, kwargs: dict, einfo: Any
    ) -> None:
        """Log failure and emit metrics."""
        exc_type = type(exc).__name__
        logger.error(
            "task: FAILED",
            extra={
                "task_id": task_id,
                "task_name": self.name,
                "exception_type": exc_type,
                "error": str(exc)[:500],
            },
        )

    def on_success(self, retval: Any, task_id: str, args: tuple, kwargs: dict) -> None:
        """Log success."""
        logger.info(
            "task: SUCCESS",
            extra={
                "task_id": task_id,
                "task_name": self.name,
            },
        )

    def on_retry(self, exc: Exception, task_id: str, args: tuple, kwargs: dict) -> None:
        """Log retry."""
        logger.warning(
            "task: RETRY",
            extra={
                "task_id": task_id,
                "task_name": self.name,
                "retries": self.request.retries,
                "max_retries": self.max_retries,
                "error": str(exc)[:200],
            },
        )

    def retry_with_backoff(
        self,
        exc: Exception,
        exc_class: type[Exception] | None = None,
    ) -> None:
        """Retry with exponential backoff.

        Args:
            exc: Exception that triggered retry
            exc_class: Exception class to raise (default: re-raise)

        Raises:
            self.retry() with the exception
        """
        if exc_class is None:
            exc_class = type(exc)

        # Only retry on retryable errors
        retryable = (
            SeltiConnectionError,
            TaskTimeoutError,
            OSError,
            TimeoutError,
        )
        if not isinstance(exc, retryable):
            logger.warning(
                "task: NON_RETRYABLE_ERROR",
                extra={
                    "task_id": self.request.id,
                    "task_name": self.name,
                    "exception_type": type(exc).__name__,
                },
            )
            raise exc from None

        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error(
                "task: MAX_RETRIES_EXCEEDED",
                extra={
                    "task_id": self.request.id,
                    "task_name": self.name,
                    "retries": self.request.retries,
                },
            )
            raise exc from None

    @staticmethod
    def handle_timeout(exc: Exception, task_name: str, task_id: str) -> None:
        """Handle timeout exceptions consistently."""
        if isinstance(exc, SoftTimeLimitExceeded):
            logger.error(
                "task: SOFT_TIMEOUT",
                extra={"task_id": task_id, "task_name": task_name},
            )
            raise TaskTimeoutError(f"Soft time limit exceeded for {task_name}") from exc
        elif isinstance(exc, TimeLimitExceeded):
            logger.error(
                "task: HARD_TIMEOUT",
                extra={"task_id": task_id, "task_name": task_name},
            )
            raise TaskTimeoutError(f"Hard time limit exceeded for {task_name}") from exc

    @staticmethod
    def validate_args(**kwargs: Any) -> None:
        """Validate task arguments. Raises ValidationError on invalid input."""
        for key, value in kwargs.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                raise ValidationError(f"Empty string for {key}")
