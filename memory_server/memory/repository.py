from __future__ import annotations

from datetime import datetime

import asyncpg

from memory_server.db import queries as q
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


class MemoryRepository:
    """Data access layer for memory records."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def insert(
        self,
        user_id: str,
        content: str,
        embedding: list[float],
        metadata: dict,
        namespace: str,
        namespace_id: str,
        content_hash: str | None = None,
        importance: int = 3,
    ) -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.INSERT_MEMORY,
                user_id,
                content,
                embedding,
                metadata,
                namespace,
                namespace_id,
                content_hash,
                importance,
            )
            return row["id"]

async def insert_batch(
        self,
        user_ids: list[str],
        contents: list[str],
        embeddings: list[str],  # text[] — pgvector casts to vector via SQL
        metadatas: list[dict],
        namespaces: list[str],
        namespace_ids: list[str],
        content_hashes: list[str | None],
        importances: list[int] | None = None,
    ) -> list[str]:
        """Batch insert multiple memories in one SQL round-trip.
        
        embeddings are passed as text[] and cast to vector via ::vector in SQL
        to avoid asyncpg's lack of vector[] array codec support.
        """
        if importances is None:
            importances = [3] * len(user_ids)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.INSERT_MEMORY_BATCH,
                user_ids,
                contents,
                embeddings,
                metadatas,
                namespaces,
                namespace_ids,
                content_hashes,
                importances,
            )
            return [str(row["id"]) for row in rows]

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
        self,
        namespace: str,
        content_hash: str,
    ) -> MemoryRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.SELECT_MEMORY_BY_CONTENT_HASH,
                namespace,
                content_hash,
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

    async def search(
        self,
        query_embedding: list[float],
        user_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
        namespace: str | None = None,
    ) -> list[SearchResult]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.SEARCH_MEMORIES,
                query_embedding,
                user_id,
                namespace,
                threshold,
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

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        embedding: list[float] | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
    ) -> MemoryRecord | None:
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

    async def delete(self, memory_id: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.DELETE_MEMORY, memory_id)
            return row is not None

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

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute(q.FORGET_MEMORIES, user_id, namespace)
            return int(result.split()[-1])
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
            return row is not None

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
            return [{"node_id": str(row["node_id"]), "depth": row["depth"]} for row in rows]

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

