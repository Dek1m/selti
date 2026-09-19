"""Реестр проектов: slug/UUID → project_id (миграция 017, решения D1/D2).

Кеш TTL-стилем по образцу NamespaceRepository: slug'и реестра меняются
редко, но резолв нужен на каждом store/search с project_id. Оба индекса
кеша (по slug и по id) заполняет любой точечный запрос.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import NamedTuple

import asyncpg
from cachetools import TTLCache

from memory_server.db import queries as q
from memory_server.exceptions import NotFoundError
from memory_server.logger import get_logger

logger = get_logger(__name__)

_PROJECT_COLUMNS = "id::text, slug, name, description, kind, status, local_path, repo_url"


class ProjectRecord(NamedTuple):
    """Карточка проекта из реестра."""

    id: str
    slug: str
    name: str
    kind: str
    status: str
    local_path: str | None = None
    description: str | None = None
    repo_url: str | None = None


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
            description=row["description"],
            repo_url=row["repo_url"],
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

    # ═══════════════════════════════════════════════════════════
    # CRUD-минимум для веб-морды (Фаза 5.1, /api/projects через воркер)
    # ═══════════════════════════════════════════════════════════

    def _invalidate_cache(self, slug: str) -> None:
        """TTL-кеш резолва устарел после записи — вытесняем вручную
        (300с TTL иначе отдавал бы старый local_path/name резолвам)."""
        rec = self._cache.pop(slug, None)
        if rec is not None:
            self._by_id.pop(rec.id, None)

    @staticmethod
    def _card(row) -> dict:
        # datetime → ISO: Celery-мост сериализует ответ в JSON
        return {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in dict(row).items()
        }

    async def fetch_card(self, slug: str) -> dict | None:
        """Полная карточка проекта по slug (даты уже ISO-строки); None — нет slug."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.SELECT_PROJECT_CARD, slug)
            return self._card(row) if row is not None else None

    async def create_card(
        self,
        slug: str,
        name: str,
        description: str | None = None,
        kind: str = "code",
        status: str = "active",
        local_path: str | None = None,
        repo_url: str | None = None,
        docs_url: str | None = None,
        homepage_url: str | None = None,
        default_branch: str = "main",
        links: list[dict] | None = None,
        technologies: list[dict] | None = None,
    ) -> dict:
        """Создать проект + replace links/technologies (одна транзакция).

        UniqueViolationError на дубль slug пробрасывается вызывающему (409).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    q.INSERT_PROJECT, slug, name, description, kind, status,
                    local_path, repo_url, docs_url, homepage_url, default_branch,
                )
                if links:
                    await self._replace_links(conn, row["id"], links)
                if technologies:
                    await self._replace_technologies(conn, row["id"], technologies)
        self._invalidate_cache(slug)
        card = await self.fetch_card(slug)
        return card if card is not None else {}

    async def update_card(
        self,
        slug: str,
        name: str | None = None,
        description: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        local_path: str | None = None,
        repo_url: str | None = None,
        docs_url: str | None = None,
        homepage_url: str | None = None,
        default_branch: str | None = None,
        links: list[dict] | None = None,
        technologies: list[dict] | None = None,
    ) -> dict | None:
        """Частичное обновление: None-поля не меняются (UPDATE_PROJECT COALESCE).

        None → slug не найден (REST отдаёт 404, создание — только POST).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    q.UPDATE_PROJECT, slug, name, description, kind, status,
                    local_path, repo_url, docs_url, homepage_url, default_branch,
                )
                if row is None:
                    return None
                if links:
                    await self._replace_links(conn, row["id"], links)
                if technologies:
                    await self._replace_technologies(conn, row["id"], technologies)
        self._invalidate_cache(slug)
        return await self.fetch_card(slug)

    @staticmethod
    async def _replace_links(conn: asyncpg.Connection, project_id: str, links: list[dict]) -> None:
        """Полный replace ссылок проекта (PATCH передаёт желаемый список целиком)."""
        await conn.execute(q.DELETE_PROJECT_LINKS, project_id)
        await conn.execute(
            q.INSERT_PROJECT_LINKS,
            project_id,
            [link["link_type"] for link in links],
            [link["url"] for link in links],
            [link.get("title") for link in links],
        )

    @staticmethod
    async def _replace_technologies(
        conn: asyncpg.Connection, project_id: str, technologies: list[dict]
    ) -> None:
        """Полный replace стека: словарь technologies пополняется без переписывания."""
        await conn.execute(q.DELETE_PROJECT_TECHNOLOGIES, project_id)
        await conn.execute(
            q.UPSERT_TECHNOLOGIES,
            [tech["name"] for tech in technologies],
            [tech.get("category") for tech in technologies],
            [tech.get("docs_url") for tech in technologies],
        )
        await conn.execute(
            q.INSERT_PROJECT_TECHNOLOGIES,
            project_id,
            [tech["name"] for tech in technologies],
            [tech.get("version") for tech in technologies],
            [tech.get("purpose") for tech in technologies],
        )
