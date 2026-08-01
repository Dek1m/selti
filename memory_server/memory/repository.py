"""MemoryRepository facade — координирует PostgreSQL + Qdrant.

Реализует MemoryRepositoryProtocol.
Делегирует PG-операции → PostgreSQLRepository, Qdrant-операции → QdrantStore.
"""
from __future__ import annotations

from datetime import datetime

import structlog
from qdrant_client import models as qm

from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.memory.qdrant_store import QdrantStore
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    MemoryStatsItem,
    Relation,
    RelationListResult,
    SearchResult,
)

logger = structlog.get_logger()


class MemoryRepository:
    """Facade: PostgreSQL (метаданные) + Qdrant (вектора).

    Внешний API совпадает с MemoryRepositoryProtocol.
    """

    def __init__(
        self,
        pg: PostgreSQLRepository,
        qdrant: QdrantStore | None = None,
    ):
        self.pg = pg
        self.qdrant = qdrant

    def _has_qdrant(self) -> bool:
        return self.qdrant is not None

    # ════════════════════════════════════════════════════════════
    # INSERT
    # ════════════════════════════════════════════════════════════

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
        metadata = metadata or {}

        memory_id = await self.pg.insert(
            user_id=user_id,
            content=content,
            metadata=metadata,
            namespace=namespace,
            namespace_id=namespace_id,
            content_hash=content_hash,
            importance=importance,
        )

        if self._has_qdrant() and embedding is not None:
            self.qdrant.upsert_vector(
                point_id=memory_id,
                vector=embedding,
                payload={
                    "user_id": user_id,
                    "content": content,
                    "namespace": namespace,
                    "metadata": metadata,
                    "importance": importance,
                    "content_hash": content_hash,
                },
            )

        return memory_id

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
        if importances is None:
            importances = [3] * len(user_ids)

        memory_ids = await self.pg.insert_batch(
            user_ids=user_ids,
            contents=contents,
            namespaces=namespaces,
            namespace_ids=namespace_ids,
            content_hashes=content_hashes,
            metadatas=metadatas,
            importances=importances,
        )

        if self._has_qdrant() and embeddings is not None:
            points = []
            for i, mid in enumerate(memory_ids):
                emb = embeddings[i] if isinstance(embeddings[i], list) else None
                if emb is None:
                    continue
                points.append(
                    qm.PointStruct(
                        id=mid,
                        vector=emb,
                        payload={
                            "user_id": user_ids[i],
                            "content": contents[i],
                            "namespace": namespaces[i],
                            "metadata": metadatas[i] if metadatas else {},
                            "importance": importances[i],
                            "content_hash": content_hashes[i],
                        },
                    )
                )
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
    ) -> list[SearchResult]:
        if self._has_qdrant():
            search_filter = QdrantStore.build_filter(user_id=user_id, namespace=namespace)
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

            rows = await self.pg.fetch_by_ids(ids)
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
                        )
                    )
            return results
        else:
            if not query_text:
                return []
            rows = await self.pg.search_fts(
                query_text=query_text,
                user_id=user_id,
                namespace=namespace,
                limit=limit,
            )
            return [
                SearchResult(
                    id=r["id"],
                    content=r["content"],
                    metadata=r["metadata"],
                    importance=r["importance"],
                    score=r["score"],
                )
                for r in rows
            ]

    # ════════════════════════════════════════════════════════════
    # UPDATE
    # ════════════════════════════════════════════════════════════

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        embedding: list[float] | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
    ) -> MemoryRecord | None:
        row = await self.pg.update(
            memory_id=memory_id,
            content=content,
            metadata=metadata,
            importance=importance,
        )

        if self._has_qdrant() and embedding is not None:
            self.qdrant.update_vector(point_id=memory_id, vector=embedding)
            if content is not None:
                payload: dict = {"content": content}
                if metadata:
                    payload.update(metadata)
                if importance is not None:
                    payload["importance"] = importance
                self.qdrant.set_payload(point_id=memory_id, payload=payload)

        return row

    # ════════════════════════════════════════════════════════════
    # DELETE
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
        count = await self.pg.forget_soft(user_id, namespace)
        if self._has_qdrant():
            search_filter = QdrantStore.build_filter(user_id=user_id, namespace=namespace)
            if search_filter:
                self.qdrant.delete_by_filter(search_filter)
        return count

    async def archive(self, memory_id: str) -> bool:
        archived = await self.pg.archive(memory_id)
        if archived and self._has_qdrant():
            self.qdrant.delete(point_ids=[memory_id])
        return archived

    # ════════════════════════════════════════════════════════════
    # READ (delegate to PG)
    # ════════════════════════════════════════════════════════════

    async def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        return await self.pg.get_by_id(memory_id)

    async def find_by_content_hash(
        self, namespace: str, content_hash: str
    ) -> MemoryRecord | None:
        return await self.pg.find_by_content_hash(namespace, content_hash)

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        return await self.pg.list(user_id=user_id, namespace=namespace, limit=limit, offset=offset)

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        return await self.pg.recent(namespace=namespace, since=since, limit=limit)

    async def get_stats(self, user_id: str | None = None) -> list[MemoryStatsItem]:
        return await self.pg.get_stats(user_id)

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
