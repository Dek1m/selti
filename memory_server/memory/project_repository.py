"""Реестр проектов: slug/UUID → project_id (миграция 017, решения D1/D2).

Кеш TTL-стилем по образцу NamespaceRepository: slug'и реестра меняются
редко, но резолв нужен на каждом store/search с project_id. Оба индекса
кеша (по slug и по id) заполняет любой точечный запрос.
"""
from __future__ import annotations

import uuid
from typing import NamedTuple

import asyncpg
from cachetools import TTLCache

from memory_server.exceptions import NotFoundError
from memory_server.logger import get_logger

logger = get_logger(__name__)

_PROJECT_COLUMNS = "id::text, slug, name, kind, status, local_path"


class ProjectRecord(NamedTuple):
    """Карточка проекта из реестра."""

    id: str
    slug: str
    name: str
    kind: str
    status: str
    local_path: str | None = None


class ProjectRepository:
    """Read-доступ к реестру projects + резолв идентификаторов.

    CRUD реестра — Фаза 3/5 (web); здесь резолв «selti»/UUID → project_id,
    стек проекта (project_technologies + project_links, Фаза 6) и список
    реестра (beat rebuild_contexts, REST /projects, ZCode-хук).
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._cache: TTLCache[str, ProjectRecord] = TTLCache(maxsize=64, ttl=300)
        self._by_id: TTLCache[str, ProjectRecord] = TTLCache(maxsize=64, ttl=300)

    def _record(self, row) -> ProjectRecord:
        rec = ProjectRecord(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            kind=row["kind"],
            status=row["status"],
            local_path=row["local_path"],
        )
        self._cache[rec.slug] = rec
        self._by_id[rec.id] = rec
        return rec

    async def get_by_slug(self, slug: str) -> ProjectRecord | None:
        if slug in self._cache:
            return self._cache[slug]
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE slug = $1", slug
            )
            if row is None:
                return None
            return self._record(row)

    async def get_by_id(self, project_id: str) -> ProjectRecord | None:
        """project_id UUID → карточка проекта (dirty-флаг: UUID → slug)."""
        if project_id in self._by_id:
            return self._by_id[project_id]
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = $1::uuid",
                project_id,
            )
            if row is None:
                return None
            return self._record(row)

    async def list_all(self) -> list[ProjectRecord]:
        """Весь реестр (beat rebuild_contexts, REST /projects)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_PROJECT_COLUMNS} FROM projects ORDER BY slug"
            )
            return [self._record(row) for row in rows]

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

    async def fetch_stack(self, project_id: str) -> dict[str, list[dict]]:
        """Стек проекта: project_technologies + project_links (Фаза 6, D9).

        Простые SELECT по project_id (миграция 017) — миграций не требуют.
        """
        async with self.pool.acquire() as conn:
            technologies = await conn.fetch(
                """
                SELECT t.name, t.category, pt.version, pt.purpose
                FROM project_technologies pt
                JOIN technologies t ON t.id = pt.technology_id
                WHERE pt.project_id = $1::uuid
                ORDER BY t.category, t.name
                """,
                project_id,
            )
            links = await conn.fetch(
                """
                SELECT link_type, url, title
                FROM project_links
                WHERE project_id = $1::uuid
                ORDER BY link_type, url
                """,
                project_id,
            )
        return {
            "technologies": [dict(row) for row in technologies],
            "links": [dict(row) for row in links],
        }
