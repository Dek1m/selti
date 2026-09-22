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

from memory_server.memory.search_fusion import HybridCandidate
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
    Методы разделены на: INSERT, SEARCH, UPDATE, DELETE, READ, CONTEXT, RELATIONS, GRAPH.

    Семантика статусов (миграция 018, D3): актуальность гранулы =
    status='asserted' AND valid_to IS NULL; отзыв = status='retracted'.
    """

    # ── INSERT ──

    async def insert(
        self,
        user_id: str,
        content: str,
        embedding: list[float] | None = None,
        metadata: dict | None = None,
        namespace_id: str | None = None,
        content_hash: str | None = None,
        importance: int = 3,
        project_id: str | None = None,
        confidence: float | None = None,
        frozen: bool = False,
        supersedes: str | None = None,
    ) -> str:
        """Создать новую гранулу. Возвращает ID."""
        ...

    async def insert_batch(
        self,
        user_ids: list[str],
        contents: list[str],
        namespace_ids: list[str],
        content_hashes: list[str | None],
        project_ids: list[str | None],
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
        project_id: str | None = None,
        include_historical: bool = False,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        status: str | None = None,
    ) -> list[SearchResult]:
        """Векторный поиск по embedding. Если Qdrant недоступен — SQL FTS fallback."""
        ...

    async def search_hybrid(
        self,
        query_embedding: list[float],
        query_text: str,
        user_id: str | None = None,
        namespace: str | None = None,
        project_id: str | None = None,
        threshold: float = 0.7,
        prefetch: int = 100,
        include_historical: bool = False,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        status: str | None = None,
    ) -> list[HybridCandidate]:
        """Двухканальный сбор кандидатов (Qdrant dense + PG FTS, Фаза 1.1)."""
        ...

    # ── UPDATE ──

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        embedding: list[float] | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
        project_id: str | None = None,
        confidence: float | None = None,
        frozen: bool | None = None,
        supersedes: str | None = None,
        content_hash: str | None = None,
    ) -> MemoryRecord | None:
        """Обновить гранулу (metadata merge-ится). supersedes — закрыть старую версию."""
        ...

    async def create_version(
        self,
        old_id: str,
        content: str,
        embedding: list[float] | None = None,
        metadata: dict | None = None,
        content_hash: str | None = None,
        importance: int | None = None,
        confidence: float | None = None,
    ) -> MemoryRecord | None:
        """Новая версия факта-конфликта (Фаза 2.1, D3): INSERT-SELECT
        наследует user/namespace/project/version+1, старая закрывается
        valid_from'ом новой. None — старая не найдена."""
        ...

    async def get_history(self, granule_id: str) -> list[MemoryRecord]:
        """Supersession-цепочка (CTE, обе стороны) — от старейшей к новейшей."""
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
        """Мягкое забвение: status='retracted', valid_to=now(). Qdrant — hard delete."""
        ...

    async def archive(self, memory_id: str, reason: str | None = None) -> bool:
        """Отзыв гранулы: status='retracted', valid_to=now(), metadata.reason."""
        ...

    # ── LIFECYCLE (Фаза 2.2) / CLUSTERS (Фаза 2.3) ──

    async def decay_confidence(
        self, ns_uids: list[str], rates: list[float], default_rate: float, floor: float
    ) -> dict[str, int]:
        """Ежедневное затухание уверенности (батч-SQL): счёт по namespace."""
        ...

    async def count_stale(self, threshold: float, stale_days: int) -> int:
        """Счётчик устаревших кандидатов (статус не меняется)."""
        ...

    async def list_stale(
        self,
        threshold: float,
        stale_days: int,
        user_id: str | None = None,
        namespace_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Кандидаты на ревизию (динамический критерий, колонки-флага нет)."""
        ...

    async def select_gc_superseded(self, retention_days: int) -> list[str]:
        """ID superseded-гранул под GC: с наследником и старше retention."""
        ...

    async def purge_memories(self, granule_ids: list[str]) -> int:
        """Hard delete гранул + Qdrant-точек + безадресных связей (GC)."""
        ...

    async def delete_orphan_relations(self) -> int:
        """Удалить связи без адреса целиком. Идемпотентно."""
        ...

    async def refresh_clusters(
        self,
        namespace_id: str,
        threshold: float,
        top_k: int = 10,
        min_members: int = 2,
    ) -> list[dict]:
        """Кластеризация Level 2 v2: Qdrant ANN-пары → assign_clusters_from_pairs (022).

        Qdrant-фаза поднимает VectorStoreError (пар нет — пересчёт запрещён).
        """
        ...

    async def list_clusters(
        self, namespace: str | None = None, project_id: str | None = None
    ) -> list[dict]:
        """Обзор кластеров Level 2 (022)."""
        ...

    # ── READ ──

    async def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        """Получить запись по ID (любого статуса — для истории/восстановления)."""
        ...

    async def find_by_content_hash(
        self, namespace: str, content_hash: str
    ) -> MemoryRecord | None:
        """Найти актуальную запись по content_hash в namespace (exact-dedup)."""
        ...

    async def find_by_content_hashes(
        self, ns_uids: list[str], content_hashes: list[str]
    ) -> dict[tuple[str, str], MemoryRecord]:
        """Batch exact-dedup: {(namespace, content_hash): record} одним запросом."""
        ...

    async def bump_access(self, memory_ids: list[str]) -> int:
        """Инкремент access_count/last_accessed_at по выданным id (Фаза 1.2)."""
        ...

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
        project_id: str | None = None,
    ) -> MemoryListResult:
        """Список актуальных гранул с общим счётчиком."""
        ...

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Последние актуальные записи по времени."""
        ...

    async def get_stats(self, user_id: str | None = None) -> list[MemoryStatsItem]:
        """Статистика по namespace (только актуальные гранулы)."""
        ...

    # ── PROJECT CONTEXTS («облачко знаний», D9) ──

    async def fetch_project_context(
        self, project_id: str, limit_per_ns: int = 15
    ) -> list[dict]:
        """Топ-гранулы проекта с квотами per namespace (хранимка 019)."""
        ...

    async def upsert_project_context(
        self,
        project_id: str,
        content: str | None = None,
        sections: dict | None = None,
        granule_count: int = 0,
    ) -> dict:
        """Сохранить/обновить снапшот контекста проекта."""
        ...

    async def get_project_context(self, project_id: str) -> dict | None:
        """Прочитать снапшот контекста проекта (None — ещё не построен)."""
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
        self,
        start_id: str,
        depth: int = 3,
        link_types: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        """Обход графа. Возвращает {nodes: JSONB, edges: JSONB}."""
        ...

    async def sync_links_to_relations(self, memory_id: str) -> int:
        """Синхронизировать metadata.links → relations для одной гранулы."""
        ...

    async def sync_links_batch(self, memory_ids: list[str]) -> int:
        """Batch-синхронизация metadata.links → relations."""
        ...

    # ── EDGE LIFECYCLE (V3.5 «Жизнь графа знаний») ──

    async def reinforce_relations(
        self, pairs: list[tuple[str, str]], alpha: float
    ) -> int:
        """Касание пар гранул: +1 использование, якорь сейчас, вес к 1.0."""
        ...

    async def prune_candidates(
        self,
        decay_lambda: float,
        lambda_min: float,
        min_age_days: int,
        floor: float,
    ) -> list[str]:
        """ID кандидатов отсечения (иммунитеты/мост/возраст/raw w_eff в SQL)."""
        ...

    async def prune_edges_apply(self, edge_ids: list[str]) -> int:
        """Отсечение: только pruned_at, батчами. Идемпотентно."""
        ...

    async def restore_edge(self, edge_id: str, restore_beta: float) -> bool:
        """Воскрешение pruned-ребра (pruned_at=NULL + усиленный reinforce)."""
        ...

    async def fetch_activation_edges(
        self,
        decay_lambda: float,
        lambda_min: float,
        link_types: list[str] | None = None,
        symmetric_link_types: list[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Живой граф (source, target, w_eff) для PPR-traverse; типы из
        symmetric_link_types зеркалятся встречными дугами."""
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
