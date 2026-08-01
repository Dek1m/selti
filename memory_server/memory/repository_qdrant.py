from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import asyncpg
from qdrant_client import QdrantClient
from qdrant_client import models as qm

from memory_server.db import queries as q
from memory_server.metrics import (
    QDRANT_OPS_TOTAL,
    QDRANT_OPS_DURATION_SECONDS,
    QDRANT_SEARCH_RESULTS,
)
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    MemoryStatsItem,
    Relation,
    RelationListResult,
    SearchResult,
    TraverseResult,
)

logger = logging.getLogger(__name__)


class MemoryRepository:
    """Data access layer for memory records.

    PostgreSQL хранит: id, user_id, content, metadata, namespace, importance, timestamps
    Qdrant хранит:     id (point), vector (4096-dim), payload (фильтры)
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        qdrant: QdrantClient | None = None,
        qdrant_collection: str = "memories",
    ):
        self.pool = pool
        self.qdrant = qdrant
        self.qdrant_collection = qdrant_collection

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
        """Создать новую запись.

        Если Qdrant доступен — вектор идёт в Qdrant, метаданные в PG.
        Если Qdrant недоступен — fallback на старый паттерн (embedding в PG).
        """
        metadata = metadata or {}

        # ── PostgreSQL: вставка метаданных ──
        if self._has_qdrant():
            # Новый паттерн: без embedding колонки
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    q.INSERT_MEMORY,
                    user_id,
                    content,
                    metadata,
                    namespace,
                    namespace_id or "",
                    content_hash,
                    importance,
                )
                memory_id = str(row["id"])

            # ── Qdrant: вставка вектора ──
            if embedding is not None:
                qstart = time.monotonic()
                self.qdrant.upsert(
                    collection_name=self.qdrant_collection,
                    points=[
                        qm.PointStruct(
                            id=memory_id,
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
                    ],
                )
                QDRANT_OPS_TOTAL.labels(operation="upsert").inc()
                QDRANT_OPS_DURATION_SECONDS.labels(operation="upsert").observe(time.monotonic() - qstart)
        else:
            # Fallback: старый паттерн с embedding в PG
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    q.INSERT_MEMORY,
                    user_id,
                    content,
                    embedding,
                    metadata,
                    namespace,
                    namespace_id or "",
                    content_hash,
                    importance,
                )
                memory_id = str(row["id"])

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
        """Batch insert. Вектора → Qdrant, метаданные → PG."""
        if importances is None:
            importances = [3] * len(user_ids)

        # ── PostgreSQL: batch insert метаданных ──
        if self._has_qdrant():
            # embeddings игнорируются для PG (они в Qdrant)
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    q.INSERT_MEMORY_BATCH,
                    user_ids,
                    contents,
                    metadatas,
                    namespaces,
                    namespace_ids,
                    content_hashes,
                    importances,
                )
                memory_ids = [str(row["id"]) for row in rows]
        else:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    q.INSERT_MEMORY_BATCH,
                    user_ids,
                    contents,
                    metadatas,
                    namespaces,
                    namespace_ids,
                    content_hashes,
                    importances,
                )
                memory_ids = [str(row["id"]) for row in rows]

        # ── Qdrant: batch upsert векторов ──
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
                            "metadata": metadatas[i],
                            "importance": importances[i],
                            "content_hash": content_hashes[i],
                        },
                    )
                )
            if points:
                qstart = time.monotonic()
                # Qdrant batch upsert (max 1000 per request)
                batch_size = 1000
                for start in range(0, len(points), batch_size):
                    self.qdrant.upsert(
                        collection_name=self.qdrant_collection,
                        points=points[start : start + batch_size],
                    )
                QDRANT_OPS_TOTAL.labels(operation="batch_upsert").inc()
                QDRANT_OPS_DURATION_SECONDS.labels(operation="batch_upsert").observe(time.monotonic() - qstart)

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
        """Векторный поиск.

        Qdrant ищет по вектору с фильтрами.
        Результаты: [{id, score}] → fetch metadata из PG.
        """
        if self._has_qdrant():
            # ── Qdrant: vector search с фильтрами ──
            must_conditions: list[qm.Condition] = []
            if user_id:
                must_conditions.append(
                    qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id))
                )
            if namespace:
                must_conditions.append(
                    qm.FieldCondition(key="namespace", match=qm.MatchValue(value=namespace))
                )

            search_filter = qm.Filter(must=must_conditions) if must_conditions else None

            qstart = time.monotonic()
            qdrant_results = self.qdrant.query_points(
                collection_name=self.qdrant_collection,
                query=query_embedding,
                query_filter=search_filter,
                limit=limit,
                score_threshold=threshold,
            )
            QDRANT_OPS_TOTAL.labels(operation="search").inc()
            QDRANT_OPS_DURATION_SECONDS.labels(operation="search").observe(time.monotonic() - qstart)
            QDRANT_SEARCH_RESULTS.observe(len(qdrant_results.points))

            if not qdrant_results.points:
                return []

            # Извлекаем IDs и scores
            ids = [r.id for r in qdrant_results.points]
            scores = {r.id: r.score for r in qdrant_results.points}

            # ── PostgreSQL: fetch metadata по IDs ──
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT id, user_id, content, metadata, namespace, importance,
                              created_at, updated_at, content_hash
                       FROM memories
                       WHERE id = ANY($1::uuid[]) AND is_archived = false""",
                    ids,
                )

            # Сохраняем порядок из Qdrant (релевантность)
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
            # ── Fallback: SQL FTS sequential scan ──
            if not query_text:
                return []
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    q.SEARCH_MEMORIES,
                    query_text,
                    user_id,
                    namespace,
                    limit,
                )
                return [
                    SearchResult(
                        id=str(row["id"]),
                        content=row["content"],
                        metadata=row["metadata"] or {},
                        importance=row["importance"],
                        score=float(row["score"]),
                    )
                    for row in rows
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
        """Обновить запись.

        Если content изменился + Qdrant — обновляем и вектор.
        """
        if self._has_qdrant():
            # ── PostgreSQL: обновление метаданных ──
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    q.UPDATE_MEMORY,
                    memory_id,
                    content,
                    metadata,
                    importance,
                )

            # ── Qdrant: обновление вектора (если content изменился) ──
            if embedding is not None:
                qstart = time.monotonic()
                # Обновляем и вектор, и payload
                self.qdrant.update_vectors(
                    collection_name=self.qdrant_collection,
                    points=[
                        qm.PointVectors(
                            id=memory_id,
                            vector=embedding,
                        )
                    ],
                )
                # Обновляем payload (content мог измениться)
                if content is not None:
                    self.qdrant.set_payload(
                        collection_name=self.qdrant_collection,
                        payload={
                            "content": content,
                            **(metadata or {}),
                            **({"importance": importance} if importance is not None else {}),
                        },
                        points=[memory_id],
                    )
                QDRANT_OPS_TOTAL.labels(operation="upsert").inc()
                QDRANT_OPS_DURATION_SECONDS.labels(operation="upsert").observe(time.monotonic() - qstart)
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    q.UPDATE_MEMORY,
                    memory_id,
                    content,
                    embedding,
                    metadata,
                    importance,
                )

        if row is None:
            return None
        return MemoryRecord(
            id=str(row["id"]),
            user_id=row["user_id"],
            content=row["content"],
            metadata=row["metadata"] or {},
            namespace=row["namespace"],
            importance=row["importance"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content_hash=row["content_hash"],
        )

    # ════════════════════════════════════════════════════════════
    # DELETE
    # ════════════════════════════════════════════════════════════

    async def delete(self, memory_id: str) -> bool:
        """Удалить запись из PG и Qdrant."""
        if self._has_qdrant():
            # PostgreSQL
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(q.DELETE_MEMORY, memory_id)

            # Qdrant
            if row is not None:
                qstart = time.monotonic()
                self.qdrant.delete(
                    collection_name=self.qdrant_collection,
                    points_selector=qm.PointIdsList(points=[memory_id]),
                )
                QDRANT_OPS_TOTAL.labels(operation="delete").inc()
                QDRANT_OPS_DURATION_SECONDS.labels(operation="delete").observe(time.monotonic() - qstart)

            return row is not None
        else:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(q.DELETE_MEMORY, memory_id)
            return row is not None

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
    ) -> int:
        """Удалить все записи пользователя (с optional namespace фильтром)."""
        # Сначала получаем IDs для удаления из Qdrant
        if self._has_qdrant():
            must_conditions = [
                qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
            ]
            if namespace:
                must_conditions.append(
                    qm.FieldCondition(key="namespace", match=qm.MatchValue(value=namespace))
                )

            # Scroll through Qdrant to get all matching IDs
            ids_to_delete = []
            offset = None
            while True:
                result = self.qdrant.scroll(
                    collection_name=self.qdrant_collection,
                    scroll_filter=qm.Filter(must=must_conditions),
                    limit=1000,
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
                points, next_offset = result
                ids_to_delete.extend(p.id for p in points)
                if next_offset is None:
                    break
                offset = next_offset

            # PostgreSQL: DELETE
            async with self.pool.acquire() as conn:
                result = await conn.execute(q.FORGET_MEMORIES, user_id, namespace)
                count = int(result.split()[-1])

            # Qdrant: batch delete
            if ids_to_delete:
                qstart = time.monotonic()
                batch_size = 1000
                for start in range(0, len(ids_to_delete), batch_size):
                    self.qdrant.delete(
                        collection_name=self.qdrant_collection,
                        points_selector=qm.PointIdsList(
                            points=ids_to_delete[start : start + batch_size]
                        ),
                    )
                QDRANT_OPS_TOTAL.labels(operation="delete").inc()
                QDRANT_OPS_DURATION_SECONDS.labels(operation="delete").observe(time.monotonic() - qstart)

            return count
        else:
            async with self.pool.acquire() as conn:
                result = await conn.execute(q.FORGET_MEMORIES, user_id, namespace)
                return int(result.split()[-1])

    # ════════════════════════════════════════════════════════════
    # READ ONLY (без изменений)
    # ════════════════════════════════════════════════════════════

    async def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.SELECT_MEMORY_BY_ID, memory_id)
            if row is None:
                return None
            return MemoryRecord(
                id=str(row["id"]),
                user_id=row["user_id"],
                content=row["content"],
                metadata=row["metadata"] or {},
                namespace=row["namespace"],
                importance=row["importance"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                content_hash=row["content_hash"],
            )

    async def find_by_content_hash(
        self, namespace: str, content_hash: str
    ) -> MemoryRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.SELECT_MEMORY_BY_CONTENT_HASH, namespace, content_hash
            )
            if row is None:
                return None
            return MemoryRecord(
                id=str(row["id"]),
                user_id=row["user_id"],
                content=row["content"],
                metadata=row["metadata"] or {},
                namespace=row["namespace"],
                importance=row["importance"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                content_hash=row["content_hash"],
            )

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.LIST_MEMORIES, user_id, namespace, limit, offset)
            total_row = await conn.fetchrow(q.COUNT_MEMORIES, user_id, namespace)
            items = [
                MemoryRecord(
                    id=str(row["id"]),
                    user_id=row["user_id"],
                    content=row["content"],
                    metadata=row["metadata"] or {},
                    namespace=row["namespace"],
                    importance=row["importance"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    content_hash=row["content_hash"],
                )
                for row in rows
            ]
            total = total_row[0]
            return MemoryListResult(items=items, total=total)

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.RECENT_MEMORIES, namespace, since, limit)
            return [
                MemoryRecord(
                    id=str(row["id"]),
                    user_id=row["user_id"],
                    content=row["content"],
                    metadata=row["metadata"] or {},
                    namespace=row["namespace"],
                    importance=row["importance"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    content_hash=row["content_hash"],
                )
                for row in rows
            ]

    async def get_stats(self, user_id: str | None = None) -> list[MemoryStatsItem]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.MEMORY_STATS, user_id)
            return [
                MemoryStatsItem(
                    namespace=row["namespace"],
                    count=row["count"],
                    last_updated=row["last_updated"],
                )
                for row in rows
            ]

    async def archive(self, memory_id: str) -> bool:
        """Мягкое удаление: установить is_archived = true."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.ARCHIVE_MEMORY, memory_id)
            if row is not None and self._has_qdrant():
                # Архивируем и в Qdrant
                qstart = time.monotonic()
                self.qdrant.delete(
                    collection_name=self.qdrant_collection,
                    points_selector=qm.PointIdsList(points=[memory_id]),
                )
                QDRANT_OPS_TOTAL.labels(operation="delete").inc()
                QDRANT_OPS_DURATION_SECONDS.labels(operation="delete").observe(time.monotonic() - qstart)
            return row is not None

    # ── Relations (без изменений — только PG) ──

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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.INSERT_RELATION,
                source_id,
                target_id,
                target_name,
                link_type,
                description,
                weight,
                metadata or {},
            )
            return str(row["id"])

    async def get_relations_by_source(
        self, source_id: str, link_type: str | None = None
    ) -> list[Relation]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SELECT_RELATIONS_BY_SOURCE, source_id, link_type)
            return [
                Relation(
                    id=str(row["id"]),
                    source_id=str(row["source_id"]),
                    target_id=str(row["target_id"]) if row["target_id"] else None,
                    target_name=row["target_name"],
                    link_type=row["link_type"],
                    description=row["description"],
                    weight=float(row["weight"]),
                    metadata=row["metadata"] or {},
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_relations_by_target(
        self, target_id: str, link_type: str | None = None
    ) -> list[Relation]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SELECT_RELATIONS_BY_TARGET, target_id, link_type)
            return [
                Relation(
                    id=str(row["id"]),
                    source_id=str(row["source_id"]),
                    target_id=str(row["target_id"]) if row["target_id"] else None,
                    target_name=row["target_name"],
                    link_type=row["link_type"],
                    description=row["description"],
                    weight=float(row["weight"]),
                    metadata=row["metadata"] or {},
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def delete_relation(
        self, source_id: str, target_id: str, link_type: str
    ) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.DELETE_RELATION, source_id, target_id, link_type
            )
            return row is not None

    async def delete_relations_by_source(self, source_id: str) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute(q.DELETE_RELATIONS_BY_SOURCE, source_id)
            return int(result.split()[-1])

    async def find_relations_between(
        self, source_id: str, target_id: str
    ) -> list[Relation]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.FIND_RELATIONS_BETWEEN, source_id, target_id)
            return [
                Relation(
                    id=str(row["id"]),
                    source_id=str(row["source_id"]),
                    target_id=str(row["target_id"]) if row["target_id"] else None,
                    target_name=row["target_name"],
                    link_type=row["link_type"],
                    description=row["description"],
                    weight=float(row["weight"]),
                    metadata=row["metadata"] or {},
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def traverse(
        self, start_id: str, depth: int = 3, link_types: list[str] | None = None
    ) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.TRAVERSE_CTE, start_id, depth, link_types)
            return [
                {"node_id": str(row["node_id"]), "depth": row["depth"]}
                for row in rows
            ]

    async def sync_links_to_relations(self, memory_id: str) -> int:
        """Синхронизировать metadata.links → relations для одной гранулы."""
        async with self.pool.acquire() as conn:
            await conn.execute(q.DELETE_SYNCED_RELATIONS, memory_id)
            rows = await conn.fetch(q.BACKFILL_RELATIONS_FROM_METADATA, memory_id)
            return len(rows)

    async def sync_links_batch(self, memory_ids: list[str]) -> int:
        """Batch-синхронизация metadata.links → relations для списка гранул."""
        if not memory_ids:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SYNC_LINKS_BATCH, memory_ids)
            return len(rows)

    async def get_graph_stats(self) -> GraphStats:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.GRAPH_STATS)
            total_granules = row["total_granules"]
            total_relations = row["total_relations"]
            linked_granules = row["linked_granules"]
            orphans = row["orphans"]
            avg = (total_relations * 2 / total_granules) if total_granules > 0 else 0.0

            ns_rows = await conn.fetch(q.GRAPH_STATS_BY_NAMESPACE)
            by_namespace = {
                r["namespace"]: {
                    "total": r["total"],
                    "linked": r["linked"],
                    "orphans": r["orphans"],
                }
                for r in ns_rows
            }

            lt_rows = await conn.fetch(q.GRAPH_STATS_BY_LINK_TYPE)
            by_link_type = {r["link_type"]: r["cnt"] for r in lt_rows}

            return GraphStats(
                total_granules=total_granules,
                total_relations=total_relations,
                linked_granules=linked_granules,
                orphans=orphans,
                avg_connections=round(avg, 2),
                by_namespace=by_namespace,
                by_link_type=by_link_type,
            )
