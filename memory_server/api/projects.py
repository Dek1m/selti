"""POST /projects/register — машинная регистрация проектов (ADR-018).

Идемпотентный upsert реестра projects (миграция 017, без новых таблиц):
ZCode-плагин selti-sync шлёт slug+local_path+repo_url на SessionStart,
вся логика матчинга — на сервере (плагин stateless, решает ничего).
Защита записи — заголовок X-SELTI-KEY (env SELTI_API_KEY; пустой →
эндпоинт открыт, совместимость). CRUD для людей — Фаза 3/5; здесь
только машинный канал. repo_url записываем в единой нормализованной
форме (без .git и trailing slash) — сравнение всегда честное.
"""

from __future__ import annotations

import secrets
from typing import Literal

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memory_server.config import settings
from memory_server.logger import get_logger
from memory_server.state import get_state

logger = get_logger(__name__)

router = APIRouter(tags=["projects"])

_REGISTER_COLUMNS = "id::text, slug, local_path, repo_url"

_SELECT_BY_SLUG = f"SELECT {_REGISTER_COLUMNS} FROM projects WHERE slug = $1"
_SELECT_BY_PATH = f"SELECT {_REGISTER_COLUMNS} FROM projects WHERE local_path = $1 AND slug <> $2"
_INSERT = (
    "INSERT INTO projects (slug, name, kind, local_path, repo_url) "
    "VALUES ($1, $2, $3, $4, $5) RETURNING id::text"
)
# Перепривязка repo_url проекту, уже владеющему этим local_path (дублей папки нет)
_UPDATE_REPO_URL = "UPDATE projects SET repo_url = $2 WHERE id = $1::uuid"
# Привязка repo_url к slug с NULL-репо (случай albedo) + актуальный local_path
_UPDATE_PATH_REPO = "UPDATE projects SET local_path = $2, repo_url = $3 WHERE id = $1::uuid"


class ProjectRegisterRequest(BaseModel):
    """Тело POST /projects/register (ADR-018, решение A)."""

    slug: str
    name: str | None = None  # None → name = slug (ADR: name = slug)
    kind: Literal["code", "infra", "domain", "workspace", "org"] = "code"
    local_path: str | None = None
    repo_url: str | None = None


def normalize_repo_url(url: str | None) -> str | None:
    """Единая форма repo_url для сравнения и записи: без .git и trailing slash.

    Пустая строка трактуется как NULL — «свободен для привязки» (ADR, NULL-семантика).
    """
    if url is None:
        return None
    trimmed = url.strip().rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    return trimmed or None


async def register_project(
    conn: asyncpg.Connection, req: ProjectRegisterRequest
) -> tuple[int, dict]:
    """Матчинг ADR-018: slug → repo_url → local_path. Возвращает (status, body).

    Порядок веток: slug свободен (INSERT / перепривязка по local_path) →
    repo_url совпал (matched/path_updated, no-op не трогает updated_at) →
    repo_url NULL — свободен для привязки → 409 slug_conflict (без записи).
    """
    repo_url = normalize_repo_url(req.repo_url)
    local_path = req.local_path or None

    by_slug = await conn.fetchrow(_SELECT_BY_SLUG, req.slug)
    if by_slug is None:
        # Папка уже числится под другим slug → перепривязываем её, не плодим дубли
        by_path = await conn.fetchrow(_SELECT_BY_PATH, local_path, req.slug) if local_path else None
        if by_path is not None:
            await conn.execute(_UPDATE_REPO_URL, by_path["id"], repo_url)
            return 200, {"status": "path_updated", "slug": by_path["slug"]}
        try:
            new_id = await conn.fetchval(
                _INSERT, req.slug, req.name or req.slug, req.kind, local_path, repo_url
            )
        except asyncpg.exceptions.UniqueViolationError:
            # Гонка двух параллельных POST одним slug — конфликт, не автосуффикс
            return 409, {"status": "slug_conflict", "slug": req.slug}
        return 201, {"status": "created", "slug": req.slug, "id": new_id}

    db_repo = normalize_repo_url(by_slug["repo_url"])
    if db_repo is not None and db_repo != repo_url:
        # slug занят другим repo_url: путь уже под другим slug → перепривязка, иначе 409
        by_path = await conn.fetchrow(_SELECT_BY_PATH, local_path, req.slug) if local_path else None
        if by_path is not None:
            await conn.execute(_UPDATE_REPO_URL, by_path["id"], repo_url)
            return 200, {"status": "path_updated", "slug": by_path["slug"]}
        return 409, {"status": "slug_conflict", "slug": req.slug}

    # repo_url совпал, либо в БД NULL — «свободен для привязки» (случай albedo)
    path_changed = (by_slug["local_path"] or None) != local_path
    repo_changed = db_repo is None and repo_url is not None
    if not path_changed and not repo_changed:
        return 200, {"status": "matched", "slug": req.slug}  # no-op: updated_at не дёргаем
    await conn.execute(_UPDATE_PATH_REPO, by_slug["id"], local_path, repo_url)
    return 200, {"status": "path_updated" if path_changed else "matched", "slug": req.slug}


@router.post("/projects/register")
async def register_project_endpoint(request: Request, req: ProjectRegisterRequest) -> JSONResponse:
    """Регистрация/матчинг проекта: 201 created, 200 matched/path_updated, 409 conflict."""
    expected = settings.selti_api_key
    # compare_digest только по байтам: не-ASCII в заголовке ронял строковое сравнение в 500
    provided = request.headers.get("x-selti-key", "").encode("utf-8")
    if expected and not secrets.compare_digest(provided, expected.encode("utf-8")):
        return JSONResponse(status_code=401, content={"detail": "invalid or missing X-SELTI-KEY"})

    pool = await get_state().get_pool()
    async with pool.acquire() as conn:
        status, body = await register_project(conn, req)

    if status == 409:
        # 409 должен быть виден человеку в логах (плагин на него молчит)
        logger.warning("project register slug conflict", extra={"slug": req.slug})
    return JSONResponse(status_code=status, content=body)
