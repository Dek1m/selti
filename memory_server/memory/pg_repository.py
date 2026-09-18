"""PostgreSQL-only repository for memory records.

Хранит: метаданные, контент, связи, граф, контексты проектов.
НЕ хранит вектора — для этого QdrantStore.

Контракт волны 2 (Фаза 0.4): namespace приходит как namespace_id UUID
(резолв имени → id делает фасад через NamespaceRepository), актуальность
гранулы = status='asserted' AND valid_to IS NULL.
"""
from __future__ import annotations

from datetime import datetime

import asyncpg

from memory_server.db import queries as q
from memory_server.logger import get_logger
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    MemoryStatsItem,
    Relation,
    RelationListResult,
)

logger = get_logger(__name__)


class PostgreSQLRepository:
    """Data access layer for PostgreSQL — метаданные, связи, граф, контексты."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @staticmethod
    def _to_record(row: asyncpg.Record) -> MemoryRecord:
        """Row канонической проекции (_MEMORY_COLUMNS) → MemoryRecord."""
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
            project_id=row["project_id"],
            status=row["status"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            ingested_at=row["ingested_at"],
            confidence=row["confidence"],
            supersedes=row["supersedes"],
            superseded_by=row["superseded_by"],
            frozen=row["frozen"],
        )

    # ════════════════════════════════════════════════════════════
    # INSERT
    # ════════════════════════════════════════════════════════════

    async def insert(
        self,
        user_id: str,
        content: str,
        metadata: dict | None = None,
        namespace_id: str | None = None,
        content_hash: str | None = None,
        importance: int = 3,
        project_id: str | None = None,
        confidence: float | None = None,
        frozen: bool = False,
        supersedes: str | None = None,
    ) -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.INSERT_MEMORY,
                user_id,
                content,
                metadata or {},
                namespace_id,
                content_hash,
                importance,
                project_id,
                confidence,
                frozen,
                supersedes,
            )
            return str(row["id"])

    async def insert_batch(
        self,
        user_ids: list[str],
        contents: list[str],
        namespace_ids: list[str],
        content_hashes: list[str | None],
        project_ids: list[str | None],
        metadatas: list[dict] | None = None,
        importances: list[int] | None = None,
    ) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.INSERT_MEMORY_BATCH,
                user_ids,
                contents,
                metadatas or [{}] * len(user_ids),
                namespace_ids,
                content_hashes,
                importances or [3] * len(user_ids),
                project_ids,
            )
            return [str(row["id"]) for row in rows]

    # ════════════════════════════════════════════════════════════
    # SEARCH (SQL FTS fallback)
    # ════════════════════════════════════════════════════════════

    async def search_fts(
        self,
        query_text: str,
        user_id: str | None = None,
        namespace_id: str | None = None,
        project_id: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Full-text search fallback — когда Qdrant недоступен."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.SEARCH_MEMORIES,
                query_text,
                user_id,
                namespace_id,
                project_id,
                limit,
            )
            return [
                {
                    "id": str(row["id"]),
                    "content": row["content"],
                    "metadata": row["metadata"] or {},
                    "namespace": row["namespace"],
                    "importance": row["importance"],
                    "project_id": row["project_id"],
                    "status": row["status"],
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
            return self._to_record(row) if row is not None else None

    async def find_by_entity_name(self, entity_name: str) -> MemoryRecord | None:
        """Найти гранулу по entity_name в metadata (fallback для строковых ID)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.SELECT_MEMORY_BY_ENTITY_NAME, entity_name)
            return self._to_record(row) if row is not None else None

    async def find_by_content_hash(
        self, namespace: str, content_hash: str
    ) -> MemoryRecord | None:
        """Exact-dedup lookup: uid → namespace_id резолвит БД (JOIN по UNIQUE-индексу)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.SELECT_MEMORY_BY_CONTENT_HASH, namespace, content_hash
            )
            return self._to_record(row) if row is not None else None

    async def fetch_by_ids(self, ids: list[str]) -> list[dict]:
        """Batch fetch метаданных по IDs (для Qdrant-выдачи).

        Фильтр актуальности (status/valid_to) применён в SQL — ретрактнутые
        гранулы не съедают лимит выдачи.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.FETCH_MEMORIES_BY_IDS, ids)
            return [dict(row) for row in rows]

    async def list(
        self,
        user_id: str | None = None,
        namespace_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.LIST_MEMORIES, user_id, namespace_id, project_id, limit, offset
            )
            items = [self._to_record(row) for row in rows]
            total = rows[0]["total_count"] if rows else 0
            return MemoryListResult(items=items, total=total)

    async def recent(
        self,
        namespace_id: str | None = None,
        project_id: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.RECENT_MEMORIES, namespace_id, project_id, since, limit
            )
            return [self._to_record(row) for row in rows]

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
    # UPDATE / SUPERSESSION
    # ════════════════════════════════════════════════════════════

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
        project_id: str | None = None,
        confidence: float | None = None,
        frozen: bool | None = None,
        supersedes: str | None = None,
    ) -> MemoryRecord | None:
        """Обновление гранулы: metadata merge-ится (dict-merge), version бампит триггер БД.

        При передаче supersedes закрывает старую гранулу атомарно (одна транзакция):
        status='superseded', valid_to=valid_from новой, superseded_by=memory_id.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    q.UPDATE_MEMORY,
                    memory_id,
                    content,
                    metadata,
                    importance,
                    project_id,
                    confidence,
                    frozen,
                    supersedes,
                )
                if row is None:
                    return None
                if supersedes is not None:
                    await conn.fetchrow(q.SUPERSEDE_MEMORY, supersedes, memory_id)
        return self._to_record(row)

    # ════════════════════════════════════════════════════════════
    # DELETE / RETRACT
    # ════════════════════════════════════════════════════════════

    async def delete(self, memory_id: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.DELETE_MEMORY, memory_id)
            return row is not None

    async def archive(self, memory_id: str) -> bool:
        """Отзыв гранулы: status='retracted', valid_to=now() (бывший is_archived)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.RETRACT_MEMORY, memory_id)
            return row is not None

    async def forget_soft(self, user_id: str, namespace_id: str | None = None) -> int:
        """Мягкое забвение всех гранул пользователя: status='retracted'."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(q.FORGET_MEMORIES, user_id, namespace_id)

    # ════════════════════════════════════════════════════════════
    # PROJECT CONTEXTS («облачко знаний», D9)
    # ════════════════════════════════════════════════════════════

    async def fetch_project_context(
        self, project_id: str, limit_per_ns: int = 15
    ) -> list[dict]:
        """Топ-гранулы проекта с квотами per namespace (хранимка 019)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.FETCH_PROJECT_CONTEXT, project_id, limit_per_ns)
            return [dict(row) for row in rows]

    async def upsert_project_context(
        self,
        project_id: str,
        content: str | None = None,
        sections: dict | None = None,
        granule_count: int = 0,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.UPSERT_PROJECT_CONTEXT,
                project_id,
                content,
                sections or {},
                granule_count,
            )
            return dict(row)

    async def get_project_context(self, project_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.SELECT_PROJECT_CONTEXT, project_id)
            return dict(row) if row is not None else None

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
            return [self._to_relation(row) for row in rows]

    async def get_relations_by_target(
        self, target_id: str, link_type: str | None = None
    ) -> list[Relation]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SELECT_RELATIONS_BY_TARGET, target_id, link_type)
            return [self._to_relation(row) for row in rows]

    async def get_relations(
        self, memory_id: str, link_type: str | None = None
    ) -> RelationListResult:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.GET_RELATIONS_UNIFIED, memory_id, link_type)

        outgoing: list[Relation] = []
        incoming: list[Relation] = []
        for row in rows:
            rel = self._to_relation(row)
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
            return [self._to_relation(row) for row in rows]

    @staticmethod
    def _to_relation(row: asyncpg.Record) -> Relation:
        return Relation(
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
