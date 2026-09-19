"""Реестр проектов: slug/UUID → project_id (миграция 017, решения D1/D2).

Кеш TTL-стилем по образцу NamespaceRepository: slug'и реестра меняются
редко, но резолв нужен на каждом store/search с project_id.
"""
from __future__ import annotations

import uuid
from typing import NamedTuple

import asyncpg
from cachetools import TTLCache

from memory_server.exceptions import NotFoundError
from memory_server.logger import get_logger

logger = get_logger(__name__)

_PROJECT_COLUMNS = "id::text, slug, name, kind, status"


class ProjectRecord(NamedTuple):
    """Карточка проекта из реестра."""

    id: str
    slug: str
    name: str
    kind: str
    status: str


class ProjectRepository:
    """Read-доступ к реестру projects + резолв идентификаторов.

    CRUD реестра — Фаза 3/5 (web); здесь только то, что нужно пути
    записи/чтения гранул: превратить «selti» или UUID в project_id UUID.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._cache: TTLCache[str, ProjectRecord] = TTLCache(maxsize=64, ttl=300)

    async def get_by_slug(self, slug: str) -> ProjectRecord | None:
        if slug in self._cache:
            return self._cache[slug]
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE slug = $1", slug
            )
            if row is None:
                return None
            rec = ProjectRecord(
                id=row["id"],
                slug=row["slug"],
                name=row["name"],
                kind=row["kind"],
                status=row["status"],
            )
            self._cache[slug] = rec
            return rec

    async def resolve_id(self, key: str | None) -> str | None:
        """Ключ тула (slug | UUID-строка | None) → project_id UUID-строка.

        None → None (глобальный слой, D2). Неизвестный slug → NotFoundError:
        понятная ошибка вместо молчаливой потери привязки.
        """
        if key is None:
            return None
        record = await self.get_by_slug(key)
        if record is not None:
            return record.id
        # Не slug из реестра: единственная допустимая альтернатива — готовый UUID
        try:
            return str(uuid.UUID(key))
        except ValueError:
            raise NotFoundError(
                key,
                message=f"Project not found: '{key}' (neither slug in registry nor UUID)",
            ) from None
