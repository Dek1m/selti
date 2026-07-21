import asyncpg

from memory_server.db import queries as q
from memory_server.models import MemoryListResult, MemoryRecord, SearchResult


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
    ) -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.INSERT_MEMORY,
                user_id,
                content,
                embedding,
                metadata,
                namespace,
            )
            return row["id"]

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
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    async def search(
        self,
        query_embedding: list[float],
        user_id: str,
        limit: int,
        threshold: float,
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
    ) -> MemoryRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.UPDATE_MEMORY,
                memory_id,
                content,
                embedding,
                metadata,
            )
            if row is None:
                return None
            return MemoryRecord(
                id=str(row["id"]),
                user_id=row["user_id"],
                content=row["content"],
                metadata=row["metadata"] or {},
                namespace=row["namespace"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
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
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]
            total = total_row[0]
            return MemoryListResult(items=items, total=total)

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute(q.FORGET_MEMORIES, user_id, namespace)
            # result format: "DELETE N"
            return int(result.split()[-1])
