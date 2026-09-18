"""MemoryRepository facade — координирует PostgreSQL + Qdrant.

Реализует MemoryRepositoryProtocol.
Делегирует PG-операции → PostgreSQLRepository, Qdrant-операции → QdrantStore.

Контракт волны 2 (Фаза 0.4):
  * Вышестоящие слои (service, dedup, tasks) работают со строковыми uid
    namespace; резолв uid → namespace_id UUID выполняет ЗДЕСЬ через
    NamespaceRepository (TTL-кеш). Незарегистрированный uid = пустой
    результат, без auto-register на пути чтения.
  * Qdrant payload — диета (D6): без content/metadata; user_id,
    namespace_id, project_id, status, content_hash, importance.
"""
from __future__ import annotations

from datetime import datetime

from qdrant_client import models as qm

from memory_server.logger import get_logger
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.memory.qdrant_store import QdrantStore
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

logger = get_logger(__name__)


class _UnknownNamespace:
    """Сентинел: uid не зарегистрирован → пустой результат запроса."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unknown-namespace>"


_UNKNOWN_NS = _UnknownNamespace()


class MemoryRepository:
    """Facade: PostgreSQL (метаданные) + Qdrant (вектора).

    Внешний API совпадает с MemoryRepositoryProtocol.
    """

    def __init__(
        self,
        pg: PostgreSQLRepository,
        qdrant: QdrantStore | None = None,
        ns_repo: NamespaceRepository | None = None,
    ):
        self.pg = pg
        self.qdrant = qdrant
        self.ns_repo = ns_repo

    def _has_qdrant(self) -> bool:
        return self.qdrant is not None

    async def _ns_id(
        self, namespace: str | None
    ) -> str | None | _UnknownNamespace:
        """uid → namespace_id. None-вход → None-фильтр; неизвестный uid → сентинел."""
        if namespace is None:
            return None
        record = await self.ns_repo.get_by_uid(namespace)
        if record is None:
            logger.warning(
                "namespace not registered: empty result",
                extra={"namespace": namespace},
            )
            return _UNKNOWN_NS
        return record.id

    @staticmethod
    def _point_payload(
        user_id: str,
        namespace_id: str,
        importance: int,
        project_id: str | None = None,
        content_hash: str | None = None,
        status: str = "asserted",
    ) -> dict:
        """Qdrant payload на диете (D6): только фильтруемые поля, без content."""
        payload: dict = {
            "user_id": user_id,
            "namespace_id": str(namespace_id),
            "status": status,
            "importance": importance,
        }
        if project_id:
            payload["project_id"] = str(project_id)
        if content_hash:
            payload["content_hash"] = content_hash
        return payload

    # ════════════════════════════════════════════════════════════
    # INSERT
    # ════════════════════════════════════════════════════════════

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
        memory_id = await self.pg.insert(
            user_id=user_id,
            content=content,
            metadata=metadata,
            namespace_id=namespace_id,
            content_hash=content_hash,
            importance=importance,
            project_id=project_id,
            confidence=confidence,
            frozen=frozen,
            supersedes=supersedes,
        )

        if self._has_qdrant() and embedding is not None:
            self.qdrant.upsert_vector(
                point_id=memory_id,
                vector=embedding,
                payload=self._point_payload(
                    user_id=user_id,
                    namespace_id=namespace_id or "",
                    importance=importance,
                    project_id=project_id,
                    content_hash=content_hash,
                ),
            )

        return memory_id

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
        if importances is None:
            importances = [3] * len(user_ids)

        memory_ids = await self.pg.insert_batch(
            user_ids=user_ids,
            contents=contents,
            namespace_ids=namespace_ids,
            content_hashes=content_hashes,
            project_ids=project_ids,
            metadatas=metadatas,
            importances=importances,
        )

        if self._has_qdrant() and embeddings is not None:
            points = []
            skipped = 0
            for i, mid in enumerate(memory_ids):
                emb = embeddings[i] if isinstance(embeddings[i], list) else None
                if emb is None:
                    skipped += 1
                    continue
                points.append(
                    qm.PointStruct(
                        id=mid,
                        vector=emb,
                        payload=self._point_payload(
                            user_id=user_ids[i],
                            namespace_id=namespace_ids[i],
                            importance=importances[i],
                            project_id=project_ids[i] if project_ids else None,
                            content_hash=content_hashes[i] if content_hashes else None,
                        ),
                    )
                )
            logger.info("insert_batch: qdrant upsert", extra={
                "points": len(points), "skipped": skipped, "total": len(memory_ids),
            })
            self.qdrant.upsert_batch(points)

        return memory_ids

    # ════════════════════════════════════════════════════════════
    # SEARCH
    # ════════════════════════════════════════════════════════════

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
    ) -> list[SearchResult]:
        """Плотный Qdrant-путь (Фаза 0). Гибридный — search_hybrid (Фаза 1.1)."""
        namespace_id = await self._ns_id(namespace)
        if isinstance(namespace_id, _UnknownNamespace):
            return []

        if self._has_qdrant():
            search_filter = QdrantStore.build_filter(
                user_id=user_id,
                namespace_id=namespace_id,
                project_id=project_id,
                active_only=not include_historical,
            )
            qdrant_results = self.qdrant.search(
                query_vector=query_embedding,
                limit=limit,
                score_threshold=threshold,
                query_filter=search_filter,
            )

            if not qdrant_results:
                return []

            ids = [r["id"] for r in qdrant_results]
            scores = {r["id"]: r["score"] for r in qdrant_results}

            # Фильтр актуальности в SQL ДО обрезки limit (Фаза 1.3)
            rows = await self.pg.fetch_by_ids(ids, include_historical=include_historical)
            rows_by_id = {str(row["id"]): row for row in rows}

            results = []
            for qid in ids:
                row = rows_by_id.get(qid)
                if row:
                    results.append(
                        SearchResult(
                            id=qid,
                            content=row["content"],
                            metadata=row["metadata"] or {},
                            importance=row["importance"],
                            score=scores[qid],
                            project_id=row["project_id"],
                            status=row["status"],
                        )
                    )
            return results
        else:
            if not query_text:
                return []
            rows = await self.pg.search_fts(
                query_text=query_text,
                user_id=user_id,
                namespace_id=namespace_id,
                project_id=project_id,
                limit=limit,
                include_historical=include_historical,
            )
            return [
                SearchResult(
                    id=r["id"],
                    content=r["content"],
                    metadata=r["metadata"],
                    importance=r["importance"],
                    score=r["score"],
                    project_id=r.get("project_id"),
                    status=r.get("status", "asserted"),
                )
                for r in rows
            ]

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
    ) -> list[HybridCandidate]:
        """Двухканальный сбор кандидатов гибридного поиска (Фаза 1.1).

        Канал A — Qdrant dense (с векторами для MMR), канал B — PG FTS
        'russian'. Отказ канала не роняет поиск: RRF честно работает и по
        одному ранжированию. Fusion/ранжирование делает MemoryService.
        """
        namespace_id = await self._ns_id(namespace)
        if isinstance(namespace_id, _UnknownNamespace):
            return []

        dense_ranks: dict[str, int] = {}
        vectors: dict[str, list[float]] = {}

        if self._has_qdrant():
            try:
                dense = self.qdrant.search(
                    query_vector=query_embedding,
                    limit=prefetch,
                    score_threshold=threshold,
                    query_filter=QdrantStore.build_filter(
                        user_id=user_id,
                        namespace_id=namespace_id,
                        project_id=project_id,
                        active_only=not include_historical,
                    ),
                    with_vectors=True,
                )
                for rank, r in enumerate(dense):
                    doc_id = str(r["id"])
                    dense_ranks[doc_id] = rank
                    if r.get("vector") is not None:
                        vectors[doc_id] = r["vector"]
            except Exception as exc:
                logger.warning(
                    "hybrid: dense channel failed, FTS-only",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )

        fts_ranks: dict[str, int] = {}
        try:
            fts_rows = await self.pg.search_fts(
                query_text=query_text,
                user_id=user_id,
                namespace_id=namespace_id,
                project_id=project_id,
                limit=prefetch,
                include_historical=include_historical,
            )
            for rank, row in enumerate(fts_rows):
                fts_ranks[str(row["id"])] = rank
        except Exception as exc:
            logger.warning(
                "hybrid: FTS channel failed, dense-only",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )

        all_ids = list(dense_ranks.keys() | fts_ranks.keys())
        if not all_ids:
            return []

        # Догрузка канонических полей одним батчем; фильтр актуальности в SQL
        # ДО fusion — prefetch с запасом гарантирует, что отсеянные
        # ретрактнутые не съедают лимит выдачи (Фаза 1.3).
        rows = await self.pg.fetch_by_ids(all_ids, include_historical=include_historical)
        return [
            HybridCandidate(
                id=str(row["id"]),
                content=row["content"],
                metadata=row["metadata"] or {},
                namespace=row["namespace"],
                importance=row["importance"],
                project_id=row["project_id"],
                status=row["status"],
                created_at=row["created_at"],
                last_accessed_at=row["last_accessed_at"],
                frozen=row["frozen"],
                rank_dense=dense_ranks.get(str(row["id"])),
                rank_fts=fts_ranks.get(str(row["id"])),
                vector=vectors.get(str(row["id"])),
            )
            for row in rows
        ]

    # ════════════════════════════════════════════════════════════
    # UPDATE / SUPERSESSION
    # ════════════════════════════════════════════════════════════

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
        record = await self.pg.update(
            memory_id=memory_id,
            content=content,
            metadata=metadata,
            importance=importance,
            project_id=project_id,
            confidence=confidence,
            frozen=frozen,
            supersedes=supersedes,
            content_hash=content_hash,
        )
        if record is None:
            return None

        if self._has_qdrant():
            if embedding is not None:
                self.qdrant.update_vector(point_id=memory_id, vector=embedding)
            # content в payload нет (D6) — синхронизируем фильтруемые поля
            payload: dict = {}
            if importance is not None:
                payload["importance"] = importance
            if project_id is not None:
                payload["project_id"] = str(project_id)
            if content_hash is not None:
                payload["content_hash"] = content_hash
            if payload:
                self.qdrant.set_payload(point_id=memory_id, payload=payload)
            if supersedes is not None:
                # Замещённая версия уходит из выдачи фильтром status
                self.qdrant.set_payload(
                    point_id=supersedes, payload={"status": "superseded"}
                )

        return record

    # ════════════════════════════════════════════════════════════
    # DELETE / RETRACT
    # ════════════════════════════════════════════════════════════

    async def delete(self, memory_id: str) -> bool:
        deleted = await self.pg.delete(memory_id)
        if deleted and self._has_qdrant():
            self.qdrant.delete(point_ids=[memory_id])
        return deleted

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
    ) -> int:
        namespace_id = await self._ns_id(namespace)
        if isinstance(namespace_id, _UnknownNamespace):
            return 0
        count = await self.pg.forget_soft(user_id, namespace_id)
        if self._has_qdrant():
            # active_only=False: забвение стирает вектора независимо от статуса
            search_filter = QdrantStore.build_filter(
                user_id=user_id, namespace_id=namespace_id, active_only=False
            )
            if search_filter:
                self.qdrant.delete_by_filter(search_filter)
        return count

    async def archive(self, memory_id: str) -> bool:
        """Отзыв: status='retracted'. Точка остаётся для time-travel, уходит из выдачи."""
        retracted = await self.pg.archive(memory_id)
        if retracted and self._has_qdrant():
            self.qdrant.set_payload(
                point_id=memory_id, payload={"status": "retracted"}
            )
        return retracted

    # ════════════════════════════════════════════════════════════
    # READ (delegate to PG)
    # ════════════════════════════════════════════════════════════

    async def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        return await self.pg.get_by_id(memory_id)

    async def find_by_entity_name(self, entity_name: str) -> MemoryRecord | None:
        return await self.pg.find_by_entity_name(entity_name)

    async def find_by_content_hash(
        self, namespace: str, content_hash: str
    ) -> MemoryRecord | None:
        namespace_id = await self._ns_id(namespace)
        if isinstance(namespace_id, _UnknownNamespace):
            return None
        return await self.pg.find_by_content_hash(namespace, content_hash)

    async def find_by_content_hashes(
        self, ns_uids: list[str], content_hashes: list[str]
    ) -> dict[tuple[str, str], MemoryRecord]:
        """Batch exact-dedup: uid-резолв делает SQL JOIN, без Python round-trip."""
        return await self.pg.find_by_content_hashes(ns_uids, content_hashes)

    async def bump_access(self, memory_ids: list[str]) -> int:
        """Инкремент access-полей выдачи (Фаза 1.2) — батч, вне транзакции чтения."""
        return await self.pg.bump_access(memory_ids)

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
        project_id: str | None = None,
    ) -> MemoryListResult:
        namespace_id = await self._ns_id(namespace)
        if isinstance(namespace_id, _UnknownNamespace):
            return MemoryListResult(items=[], total=0)
        return await self.pg.list(
            user_id=user_id,
            namespace_id=namespace_id,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[MemoryRecord]:
        namespace_id = await self._ns_id(namespace)
        if isinstance(namespace_id, _UnknownNamespace):
            return []
        return await self.pg.recent(
            namespace_id=namespace_id,
            project_id=project_id,
            since=since,
            limit=limit,
        )

    async def get_stats(self, user_id: str | None = None) -> list[MemoryStatsItem]:
        return await self.pg.get_stats(user_id)

    # ════════════════════════════════════════════════════════════
    # PROJECT CONTEXTS («облачко знаний», D9)
    # ════════════════════════════════════════════════════════════

    async def fetch_project_context(
        self, project_id: str, limit_per_ns: int = 15
    ) -> list[dict]:
        return await self.pg.fetch_project_context(project_id, limit_per_ns)

    async def upsert_project_context(
        self,
        project_id: str,
        content: str | None = None,
        sections: dict | None = None,
        granule_count: int = 0,
    ) -> dict:
        return await self.pg.upsert_project_context(
            project_id, content=content, sections=sections, granule_count=granule_count
        )

    async def get_project_context(self, project_id: str) -> dict | None:
        return await self.pg.get_project_context(project_id)

    # ════════════════════════════════════════════════════════════
    # RELATIONS (delegate to PG)
    # ════════════════════════════════════════════════════════════

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
        return await self.pg.add_relation(
            source_id=source_id,
            target_id=target_id,
            target_name=target_name,
            link_type=link_type,
            description=description,
            weight=weight,
            metadata=metadata,
        )

    async def get_relations_by_source(
        self, source_id: str, link_type: str | None = None
    ) -> list[Relation]:
        return await self.pg.get_relations_by_source(source_id, link_type)

    async def get_relations_by_target(
        self, target_id: str, link_type: str | None = None
    ) -> list[Relation]:
        return await self.pg.get_relations_by_target(target_id, link_type)

    async def get_relations(
        self, memory_id: str, link_type: str | None = None
    ) -> RelationListResult:
        return await self.pg.get_relations(memory_id, link_type)

    async def delete_relation(
        self, source_id: str, target_id: str, link_type: str
    ) -> bool:
        return await self.pg.delete_relation(source_id, target_id, link_type)

    async def delete_relations_by_source(self, source_id: str) -> int:
        return await self.pg.delete_relations_by_source(source_id)

    async def find_relations_between(
        self, source_id: str, target_id: str
    ) -> list[Relation]:
        return await self.pg.find_relations_between(source_id, target_id)

    # ════════════════════════════════════════════════════════════
    # GRAPH (delegate to PG)
    # ════════════════════════════════════════════════════════════

    async def traverse(
        self, start_id: str, depth: int = 3, link_types: list[str] | None = None
    ) -> dict:
        return await self.pg.traverse(start_id, depth, link_types)

    async def sync_links_to_relations(self, memory_id: str) -> int:
        return await self.pg.sync_links_to_relations(memory_id)

    async def sync_links_batch(self, memory_ids: list[str]) -> int:
        return await self.pg.sync_links_batch(memory_ids)

    async def get_graph_stats(self) -> GraphStats:
        return await self.pg.get_graph_stats()
