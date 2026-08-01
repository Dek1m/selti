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
        logger.info("store: START", extra={
            "content_len": len(content), "user_id": user_id,
            "namespace": namespace, "importance": importance,
        })
        ns_record = await self.ns_repo.get_or_create(namespace)
        content_hash: str | None = None
        embedding: list[float] | None = None

        if self.config.dedup_enabled:
            decision = await self.dedup.check(content, user_id, namespace, metadata=metadata)
            content_hash = decision.content_hash
            embedding = decision.embedding  # кэш эмбеддинга от dedup
            logger.info("store: dedup decision", extra={
                "action": decision.action.value,
                "existing_id": decision.existing_id,
                "score": decision.existing_score,
            })

            if decision.action == DedupAction.SKIP:
                record = await self.repository.get_by_id(decision.existing_id)
                if record is None:
                    raise RuntimeError(f"Failed to retrieve existing memory: {decision.existing_id}")
                logger.info("store: SKIP", extra={"existing_id": decision.existing_id})
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
                logger.info("store: UPDATE", extra={"existing_id": decision.existing_id})
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

        # Sync metadata.links → relations если есть links
        if metadata and "links" in metadata:
            try:
                synced = await self.repository.sync_links_to_relations(memory_id)
                logger.info("store: sync_links", extra={"synced": synced, "id": memory_id})
            except Exception as e:
                logger.exception("store: sync_links FAILED (non-fatal)", extra={"id": memory_id})

        logger.info("store: INSERT", extra={"id": record.id, "namespace": namespace})
        return record, DedupAction.INSERT

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
        namespace: str | None = None,
    ) -> list[SearchResult]:
        logger.info("search: START", extra={
            "query": query[:200], "namespace": namespace,
            "limit": limit, "threshold": threshold, "user_id": user_id,
        })
        query_embedding = await self.embedding.embed(query)
        results = await self.repository.search(
            query_embedding=query_embedding,
            user_id=user_id,
            limit=limit,
            threshold=threshold,
            namespace=namespace,
            query_text=query,
        )
        logger.info("search: done", extra={"count": len(results)})
        return results

    async def get(self, memory_id: str) -> MemoryRecord:
        logger.info("get", extra={"id": memory_id})
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            logger.info("get: not found", extra={"id": memory_id})
            raise NotFoundError(memory_id)
        logger.info("get: found", extra={"id": record.id, "namespace": record.namespace})
        return record

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
    ) -> MemoryRecord:
        logger.info("update: START", extra={
            "id": memory_id, "has_content": content is not None,
            "has_metadata": metadata is not None, "importance": importance,
        })
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
            logger.info("update: not found", extra={"id": memory_id})
            raise NotFoundError(memory_id)

        # Sync metadata.links → relations если metadata обновились
        if metadata is not None and "links" in metadata:
            try:
                synced = await self.repository.sync_links_to_relations(memory_id)
                logger.info("update: sync_links", extra={"synced": synced, "id": memory_id})
            except Exception as e:
                logger.exception("update: sync_links FAILED (non-fatal)", extra={"id": memory_id})

        logger.info("update: done", extra={"id": record.id, "namespace": record.namespace})
        return record

    async def delete(self, memory_id: str) -> bool:
        logger.info("delete", extra={"id": memory_id})
        result = await self.repository.delete(memory_id)
        logger.info("delete: done", extra={"id": memory_id, "success": result})
        return result

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        logger.info("list", extra={
            "namespace": namespace, "limit": limit,
            "offset": offset, "user_id": user_id,
        })
        result = await self.repository.list(
            user_id=user_id,
            namespace=namespace,
            limit=limit,
            offset=offset,
        )
        logger.info("list: done", extra={"total": result.total, "items": len(result.items)})
        return result

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        logger.info("recent", extra={"namespace": namespace, "limit": limit, "since": str(since)})
        results = await self.repository.recent(
            namespace=namespace,
            since=since,
            limit=limit,
        )
        logger.info("recent: done", extra={"count": len(results)})
        return results

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
    ) -> int:
        logger.info("forget", extra={"user_id": user_id, "namespace": namespace})
        count = await self.repository.forget(
            user_id=user_id,
            namespace=namespace,
        )
        logger.info("forget: done", extra={"deleted_count": count})
        return count

    async def get_stats(self, user_id: str | None = None) -> list:
        logger.info("get_stats", extra={"user_id": user_id})
        result = await self.repository.get_stats(user_id)
        logger.info("get_stats: done", extra={"namespaces": len(result)})
        return result

    async def archive(self, memory_id: str) -> bool:
        """Мягкое удаление: установить is_archived = true."""
        logger.info("archive", extra={"id": memory_id})
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            logger.info("archive: not found", extra={"id": memory_id})
            raise NotFoundError(memory_id)
        result = await self.repository.archive(memory_id)
        logger.info("archive: done", extra={"id": memory_id, "success": result})
        return result

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
        logger.info("add_relation", extra={
            "source": source_id, "target": target_id,
            "type": link_type, "weight": weight,
        })
        # Валидация: source_id должен существовать
        source = await self.repository.get_by_id(source_id)
        if source is None:
            raise NotFoundError(f"Source granule: {source_id}")
        # Валидация: target_id должен существовать (если указан)
        if target_id is not None:
            target = await self.repository.get_by_id(target_id)
            if target is None:
                raise NotFoundError(f"Target granule: {target_id}")
        rel_id = await self.repository.add_relation(
            source_id=source_id,
            target_id=target_id,
            target_name=target_name,
            link_type=link_type,
            description=description,
            weight=weight,
            metadata=metadata,
        )
        logger.info("add_relation: done", extra={"relation_id": rel_id})
        return rel_id

    async def get_relations(
        self, memory_id: str, link_type: str | None = None
    ) -> RelationListResult:
        """Получить входящие и исходящие связи гранулы."""
        logger.info("get_relations", extra={"id": memory_id, "link_type": link_type})
        outgoing = await self.repository.get_relations_by_source(memory_id, link_type)
        incoming = await self.repository.get_relations_by_target(memory_id, link_type)
        logger.info("get_relations: done", extra={"incoming": len(incoming), "outgoing": len(outgoing)})
        return RelationListResult(incoming=incoming, outgoing=outgoing)

    async def delete_relation(
        self, source_id: str, target_id: str, link_type: str
    ) -> bool:
        """Удалить связь."""
        logger.info("delete_relation", extra={
            "source": source_id, "target": target_id, "type": link_type,
        })
        result = await self.repository.delete_relation(source_id, target_id, link_type)
        logger.info("delete_relation: done", extra={"success": result})
        return result

    async def traverse(
        self, start_id: str, depth: int = 3, link_types: list[str] | None = None
    ) -> TraverseResult:
        """Обход графа от начальной ноды."""
        logger.info("traverse", extra={
            "start_id": start_id, "depth": depth, "link_types": link_types,
        })
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
        logger.info("traverse: done", extra={"nodes": len(nodes), "edges": len(all_edges)})
        return TraverseResult(nodes=nodes, edges=all_edges)

    async def get_graph_stats(self) -> GraphStats:
        """Статистика графа знаний."""
        logger.info("get_graph_stats")
        result = await self.repository.get_graph_stats()
        logger.info("get_graph_stats: done", extra={
            "granules": result.total_granules,
            "relations": result.total_relations,
            "orphans": result.orphans,
        })
        return result
