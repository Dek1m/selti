from __future__ import annotations

from datetime import datetime

import asyncpg

from memory_server.db import queries as q


class HashRepository:
    """Data access layer for resource_hashes."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def upsert(
        self,
        source_type: str,
        source_id: str,
        content_hash: str,
        size_bytes: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """UPSERT хеша. Возвращает {id, created_at, updated_at}."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.UPSERT_RESOURCE_HASH,
                source_type,
                source_id,
                content_hash,
                size_bytes,
                metadata or {},
            )
            return dict(row)

    async def get(self, source_type: str, source_id: str) -> dict | None:
        """Точный поиск по source_type + source_id."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.SELECT_RESOURCE_HASH,
                source_type,
                source_id,
            )
            return dict(row) if row else None

    async def list(
        self,
        source_type: str | None = None,
        updated_since: datetime | None = None,
        project: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Список с фильтрами."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.LIST_RESOURCE_HASHES,
                source_type,
                updated_since,
                project,
                limit,
                offset,
            )
            return [dict(r) for r in rows]

    async def delete(self, source_type: str, source_id: str) -> str | None:
        """Удаление. Возвращает ID удалённой записи или None."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.DELETE_RESOURCE_HASH,
                source_type,
                source_id,
            )
            return row["id"] if row else None
