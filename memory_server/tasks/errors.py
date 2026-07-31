"""Custom exceptions for selti Celery tasks.

Hierarchy:
    SeltiTaskError (base)
        ├── SeltiConnectionError (retryable)
        ├── ValidationError (NOT retryable)
        ├── TaskTimeoutError (retryable)
        ├── MemoryTaskError
        └── HashTaskError
"""


class SeltiTaskError(Exception):
    """Base exception for all selti task errors."""
    pass


class SeltiConnectionError(SeltiTaskError):
    """Connection errors (DB, Qdrant, Redis). Retryable."""
    pass


class ValidationError(SeltiTaskError):
    """Validation errors. NOT retryable — fix input."""
    pass


class TaskTimeoutError(SeltiTaskError):
    """Task timeout. Retryable with backoff."""
    pass


class MemoryTaskError(SeltiTaskError):
    """Errors specific to memory operations."""
    pass


class HashTaskError(SeltiTaskError):
    """Errors specific to hash operations."""
    pass
