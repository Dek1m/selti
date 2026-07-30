from __future__ import annotations

import logging
from typing import NamedTuple

import asyncpg

logger = logging.getLogger(__name__)


class NamespaceRecord(NamedTuple):
    """Информация о namespace."""
    id: str
    uid: str
    name: str
    description: str


class NamespaceRepository:
    """Реестр namespace-ов с in-memory кэшем.

    При первом обращении к namespace — загружает из БД.
    Если namespace не существует — создаёт (auto-register).
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._cache: dict[str, NamespaceRecord] = {}

    async def _load_all(self) -> None:
        """Загрузить все namespaces из БД в кэш."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, uid, name, description FROM namespaces ORDER BY uid")
            for row in rows:
                rec = NamespaceRecord(
                    id=str(row["id"]),
                    uid=row["uid"],
                    name=row["name"],
                    description=row["description"] or "",
                )
                self._cache[rec.uid] = rec

    async def get_or_create(self, uid: str, name: str | None = None) -> NamespaceRecord:
        """Получить namespace по uid. Если нет — создать автоматически.

        Args:
            uid: строковый ID namespace (snake_case)
            name: отображаемое имя (если None → uid)

        Returns:
            NamespaceRecord с id, uid, name, description
        """
        # Проверяем кэш
        if uid in self._cache:
            return self._cache[uid]

        # Пробуем загрузить из БД
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, uid, name, description FROM namespaces WHERE uid = $1",
                uid,
            )

            if row is None:
                # Auto-register: создаём новый namespace
                display_name = name or uid.replace("_", " ").title()
                row = await conn.fetchrow(
                    """INSERT INTO namespaces (uid, name, description)
                       VALUES ($1, $2, '')
                       ON CONFLICT (uid) DO UPDATE SET uid = EXCLUDED.uid
                       RETURNING id, uid, name, description""",
                    uid,
                    display_name,
                )
                logger.info("Auto-registered namespace", extra={"name": display_name, "uid": uid})

            rec = NamespaceRecord(
                id=str(row["id"]),
                uid=row["uid"],
                name=row["name"],
                description=row["description"] or "",
            )
            self._cache[rec.uid] = rec
            return rec

    async def get_by_uid(self, uid: str) -> NamespaceRecord | None:
        """Получить namespace по uid. Если нет — вернуть None (без auto-register)."""
        if uid in self._cache:
            return self._cache[uid]

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, uid, name, description FROM namespaces WHERE uid = $1",
                uid,
            )
            if row is None:
                return None

            rec = NamespaceRecord(
                id=str(row["id"]),
                uid=row["uid"],
                name=row["name"],
                description=row["description"] or "",
            )
            self._cache[rec.uid] = rec
            return rec

    async def list_all(self) -> list[NamespaceRecord]:
        """Получить все namespaces."""
        if not self._cache:
            await self._load_all()
        return sorted(self._cache.values(), key=lambda r: r.uid)

    async def invalidate(self, uid: str | None = None) -> None:
        """Сбросить кэш для конкретного namespace или всего."""
        if uid:
            self._cache.pop(uid, None)
        else:
            self._cache.clear()
