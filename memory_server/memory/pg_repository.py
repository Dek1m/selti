"""PostgreSQL-only repository for memory records.

Хранит: метаданные, контент, связи, граф.
НЕ хранит вектора — для этого QdrantStore.
"""
from __future__ import annotations

from datetime import datetime

import asyncpg
import structlog

from memory_server.db import queries as q
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    MemoryStatsItem,
    Relation,
    RelationListResult,
)

logger = structlog.get_logger()


class PostgreSQLRepository:
    """Data access layer for PostgreSQL — метаданные, связи, граф."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ════════════════════════════════════════════════════════════
    # INSERT
    # ════════════════════════════════════════════════════════════

    async def insert(
        self,
        user_id: str,
        content: str,
        metadata: dict | None = None,
        namespace: str = "default",
        namespace_id: str | None = None,
        content_hash: str | None = None,
        importance: int = 3,
    ) -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.INSERT_MEMORY,
                user_id,
                content,
                metadata or {},
                namespace,
                namespace_id or "",
                content_hash,
                importance,
            )
            return str(row["id"])

    async def insert_batch(
        self,
        user_ids: list[str],
        contents: list[str],
        namespaces: list[str],
        namespace_ids: list[str],
        content_hashes: list[str | None],
        metadatas: list[dict] | None = None,
        importances: list[int] | None = None,
    ) -> list[str]:
        if importances is None:
            importances = [3] * len(user_ids)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.INSERT_MEMORY_BATCH,
                user_ids,
                contents,
                metadatas or [{}] * len(user_ids),
                namespaces,
                namespace_ids,
                content_hashes,
                importances,
            )
            return [str(row["id"]) for row in rows]

    # ════════════════════════════════════════════════════════════
    # SEARCH (SQL FTS fallback)
    # ════════════════════════════════════════════════════════════

    async def search_fts(
        self,
        query_text: str,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 10,
    ) -> list:
        """Full-text search fallback — когда Qdrant недоступен."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.SEARCH_MEMORIES,
                query_text,
                user_id,
                namespace,
                limit,
            )
            return [
                {
                    "id": str(row["id"]),
                    "content": row["content"],
                    "metadata": row["metadata"] or {},
                    "importance": row["importance"],
                    "score": float(row["score"]),
                }
                for row in rows
            ]

    # ════════════════════════════════════════════════════════════
    # READ
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

    async def fetch_by_ids(self, ids: list[str]) -> list[dict]:
        """Batch fetch metadata по IDs (для Qdrant search results)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, user_id, content, metadata, namespace, importance,
                          created_at, updated_at, content_hash
                   FROM memories
                   WHERE id = ANY($1::uuid[]) AND is_archived = false""",
                ids,
            )
            return [dict(row) for row in rows]

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.LIST_WITH_COUNT, user_id, namespace, limit, offset)
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
            total = rows[0]["total_count"] if rows else 0
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

    # ════════════════════════════════════════════════════════════
    # UPDATE
    # ════════════════════════════════════════════════════════════

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
    ) -> MemoryRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.UPDATE_MEMORY,
                memory_id,
                content,
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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.DELETE_MEMORY, memory_id)
            return row is not None

    async def archive(self, memory_id: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.ARCHIVE_MEMORY, memory_id)
            return row is not None

    async def forget_soft(self, user_id: str, namespace: str | None = None) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(q.MEMORY_FORGET_SOFT, user_id, namespace)

    # ════════════════════════════════════════════════════════════
    # RELATIONS
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

    async def get_relations(
        self, memory_id: str, link_type: str | None = None
    ) -> RelationListResult:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.GET_RELATIONS_UNIFIED, memory_id, link_type)

        outgoing: list[Relation] = []
        incoming: list[Relation] = []
        for row in rows:
            rel = Relation(
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
            if row["direction"] == "outgoing":
                outgoing.append(rel)
            else:
                incoming.append(rel)
        return RelationListResult(incoming=incoming, outgoing=outgoing)

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

    # ════════════════════════════════════════════════════════════
    # GRAPH
    # ════════════════════════════════════════════════════════════

    async def traverse(
        self, start_id: str, depth: int = 3, link_types: list[str] | None = None
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.TRAVERSE_FULL, start_id, depth, link_types or None)
            if row is None:
                return {"nodes": [], "edges": []}
            return {"nodes": row["nodes"] or [], "edges": row["edges"] or []}

    async def sync_links_to_relations(self, memory_id: str) -> int:
        async with self.pool.acquire() as conn:
            await conn.execute(q.DELETE_SYNCED_RELATIONS, memory_id)
            rows = await conn.fetch(q.BACKFILL_RELATIONS_FROM_METADATA, memory_id)
            return len(rows)

    async def sync_links_batch(self, memory_ids: list[str]) -> int:
        if not memory_ids:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SYNC_LINKS_BATCH, memory_ids)
            return len(rows)

    async def get_graph_stats(self) -> GraphStats:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.GRAPH_STATS_UNIFIED)
            total_granules = row["p_total_granules"]
            total_relations = row["p_total_relations"]
            linked_granules = row["p_linked_granules"]
            orphans = row["p_orphans"]
            avg = (total_relations * 2 / total_granules) if total_granules > 0 else 0.0
            return GraphStats(
                total_granules=total_granules,
                total_relations=total_relations,
                linked_granules=linked_granules,
                orphans=orphans,
                avg_connections=round(avg, 2),
                by_namespace=row["p_by_namespace"] or {},
                by_link_type=row["p_by_link_type"] or {},
            )
