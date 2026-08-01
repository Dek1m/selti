"""Protocol-based interfaces for DI and testing.

Используй typing.Protocol для structural subtyping:
  - Не нарушает Liskov Substitution Principle
  - Позволяет мокать отдельные методы без наследования
  - Проверяется статическим анализатором (mypy, pyright)

Пример использования:
    from memory_server.memory.interfaces import MemoryRepositoryProtocol

    def process(repo: MemoryRepositoryProtocol) -> None:
        # repo может быть MemoryRepository, MockMemoryRepository, и т.д.
        record = await repo.get_by_id("123")
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    MemoryStatsItem,
    Relation,
    RelationListResult,
    SearchResult,
)


class EmbeddingProviderProtocol(Protocol):
    """Контракт для embedding клиентов.

    Поддерживает embed, embed_many, aclose.
    EmbeddingClient реализует этот протокол.
    """

    async def embed(self, text: str) -> list[float]:
        """Встроить один текст в векторное пространство."""
        ...

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Batch-встраивание текстов."""
        ...

    async def aclose(self) -> None:
        """Закрыть HTTP-клиент и освободить ресурсы."""
        ...


class MemoryRepositoryProtocol(Protocol):
    """Контракт для хранилища гранул памяти.

    Объединяет PostgreSQL (метаданные) + Qdrant (вектора).
    Методы разделены на: INSERT, SEARCH, UPDATE, DELETE, READ, RELATIONS, GRAPH.
    """

    # ── INSERT ──

    async def insert(
        self,
        user_id: str,
        content: str,
        embedding: list[float] | None = None,
        metadata: dict | None = None,
        namespace: str = "default",
        namespace_id: str | None = None,
        content_hash: str | None = None,
        importance: int = 3,
    ) -> str:
        """Создать новую запись. Возвращает ID."""
        ...

    async def insert_batch(
        self,
        user_ids: list[str],
        contents: list[str],
        namespaces: list[str],
        namespace_ids: list[str],
        content_hashes: list[str | None],
        embeddings: list[list[float]] | list[str] | None = None,
        metadatas: list[dict] | None = None,
        importances: list[int] | None = None,
    ) -> list[str]:
        """Batch insert. Возвращает список ID."""
        ...

    # ── SEARCH ──

    async def search(
        self,
        query_embedding: list[float],
        user_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
        namespace: str | None = None,
        query_text: str | None = None,
    ) -> list[SearchResult]:
        """Векторный поиск по embedding. Если Qdrant недоступен — SQL FTS fallback."""
        ...

    # ── UPDATE ──

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        embedding: list[float] | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
    ) -> MemoryRecord | None:
        """Обновить запись. Если content изменился + Qdrant — обновляем и вектор."""
        ...

    # ── DELETE ──

    async def delete(self, memory_id: str) -> bool:
        """Hard delete из PG и Qdrant."""
        ...

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
    ) -> int:
        """Soft delete: установить is_archived = true. Qdrant — hard delete."""
        ...

    async def archive(self, memory_id: str) -> bool:
        """Мягкое удаление: установить is_archived = true."""
        ...

    # ── READ ──

    async def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        """Получить запись по ID."""
        ...

    async def find_by_content_hash(
        self, namespace: str, content_hash: str
    ) -> MemoryRecord | None:
        """Найти запись по content_hash в namespace."""
        ...

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        """Список memories с общим счётчиком."""
        ...

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        """Последние записи по времени."""
        ...

    async def get_stats(self, user_id: str | None = None) -> list[MemoryStatsItem]:
        """Статистика по namespace."""
        ...

    # ── RELATIONS ──

    async def add_relation(
        self,
        source_id: str,
        target_id: str | None = None,
        target_name: str | None = None,
        link_type: str = "related_to",
        description: str | None = None,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> str:
        """Создать связь. Возвращает ID."""
        ...

    async def get_relations_by_source(
        self, source_id: str, link_type: str | None = None
    ) -> list[Relation]:
        """Исходящие связи из source_id."""
        ...

    async def get_relations_by_target(
        self, target_id: str, link_type: str | None = None
    ) -> list[Relation]:
        """Входящие связи в target_id."""
        ...

    async def get_relations(
        self, memory_id: str, link_type: str | None = None
    ) -> RelationListResult:
        """Все связи гранулы (incoming + outgoing) одним запросом."""
        ...

    async def delete_relation(
        self, source_id: str, target_id: str, link_type: str
    ) -> bool:
        """Удалить конкретную связь."""
        ...

    async def delete_relations_by_source(self, source_id: str) -> int:
        """Удалить все связи из source_id."""
        ...

    async def find_relations_between(
        self, source_id: str, target_id: str
    ) -> list[Relation]:
        """Найти связи между двумя гранулами."""
        ...

    # ── GRAPH ──

    async def traverse(
        self, start_id: str, depth: int = 3, link_types: list[str] | None = None
    ) -> dict:
        """Обход графа. Возвращает {nodes: JSONB, edges: JSONB}."""
        ...

    async def sync_links_to_relations(self, memory_id: str) -> int:
        """Синхронизировать metadata.links → relations для одной гранулы."""
        ...

    async def sync_links_batch(self, memory_ids: list[str]) -> int:
        """Batch-синхронизация metadata.links → relations."""
        ...

    async def get_graph_stats(self) -> GraphStats:
        """Статистика графа."""
        ...


class NamespaceRepositoryProtocol(Protocol):
    """Контракт для реестра namespace-ов.

    Управление namespace: auto-register, кэширование, инвалидация.
    """

    async def get_or_create(self, uid: str, name: str | None = None):
        """Получить namespace по uid. Если нет — создать автоматически.

        Возвращает NamespaceRecord(id, uid, name, description).
        """
        ...

    async def get_by_uid(self, uid: str):
        """Получить namespace по uid. Если нет — вернуть None (без auto-register).

        Возвращает NamespaceRecord или None.
        """
        ...

    async def list_all(self):
        """Получить все namespaces.

        Возвращает list[NamespaceRecord].
        """
        ...

    async def invalidate(self, uid: str | None = None) -> None:
        """Сбросить кэш для конкретного namespace или всего."""
        ...
