from __future__ import annotations

from datetime import datetime

from memory_server.config import Settings
from memory_server.embedding.provider import EmbeddingProvider
from memory_server.exceptions import NotFoundError
from memory_server.logger import async_measure_duration, get_logger
from memory_server.memory.dedup import DedupAction, DedupEngine
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.project_repository import ProjectRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    ProjectContext,
    Relation,
    RelationListResult,
    SearchResult,
    TraverseResult,
)

logger = get_logger(__name__)

# Порядок секций механической сборки снапшота (D9); прочие ns — по алфавиту
_CONTEXT_SECTION_ORDER = (
    "project_meta",
    "code_knowledge",
    "dialogue_insights",
    "infrastructure",
)


class MemoryService:
    """Business logic layer for memory operations."""

    def __init__(
        self,
        repository: MemoryRepository,
        embedding_provider: EmbeddingProvider,
        namespace_repository: NamespaceRepository,
        config: Settings | None = None,
        project_repository: ProjectRepository | None = None,
    ):
        self.repository = repository
        self.embedding = embedding_provider
        self.ns_repo = namespace_repository
        self.project_repo = project_repository
        self.config = config or Settings()
        self.dedup = DedupEngine(repository, embedding_provider, self.config)

    async def resolve_project(self, project_id: str | None) -> str | None:
        """Ключ тула (slug | UUID | None) → project_id UUID. Неизвестный slug → NotFoundError."""
        if project_id is None:
            return None
        if self.project_repo is None:
            raise RuntimeError("project_repository is not configured")
        return await self.project_repo.resolve_id(project_id)

    async def store(
        self,
        content: str,
        user_id: str,
        metadata: dict | None = None,
        namespace: str | None = None,
        importance: int | None = None,
        project_id: str | None = None,
    ) -> tuple[MemoryRecord, DedupAction]:
        namespace = namespace or "default"
        async with async_measure_duration(logger, "store", namespace=namespace, user_id=user_id):
            ns_record = await self.ns_repo.get_or_create(namespace)
            resolved_project = await self.resolve_project(project_id)
            content_hash: str | None = None
            embedding: list[float] | None = None

            if self.config.dedup_enabled:
                decision = await self.dedup.check(content, user_id, namespace, metadata=metadata)
                content_hash = decision.content_hash
                embedding = decision.embedding
                logger.info("store: dedup", extra={
                    "action": decision.action.value,
                    "existing_id": decision.existing_id,
                    "score": decision.existing_score,
                })

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

            if embedding is None:
                embedding = await self.embedding.embed(content)
            memory_id = await self.repository.insert(
                user_id=user_id,
                content=content,
                embedding=embedding,
                metadata=metadata or {},
                namespace_id=ns_record.id,
                content_hash=content_hash,
                importance=importance or 3,
                project_id=resolved_project,
            )
            record = await self.repository.get_by_id(memory_id)
            if record is None:
                raise RuntimeError(f"Failed to retrieve memory after insert: {memory_id}")

            if metadata and "links" in metadata:
                try:
                    synced = await self.repository.sync_links_to_relations(memory_id)
                    logger.info("store: sync_links", extra={"synced": synced, "id": memory_id})
                except Exception:
                    logger.exception("store: sync_links FAILED (non-fatal)", extra={"id": memory_id})

            return record, DedupAction.INSERT

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
        namespace: str | None = None,
        project_id: str | None = None,
    ) -> list[SearchResult]:
        async with async_measure_duration(logger, "search", namespace=namespace, user_id=user_id):
            resolved_project = await self.resolve_project(project_id)
            query_embedding = await self.embedding.embed(query)
            results = await self.repository.search(
                query_embedding=query_embedding,
                user_id=user_id,
                limit=limit,
                threshold=threshold,
                namespace=namespace,
                query_text=query,
                project_id=resolved_project,
            )
            return results

    async def get(self, memory_id: str) -> MemoryRecord:
        async with async_measure_duration(logger, "get"):
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
        project_id: str | None = None,
        supersedes: str | None = None,
    ) -> MemoryRecord:
        """Обновить гранулу: metadata merge-ится, version бампит триггер БД.

        supersedes — ID замещаемой версии: старая закрывается атомарно
        (status='superseded', valid_to=valid_from этой, superseded_by=id этой).
        """
        async with async_measure_duration(logger, "update"):
            resolved_project = await self.resolve_project(project_id)
            embedding = None
            if content is not None:
                embedding = await self.embedding.embed(content)
            record = await self.repository.update(
                memory_id=memory_id,
                content=content,
                embedding=embedding,
                metadata=metadata,
                importance=importance,
                project_id=resolved_project,
                supersedes=supersedes,
            )
            if record is None:
                raise NotFoundError(memory_id)

            if metadata is not None and "links" in metadata:
                try:
                    synced = await self.repository.sync_links_to_relations(memory_id)
                    logger.info("update: sync_links", extra={"synced": synced, "id": memory_id})
                except Exception:
                    logger.exception("update: sync_links FAILED (non-fatal)", extra={"id": memory_id})

            return record

    async def delete(self, memory_id: str) -> bool:
        async with async_measure_duration(logger, "delete"):
            return await self.repository.delete(memory_id)

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
        project_id: str | None = None,
    ) -> MemoryListResult:
        async with async_measure_duration(logger, "list", namespace=namespace):
            resolved_project = await self.resolve_project(project_id)
            return await self.repository.list(
                user_id=user_id,
                namespace=namespace,
                limit=limit,
                offset=offset,
                project_id=resolved_project,
            )

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[MemoryRecord]:
        logger.info("recent", extra={"namespace": namespace, "limit": limit, "since": str(since)})
        resolved_project = await self.resolve_project(project_id)
        results = await self.repository.recent(
            namespace=namespace,
            since=since,
            limit=limit,
            project_id=resolved_project,
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
        """Отзыв гранулы: status='retracted', valid_to=now().

        Гранула уходит из выдачи, но остаётся в БД и Qdrant (с пометкой
        статуса) — восстановима и доступна для time-travel (Фаза 1.3).
        """
        logger.info("archive", extra={"id": memory_id})
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            logger.info("archive: not found", extra={"id": memory_id})
            raise NotFoundError(memory_id)
        result = await self.repository.archive(memory_id)
        logger.info("archive: done", extra={"id": memory_id, "success": result})
        return result

    # ── Project context («облачко знаний», D9) ──

    async def get_project_context(self, project: str, refresh: bool = False) -> ProjectContext:
        """Снапшот контекста проекта. refresh=True — немедленный пересчёт.

        Fast-path без пересчёта; Redis-кеш ctx:{slug} и Celery-обвязка — Фаза 6.
        """
        project_id = await self.resolve_project(project)
        if project_id is None:
            raise NotFoundError(project, message="project is required for context")
        if not refresh:
            existing = await self.repository.get_project_context(project_id)
            if existing is not None:
                return ProjectContext.model_validate(existing)
        return await self._rebuild_context(project_id)

    async def rebuild_project_context(self, project: str) -> ProjectContext:
        """Пересчитать снапшот из топ-гранул проекта (хранимка 019)."""
        project_id = await self.resolve_project(project)
        if project_id is None:
            raise NotFoundError(project, message="project is required for context")
        return await self._rebuild_context(project_id)

    async def _rebuild_context(self, project_id: str) -> ProjectContext:
        """Механическая сборка снапшота: секции по namespace, без прозы.

        Проза Тиши (sections.prose) — Фаза 6; этот формат — её fallback.
        """
        rows = await self.repository.fetch_project_context(project_id)
        sections: dict[str, list[str]] = {}
        for row in rows:
            sections.setdefault(row["namespace"], []).append(row["content"])

        ordered = [ns for ns in _CONTEXT_SECTION_ORDER if ns in sections]
        ordered += sorted(ns for ns in sections if ns not in _CONTEXT_SECTION_ORDER)
        content = "\n\n".join(
            f"## {ns}\n" + "\n".join(f"- {item}" for item in sections[ns])
            for ns in ordered
        )

        saved = await self.repository.upsert_project_context(
            project_id,
            content=content,
            sections=sections,
            granule_count=len(rows),
        )
        return ProjectContext(
            project_id=project_id,
            content=content,
            sections=sections,
            granule_count=len(rows),
            computed_at=saved.get("computed_at"),
        )

    # ── Relations ──

    async def _resolve_granule(self, granule_id: str) -> MemoryRecord | None:
        """Найти гранулу: сначала по UUID, потом по entity_name (fallback)."""
        # Пробуем UUID
        try:
            record = await self.repository.get_by_id(granule_id)
            if record is not None:
                return record
        except Exception as exc:
            # Ожидаемо для не-UUID входа, но сюда же попадает сбой БД — не молчим
            logger.warning("resolve_granule: uuid lookup failed, fallback to entity_name", extra={
                "input": granule_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })

        # Fallback: ищем по entity_name
        record = await self.repository.find_by_entity_name(granule_id)
        if record is not None:
            logger.info("resolve: found by entity_name", extra={
                "input": granule_id, "resolved_id": record.id,
            })
            return record

        logger.warning("resolve_granule: not found by uuid nor entity_name", extra={
            "input": granule_id,
        })
        return record

    async def add_relation(
        self,
        source_id: str,
        target_id: str | None = None,
        target_name: str | None = None,
        link_type: str = "related_to",
        description: str | None = None,
        weight: float = 1.0,
        metadata: dict | None = None,
        project_id: str | None = None,
    ) -> str | None:
        """Создать связь между гранулами. Поддерживает строковые entity_name как ID.

        project_id — только ранняя валидация проекта (понятная ошибка при
        неизвестном slug); связи фильтром проекта не ограничены.
        """
        logger.info("add_relation", extra={
            "source": source_id, "target": target_id,
            "type": link_type, "weight": weight,
        })
        if project_id is not None:
            await self.resolve_project(project_id)

        # Resolve source
        source = await self._resolve_granule(source_id)
        if source is None:
            logger.warning("add_relation: source not found", extra={"source": source_id})
            return None
        resolved_source_id = source.id

        # Resolve target
        resolved_target_id: str | None = None
        if target_id is not None:
            target = await self._resolve_granule(target_id)
            if target is None:
                logger.warning("add_relation: target not found", extra={"target": target_id})
                return None
            resolved_target_id = target.id

        rel_id = await self.repository.add_relation(
            source_id=resolved_source_id,
            target_id=resolved_target_id,
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
        """Получить входящие и исходящие связи гранулы. Один запрос вместо двух."""
        logger.info("get_relations", extra={"id": memory_id, "link_type": link_type})
        result = await self.repository.get_relations(memory_id, link_type)
        logger.info("get_relations: done", extra={"incoming": len(result.incoming), "outgoing": len(result.outgoing)})
        return result

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
        """Обход графа от начальной ноды. Один round-trip вместо 2N+2."""
        logger.info("traverse", extra={
            "start_id": start_id, "depth": depth, "link_types": link_types,
        })
        # Валидация: start_id должен существовать
        start = await self.repository.get_by_id(start_id)
        if start is None:
            raise NotFoundError(f"Start granule: {start_id}")
        raw = await self.repository.traverse(start_id, depth, link_types)
        # Парсим результат хранимки
        nodes = raw["nodes"]  # [{id, content, namespace, importance, depth}]
        edges = [
            Relation(
                id=str(e["id"]),
                source_id=str(e["source_id"]),
                target_id=str(e["target_id"]) if e.get("target_id") else None,
                link_type=e["link_type"],
                description=e.get("description"),
                weight=float(e.get("weight", 1.0)),
                metadata=e.get("metadata", {}),
            )
            for e in raw["edges"]
        ]
        logger.info("traverse: done", extra={"nodes": len(nodes), "edges": len(edges)})
        return TraverseResult(nodes=nodes, edges=edges)

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
