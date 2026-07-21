class MemoryError(Exception):
    """Base exception for memory server errors."""
    pass


class NotFoundError(MemoryError):
    """Raised when a memory record is not found."""

    def __init__(self, memory_id: str, message: str | None = None):
        self.id = memory_id
        super().__init__(message or f"Memory record not found: {memory_id}")


class EmbeddingError(MemoryError):
    """Raised when embedding API request fails."""

    def __init__(self, status_code: int, detail: str, message: str | None = None):
        self.status_code = status_code
        self.detail = detail
        super().__init__(message or f"Embedding API error {status_code}: {detail}")


class DatabaseError(MemoryError):
    """Wrapper for database-related errors."""
    pass
