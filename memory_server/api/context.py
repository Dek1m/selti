"""REST для ZCode-хука «облачка знаний» (Фаза 6.3, план 5.1-стиль).

Лёгкие read-only эндпоинты рядом с /live: хук не умеет MCP/Celery —
ходит HTTP. Чтение из Redis ctx:{slug}/таблицы project_contexts без
задач воркера; снапшот ещё не посчитан → 404 (хук деградирует молча).

- GET /context/{slug} — снапшот контекста проекта (тот же JSON, что тул)
- GET /context/{slug}/digest — sha256 контента и секций (кеш-сохраняющий
  протокол хука: не изменился → хук не инжектит ничего, префикс-кеш
  GLM дремлет; решение Мастера 19.09)
- GET /projects — реестр slug+name (+local_path/kind/status): диагностике
  хука нужен матч ZCODE_PROJECT_DIR → slug.
"""

import hashlib

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from memory_server.exceptions import NotFoundError
from memory_server.logger import get_logger
from memory_server.state import get_state

logger = get_logger(__name__)

router = APIRouter(tags=["context"])


def _digest(text: str) -> str:
    """sha256 как идентификатор содержимого (content-addressed deltas)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@router.get("/context/{slug}")
@router.get("/api/contexts/{slug}")
async def get_context(slug: str, refresh: bool = False) -> JSONResponse:
    """Снапшот контекста проекта (Redis → таблица; refresh=1 — пересчёт).

    /api/contexts/{slug} — алиас для веб-морды (Фаза 5.1, конвенция /api/*);
    /context/{slug} — внешний контракт ZCode-хука, не переименовывается.
    """
    service = await get_state().get_memory_service()
    try:
        context = await service.get_project_context(slug, refresh=refresh)
    except NotFoundError:
        return JSONResponse(status_code=404, content={"detail": f"project not found: {slug}"})
    return JSONResponse(content=context.model_dump(mode="json"))


@router.get("/context/{slug}/digest")
async def get_context_digest(slug: str) -> JSONResponse:
    """Дайджест облачка: sha256 контента + sha256 каждой секции.

    Ответ — десятки байт: хук на UserPromptSubmit сверяет digest до
    какого-либо тяжёлого действия.
    """
    service = await get_state().get_memory_service()
    try:
        context = await service.get_project_context(slug)
    except NotFoundError:
        return JSONResponse(status_code=404, content={"detail": f"project not found: {slug}"})
    return JSONResponse(content={
        "digest": _digest(context.content or ""),
        "sections": {
            key: _digest("\n".join(lines))
            for key, lines in (context.sections or {}).items()
            if isinstance(lines, list)
        },
        "stale": context.stale,
        "computed_at": context.computed_at.isoformat() if context.computed_at else None,
    })


@router.get("/projects")
async def list_projects() -> dict:
    """Реестр проектов для диагностики хука (матч local_path → slug)."""
    project_repo = await get_state().get_project_repository()
    records = await project_repo.list_all()
    return {
        "projects": [
            {
                "slug": rec.slug,
                "name": rec.name,
                "kind": rec.kind,
                "status": rec.status,
                "local_path": rec.local_path,
            }
            for rec in records
        ]
    }
