from __future__ import annotations

from datetime import datetime
import logging

from memory_server.config import Settings
from memory_server.embedding.provider import EmbeddingProvider
from memory_server.exceptions import NotFoundError
from memory_server.memory.dedup import DedupAction, DedupEngine
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    Relation,
    RelationListResult,
    SearchResult,
    TraverseResult,
)

logger = logging.getLogger(__name__)


class MemoryService:
    """Business logic layer for memory operations."""

    def __init__(
        self,
        repository: MemoryRepository,
        embedding_provider: EmbeddingProvider,
        namespace_repository: NamespaceRepository,
        config: Settings | None = None,
    ):
        self.repository = repository
        self.embedding = embedding_provider
        self.ns_repo = namespace_repository
        self.config = config or Settings()
        self.dedup = DedupEngine(repository, embedding_provider, self.config)

    async def store(
        self,
        content: str,
        user_id: str,
        metadata: dict | None = None,
        namespace: str | None = None,
        importance: int | None = None,
    ) -> tuple[MemoryRecord, DedupAction]:
        namespace = namespace or "default"
        ns_record = await self.ns_repo.get_or_create(namespace)
        content_hash: str | None = None
        embedding: list[float] | None = None

        if self.config.dedup_enabled:
            decision = await self.dedup.check(content, user_id, namespace, metadata=metadata)
            content_hash = decision.content_hash
            embedding = decision.embedding  # кэш эмбеддинга от dedup

            if decision.action == DedupAction.SKIP:
                record = await self.repository.get_by_id(decision.existing_id)
                if record is None:
                    raise RuntimeError(f"Failed to retrieve existing memory: {decision.existing_id}")
                return record, DedupAction.SKIP

            if decision.action == DedupAction.UPDATE:
                record = await self.repository.get_by_id(decision.existing_id)
                if record is None:
                    raise RuntimeError(f"Failed to retrieve memory for update: {decision.existing_id}")
                updated = await self.repository.update(
                    memory_id=decision.existing_id,
                    metadata={**record.metadata, **(metadata or {})},
                )
                if updated is None:
                    raise RuntimeError(f"Failed to update memory: {decision.existing_id}")
                return updated, DedupAction.UPDATE

        # Используем кэшированный эмбеддинг, или генерируем новый
        if embedding is None:
            embedding = await self.embedding.embed(content)
        memory_id = await self.repository.insert(
            user_id=user_id,
            content=content,
            embedding=embedding,
            metadata=metadata or {},
            namespace=namespace,
            namespace_id=ns_record.id,
            content_hash=content_hash,
            importance=importance or 3,
        )
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            raise RuntimeError(f"Failed to retrieve memory after insert: {memory_id}")
        return record, DedupAction.INSERT

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
        namespace: str | None = None,
    ) -> list[SearchResult]:
        query_embedding = await self.embedding.embed(query)
        return await self.repository.search(
            query_embedding=query_embedding,
            user_id=user_id,
            limit=limit,
            threshold=threshold,
            namespace=namespace,
        )

    async def get(self, memory_id: str) -> MemoryRecord:
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            raise NotFoundError(memory_id)
        return record

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
    ) -> MemoryRecord:
        embedding = None
        if content is not None:
            embedding = await self.embedding.embed(content)
        record = await self.repository.update(
            memory_id=memory_id,
            content=content,
            embedding=embedding,
            metadata=metadata,
            importance=importance,
        )
        if record is None:
            raise NotFoundError(memory_id)
        return record

    async def delete(self, memory_id: str) -> bool:
        return await self.repository.delete(memory_id)

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        return await self.repository.list(
            user_id=user_id,
            namespace=namespace,
            limit=limit,
            offset=offset,
        )

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        return await self.repository.recent(
            namespace=namespace,
            since=since,
            limit=limit,
        )

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
    ) -> int:
        return await self.repository.forget(
            user_id=user_id,
            namespace=namespace,
        )

    async def get_stats(self, user_id: str | None = None) -> list:
        return await self.repository.get_stats(user_id)

    async def archive(self, memory_id: str) -> bool:
        """Мягкое удаление: установить is_archived = true."""
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            raise NotFoundError(memory_id)
        return await self.repository.archive(memory_id)

    # ── Relations ──

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
        """Создать связь между гранулами."""
        # Валидация: source_id должен существовать
        source = await self.repository.get_by_id(source_id)
        if source is None:
            raise NotFoundError(f"Source granule: {source_id}")
        # Валидация: target_id должен существовать (если указан)
        if target_id is not None:
            target = await self.repository.get_by_id(target_id)
            if target is None:
                raise NotFoundError(f"Target granule: {target_id}")
        return await self.repository.add_relation(
            source_id=source_id,
            target_id=target_id,
            target_name=target_name,
            link_type=link_type,
            description=description,
            weight=weight,
            metadata=metadata,
        )

    async def get_relations(
        self, memory_id: str, link_type: str | None = None
    ) -> RelationListResult:
        """Получить входящие и исходящие связи гранулы."""
        outgoing = await self.repository.get_relations_by_source(memory_id, link_type)
        incoming = await self.repository.get_relations_by_target(memory_id, link_type)
        return RelationListResult(incoming=incoming, outgoing=outgoing)

    async def delete_relation(
        self, source_id: str, target_id: str, link_type: str
    ) -> bool:
        """Удалить связь."""
        return await self.repository.delete_relation(source_id, target_id, link_type)

    async def traverse(
        self, start_id: str, depth: int = 3, link_types: list[str] | None = None
    ) -> TraverseResult:
        """Обход графа от начальной ноды."""
        # Валидация: start_id должен существовать
        start = await self.repository.get_by_id(start_id)
        if start is None:
            raise NotFoundError(f"Start granule: {start_id}")
        nodes_raw = await self.repository.traverse(start_id, depth, link_types)
        # Загружаем полные данные для каждой ноды
        nodes = []
        all_edges: list[Relation] = []
        for n in nodes_raw:
            record = await self.repository.get_by_id(n["node_id"])
            if record:
                nodes.append({
                    "id": record.id,
                    "content": record.content[:200],
                    "namespace": record.namespace,
                    "depth": n["depth"],
                })
                # Собираем рёбра от этой ноды
                edges = await self.repository.get_relations_by_source(
                    n["node_id"],
                    link_type=None,  # все типы
                )
                # Фильтруем только те, которые ведут в пределах обхода
                node_ids = {nd["node_id"] for nd in nodes_raw}
                all_edges.extend(
                    e for e in edges if e.target_id and e.target_id in node_ids
                )
        return TraverseResult(nodes=nodes, edges=all_edges)

    async def get_graph_stats(self) -> GraphStats:
        """Статистика графа знаний."""
        return await self.repository.get_graph_stats()
