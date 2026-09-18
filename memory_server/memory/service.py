from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from memory_server.config import Settings
from memory_server.embedding.provider import EmbeddingProvider
from memory_server.exceptions import NotFoundError
from memory_server.logger import async_measure_duration, get_logger
from memory_server.memory.dedup import DedupAction, DedupEngine
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.project_repository import ProjectRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.memory.search_fusion import (
    final_score,
    importance_weight,
    mmr_rerank,
    recency_decay,
    rrf_fuse,
)
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


def _days_since(moment: datetime | None, now: datetime) -> float:
    """Полных дней с момента (для recency decay); отсутствие момента → 0."""
    if moment is None:
        return 0.0
    return max((now - moment).total_seconds() / 86400.0, 0.0)


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
                    # Обновляем и content, и пересчитанный content_hash (Фаза 1.4):
                    # иначе unique-индекс (namespace_id, content_hash) поймает
                    # рассинхрон при изменившемся контенте.
                    updated = await self.repository.update(
                        memory_id=decision.existing_id,
                        content=content,
                        content_hash=content_hash,
                        embedding=embedding,
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
        include_historical: bool = False,
    ) -> list[SearchResult]:
        async with async_measure_duration(logger, "search", namespace=namespace, user_id=user_id):
            resolved_project = await self.resolve_project(project_id)
            query_embedding = await self.embedding.embed(query)
            if not self.config.hybrid_search_enabled:
                # Фича-флаг отката (не legacy): выключенный hybrid = плотный
                # Qdrant-путь Фазы 0, документирован в конфиге.
                return await self.repository.search(
                    query_embedding=query_embedding,
                    user_id=user_id,
                    limit=limit,
                    threshold=threshold,
                    namespace=namespace,
                    query_text=query,
                    project_id=resolved_project,
                    include_historical=include_historical,
                )
            return await self._search_hybrid(
                query=query,
                query_embedding=query_embedding,
                user_id=user_id,
                limit=limit,
                threshold=threshold,
                namespace=namespace,
                project_id=resolved_project,
                include_historical=include_historical,
            )

    async def _search_hybrid(
        self,
        query: str,
        query_embedding: list[float],
        user_id: str | None,
        limit: int,
        threshold: float,
        namespace: str | None,
        project_id: str | None,
        include_historical: bool,
    ) -> list[SearchResult]:
        """Hybrid search (Фаза 1.1/1.2): RRF-fusion → MMR → D4-ранжирование."""
        candidates = await self.repository.search_hybrid(
            query_embedding=query_embedding,
            query_text=query,
            user_id=user_id,
            namespace=namespace,
            project_id=project_id,
            threshold=threshold,
            prefetch=self.config.hybrid_prefetch,
            include_historical=include_historical,
        )
        if not candidates:
            return []

        rankings = [
            [c.id for c in candidates if c.rank_dense is not None],
            [c.id for c in candidates if c.rank_fts is not None],
        ]
        rrf_scores = rrf_fuse(rankings, k=self.config.rrf_k)
        vectors = {c.id: c.vector for c in candidates if c.vector}
        ordered = mmr_rerank(
            rrf_scores, vectors, top_k=limit, lambda_=self.config.mmr_lambda
        )

        now = datetime.now(timezone.utc)
        by_id = {c.id: c for c in candidates}
        results: list[SearchResult] = []
        for cand_id in ordered:
            cand = by_id[cand_id]
            # frozen — вечный факт: не затухает (D4)
            decay = (
                1.0
                if cand.frozen
                else recency_decay(
                    _days_since(cand.last_accessed_at or cand.created_at, now),
                    self.config.recency_decay_rates.get(
                        cand.namespace, self.config.recency_decay_rate
                    ),
                )
            )
            weight = importance_weight(
                cand.importance,
                self.config.importance_multipliers.get(cand.namespace, 1.0),
            )
            results.append(
                SearchResult(
                    id=cand_id,
                    content=cand.content,
                    metadata=cand.metadata,
                    importance=cand.importance,
                    score=round(final_score(rrf_scores[cand_id], decay, weight), 6),
                    project_id=cand.project_id,
                    status=cand.status,
                )
            )
        results.sort(key=lambda r: r.score, reverse=True)

        # Инкремент access-полей (Фаза 1.2): батч-UPDATE вне транзакции
        # чтения, только по фактически выданным id; сбой не роняет выдачу.
        try:
            await self.repository.bump_access([r.id for r in results])
        except Exception:
            logger.exception("search: bump_access FAILED (non-fatal)")

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

        При content пересчитывается content_hash (sha256) — иначе unique-индекс
        дедупа словит рассинхрон на следующем UPDATE (Фаза 1.4).
        supersedes — ID замещаемой версии: старая закрывается атомарно
        (status='superseded', valid_to=valid_from этой, superseded_by=id этой).
        """
        async with async_measure_duration(logger, "update"):
            resolved_project = await self.resolve_project(project_id)
            embedding = None
            content_hash = None
            if content is not None:
                embedding = await self.embedding.embed(content)
                content_hash = hashlib.sha256(content.encode()).hexdigest()
            record = await self.repository.update(
                memory_id=memory_id,
                content=content,
                embedding=embedding,
                metadata=metadata,
                importance=importance,
                project_id=resolved_project,
                supersedes=supersedes,
                content_hash=content_hash,
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
        self,
        start_id: str,
        depth: int = 3,
        link_types: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> TraverseResult:
        """Обход графа от начальной ноды. Один round-trip вместо 2N+2.

        Cap traverse_max_nodes + курсорная пагинация — Python-слой поверх
        хранимки graph_traverse_full (Фаза 1.5, миграций нет): стабильный
        порядок сортировкой id, срез [offset : offset+limit], рёбра —
        только между выданными узлами. total_nodes/truncated — навигация.
        """
        logger.info("traverse", extra={
            "start_id": start_id, "depth": depth, "link_types": link_types,
            "limit": limit, "offset": offset,
        })
        # Валидация: start_id должен существовать
        start = await self.repository.get_by_id(start_id)
        if start is None:
            raise NotFoundError(f"Start granule: {start_id}")
        raw = await self.repository.traverse(start_id, depth, link_types)

        all_nodes = sorted(raw["nodes"], key=lambda n: str(n["id"]))
        total = len(all_nodes)
        capped = all_nodes[: self.config.traverse_max_nodes]
        page = capped[offset : offset + limit] if limit is not None else capped[offset:]
        visible_ids = {str(n["id"]) for n in page}

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
            # Подграф из выданных узлов: soft-resolve связи (target_name-only)
            # сохраняем — они привязаны к видимому источнику
            if str(e["source_id"]) in visible_ids
            and (e.get("target_id") is None or str(e["target_id"]) in visible_ids)
        ]
        logger.info("traverse: done", extra={
            "nodes": len(page), "edges": len(edges), "total_nodes": total,
        })
        return TraverseResult(
            nodes=page,
            edges=edges,
            total_nodes=total,
            truncated=len(page) < total,
        )

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
