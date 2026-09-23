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


class ConflictError(MemoryError):
    """Raised when an operation conflicts with the granule lifecycle state.

    Фаза 2: нельзя суперседить не-asserted гранулу, нельзя создавать
    новую версию с идентичным контентом (unique-индекс дедупа).
    """

    def __init__(self, granule_id: str, reason: str):
        self.id = granule_id
        super().__init__(f"Lifecycle conflict for {granule_id}: {reason}")


class SchemaPendingError(MemoryError):
    """Required DB objects are not migrated yet (graceful degradation).

    Фаза 2.3: кластеризация кодится под миграцию 022 (Нора); до её
    применения на проде SQL кластеров деградирует в понятный ответ,
    а не в 500.
    """
    pass


class VectorStoreError(MemoryError):
    """Векторный бэкенд (Qdrant) недоступен или ответил ошибкой.

    Фаза 2.3 v2: кластеризация берёт кандидатов из Qdrant ANN. При отказе
    векторного бэкенда пары собрать нельзя — пересчёт обязан честно
    отказаться (сервис вернёт ok=False qdrant_unavailable), а не заливать
    пустые пары и стирать существующую разметку кластеров.
    """

    def __init__(self, message: str):
        super().__init__(message)


class JevServiceError(MemoryError):
    """Decision-сервис Open-Jev-2B (System One) недоступен или ответил ошибкой.

    Слой рефлексов (приказ Мастера 23.09): тулы jev_* обязаны падать
    этой ошибкой наружу — молчаливый дефолт решений запрещён, агент
    должен видеть, что рефлекс-слой отказал, и решать сам.
    """

    def __init__(self, message: str):
        super().__init__(message)
