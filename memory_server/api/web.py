"""REST-каркас веб-морды (Фаза 5.1): /api/* рядом с MCP.

Единый путь исполнения (принцип 3 плана): все эндпоинты вызывают
операции через celery_call-мост — как MCP-тула; прямого MemoryService
в web-процессе нет. Ответы повторяют контракты тулов; новые поля
добавляются в конец, существующие не переименовываются.

Исключение — /api/contexts/{slug}: алиас fast-path облачка Фазы 6
(Redis/таблица, без воркера; снапшот материализован beat-задачей,
задача Афины — переиспользовать, не дублировать).

Auth (/api/*, middleware в __main__.py): localhost свободно; снаружи —
только с валидным Bearer (settings.api_key; не задан — наружу 403).
CORS — под фронт-порт Vite (settings.cors_origins).
"""

from datetime import datetime
from hashlib import sha1
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from memory_server.config import settings
from memory_server.tools.task_bridge import celery_call

router = APIRouter(prefix="/api", tags=["web"])

# localhost-клиенты: UI и проверки ходят с той же машины без токена
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_api_authorized(client_host: str | None, auth_header: str, api_key: str) -> bool:
    """Правило доступа /api/* (Фаза 5.1): localhost свободно; снаружи —
    только с валидным Bearer. Токен опционален настройкой (env): не задан —
    наружу 403, API не должен молча торчать наружу без защиты."""
    if api_key and auth_header == f"Bearer {api_key}":
        return True
    return client_host in _LOCAL_HOSTS

# Имена задач (конвенция memory_tools.py: литералы рядом с вызовами)
TASK_SEARCH = "memory_server.tasks.memory_tasks.search_memories"
TASK_GET = "memory_server.tasks.memory_tasks.get_memory"
TASK_STATS = "memory_server.tasks.memory_tasks.get_stats"
TASK_TRAVERSE = "memory_server.tasks.memory_tasks.traverse_graph"
TASK_GET_HISTORY = "memory_server.tasks.memory_tasks.get_memory_history"
TASK_GET_RELATIONS = "memory_server.tasks.memory_tasks.get_relations"
TASK_FIND_SIMILAR = "memory_server.tasks.memory_tasks.find_similar"
TASK_NAMESPACES = "memory_server.tasks.memory_tasks.get_namespaces"
TASK_LINKER_STATS = "memory_server.tasks.linker_tasks.linker_stats"
TASK_MAP_META = "memory_server.tasks.map_tasks.map_meta"
TASK_MAP_BUILD = "memory_server.tasks.map_tasks.build_map_snapshot"
TASK_MAP_POSITIONS = "memory_server.tasks.map_tasks.map_positions"
TASK_PROJECT_LIST = "memory_server.tasks.project_tasks.list_projects"
TASK_PROJECT_GET = "memory_server.tasks.project_tasks.get_project"
TASK_PROJECT_CREATE = "memory_server.tasks.project_tasks.create_project"
TASK_PROJECT_UPDATE = "memory_server.tasks.project_tasks.update_project"

# Caps запросов UI (hard-cap узлов traverse — traverse_max_nodes, воркер)
MAX_SEARCH_LIMIT = 100
MAX_GRAPH_DEPTH = 10

GranuleStatus = Literal["asserted", "superseded", "retracted", "uncertain"]
ProjectKind = Literal["code", "infra", "domain", "workspace", "org"]
ProjectStatus = Literal["active", "archived", "frozen"]
LinkType = Literal["repo", "ci", "docs", "board", "monitoring", "adr", "other"]


def _exc_name(exc: BaseException) -> str:
    """Имя класса исключения: Celery-мост реконструирует исключения воркера,
    isinstance через Redis-бэкенд ненадёжен — имя класса стабильнее."""
    return type(exc).__name__


async def _call(task_name: str, **kwargs: Any) -> Any:
    """celery_call с маппингом ошибок воркера на HTTP-статусы.

    Timeout моста → 504 (воркер перегружен/умер); NotFoundError → 404;
    ValidationError/ValueError → 400 (ошибка входа, не retryable);
    дубль slug (UniqueViolationError) → 409. Прочее — 500 (не глотаем).
    """
    try:
        return await celery_call(task_name, **kwargs)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except Exception as exc:
        name = _exc_name(exc)
        if name == "NotFoundError":
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if name in ("ValidationError", "ValueError"):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if name == "UniqueViolationError":
            raise HTTPException(status_code=409, detail="project slug already exists") from exc
        raise


# ═══════════════════════════════════════════════════════════════
# Search / granules / graph / stats
# ═══════════════════════════════════════════════════════════════


async def _attach_positions(items: list[dict[str, Any]]) -> None:
    """Дописать position: [x, y, z] из map_layout (созвездие web-морды).

    LEFT JOIN семантика: поле появляется только у гранул со строкой в
    таблице — существующий контракт ответа не меняется, только расширяется.
    """
    ids = [item["id"] for item in items if item.get("id")]
    if not ids:
        return
    positions = await _call(TASK_MAP_POSITIONS, ids=ids)
    for item in items:
        position = positions.get(item["id"])
        if position is not None:
            item["position"] = position


@router.get("/search")
async def search(
    query: str,
    user_id: str | None = None,
    limit: int = Query(10, ge=1, le=MAX_SEARCH_LIMIT),
    offset: int = Query(0, ge=0),
    threshold: float = Query(settings.search_default_threshold, ge=0.0, le=1.0),
    namespace: str | None = None,
    project_id: str | None = None,
    include_historical: bool = False,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    status: GranuleStatus | None = None,
    with_positions: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid-поиск (тот же JSON, что тул memory_search) + фильтры Фазы 5.1:
    namespace/project_id, окно created_at, точный статус. offset —
    пагинация /ui (Фаза 5.2): слайс детерминированного ранжирования.
    with_positions — координаты map_layout на хитах (граф «Созвездие»)."""
    results = await _call(
        TASK_SEARCH,
        query=query,
        user_id=user_id,
        limit=limit,
        offset=offset,
        threshold=threshold,
        namespace=namespace,
        project_id=project_id,
        include_historical=include_historical,
        # Celery JSON-сериализация не несёт datetime — ISO-строки
        created_after=created_after.isoformat() if created_after else None,
        created_before=created_before.isoformat() if created_before else None,
        status=status,
    )
    if with_positions:
        await _attach_positions(results)
    return results


@router.get("/memories/{memory_id}")
async def get_memory(memory_id: str, include_history: bool = False, with_positions: bool = False) -> dict[str, Any]:
    """Гранула (контракт memory_get); include_history=true добавляет поле
    history — supersession-цепочка {items, current_id} (контракт memory_get_history).
    with_positions — поле position из map_layout (граф «Созвездие»)."""
    record = await _call(TASK_GET, memory_id=memory_id)
    if with_positions:
        await _attach_positions([record])
    if include_history:
        record["history"] = await _call(TASK_GET_HISTORY, granule_id=memory_id)
    return record


@router.get("/memories/{memory_id}/relations")
async def memory_relations(
    memory_id: str, link_type: str | None = None, with_positions: bool = False
) -> dict[str, Any]:
    """Входящие/исходящие связи (контракт memory_get_relations) — секция
    «Связи» карточки гранулы (§5.1 дизайна: синапсы двух направлений).
    with_positions — карта positions соседей {id: [x, y, z]} из map_layout
    (граф «Созвездие»); без строк — пустая карта."""
    payload = await _call(TASK_GET_RELATIONS, source_id=memory_id, link_type=link_type)
    if with_positions:
        neighbor_ids = sorted(
            {rel["target_id"] for rel in payload["outgoing"] if rel.get("target_id")}
            | {rel["source_id"] for rel in payload["incoming"] if rel.get("source_id")}
        )
        payload["positions"] = await _call(TASK_MAP_POSITIONS, ids=neighbor_ids)
    return payload


@router.get("/memories/{memory_id}/similar")
async def memory_similar(
    memory_id: str,
    limit: int = Query(5, ge=1, le=MAX_SEARCH_LIMIT),
    threshold: float = Query(settings.search_default_threshold, ge=0.0, le=1.0),
) -> list[dict[str, Any]]:
    """Похожие гранулы (контракт memory_find_similar): контент гранулы —
    seed запроса; сама гранула исключается из выдачи."""
    record = await _call(TASK_GET, memory_id=memory_id)
    similar = await _call(
        TASK_FIND_SIMILAR,
        content=record["content"],
        limit=limit + 1,
        threshold=threshold,
    )
    return [item for item in similar if item["id"] != memory_id][:limit]


@router.get("/graph/{memory_id}")
async def graph(
    memory_id: str,
    depth: int = Query(3, ge=1, le=MAX_GRAPH_DEPTH),
    link_types: list[str] | None = Query(None),
    limit: int | None = Query(None, ge=1, le=settings.traverse_max_nodes),
    offset: int = Query(0, ge=0),
    project_id: str | None = None,
) -> dict[str, Any]:
    """Обход графа от узла (контракт memory_traverse); hard-cap узлов —
    traverse_max_nodes на воркере, depth ограничен REST-слоем."""
    return await _call(
        TASK_TRAVERSE,
        start_id=memory_id,
        depth=depth,
        link_types=link_types,
        limit=limit,
        offset=offset,
        project_id=project_id,
    )


@router.get("/stats")
async def stats(
    user_id: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Статистика по namespace (контракт memory_stats); project_id — срез по проекту."""
    return await _call(TASK_STATS, user_id=user_id, project_id=project_id)


@router.get("/namespaces")
async def namespaces() -> list[dict[str, Any]]:
    """Реестр namespace (контракт memory_namespaces) — спектр цветов UI (§11 дизайна)."""
    return await _call(TASK_NAMESPACES)


@router.get("/linker/stats")
async def linker_stats() -> dict[str, Any]:
    """Статистика Линкера V3 (контракт memory_linker_stats). Read-only."""
    return await _call(TASK_LINKER_STATS)


# ═══════════════════════════════════════════════════════════════
# Полная карта 3D (PLAN_FULL_MAP_3D M1)
# ═══════════════════════════════════════════════════════════════

# Байты снапшота лежат в Redis сжатыми (Celery-JSON байты не переносит):
# web-процесс читает их сам — тот же fast-path-паттерн, что у облачка
# Фазы 6 (/api/contexts). Отдельная функция — точка подмены в тестах.
async def _redis_get_bytes(key: str) -> bytes | None:
    from memory_server.state import get_state

    redis = await get_state().get_redis_bytes()
    return await redis.get(key)


def _map_etag(
    version: str, with_preview: bool, project_id: str | None, namespace: str | None
) -> str:
    """ETag снапшота: дефолтный набор параметров = чистая version (план §2.5);
    фильтры/with_preview меняют тело при той же version — расширяют тег."""
    if with_preview and not project_id and not namespace:
        return version
    return sha1(f"{version}|{int(with_preview)}|{project_id or ''}|{namespace or ''}".encode()).hexdigest()[:12]


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """If-None-Match: список тегов через запятую, возможен слабый W/-префикс."""
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        normalized = candidate.strip()
        if normalized.startswith("W/"):
            normalized = normalized[2:]
        if normalized.strip('"') == etag:
            return True
    return False


_MAP_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=600"


@router.get("/map/meta")
async def map_meta() -> dict[str, Any]:
    """Мета Полной карты: version-хэш, счётчики, layout_at/stale (<50мс,
    кеш Redis 60с). Источник ETag для /api/map/full."""
    return await _call(TASK_MAP_META)


@router.get("/map/full")
async def map_full(
    request: Request,
    with_preview: bool = True,
    project_id: str | None = Query(None, max_length=64),
    namespace: str | None = Query(None, max_length=64),
) -> Response:
    """Снапшот Полной карты (columnar, план §3): узлы/рёбра индексными
    массивами, координаты из map_layout (или сферический fallback до
    первого прогона layout_map). gz-байты отдаются как есть с
    Content-Encoding: gzip — GZipMiddleware в приложении нет, двойного
    сжатия не возникает. If-None-Match → 304."""
    meta = await _call(TASK_MAP_META)
    etag = _map_etag(meta["version"], with_preview, project_id, namespace)
    headers = {"ETag": f'"{etag}"', "Cache-Control": _MAP_CACHE_CONTROL}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)

    report = await _call(
        TASK_MAP_BUILD,
        with_preview=with_preview,
        project_id=project_id,
        namespace=namespace,
    )
    if report.get("stalled"):
        # Конкурент держит build-lock дольше лимита ожидания — клиент
        # повторит через секунды (тело ещё собирается)
        raise HTTPException(status_code=503, detail="map snapshot build in progress")
    # Фикс F5: ETag — из ТОГО ЖЕ ответа, что собирал тело. Данные могли
    # смениться между meta- и build-вызовами: метка обязана описывать
    # отданные байты, а не версию на секунду старше.
    etag = _map_etag(report["version"], with_preview, project_id, namespace)
    headers["ETag"] = f'"{etag}"'
    snapshot = await _redis_get_bytes(report["key"])
    if snapshot is None:
        raise HTTPException(status_code=502, detail="map snapshot missing in cache after build")
    return Response(
        content=snapshot,
        media_type="application/json",
        headers={**headers, "Content-Encoding": "gzip"},
    )


# ═══════════════════════════════════════════════════════════════
# Projects CRUD-минимум
# ═══════════════════════════════════════════════════════════════

class ProjectLinkIn(BaseModel):
    link_type: LinkType
    url: str
    title: str | None = None


class ProjectTechnologyIn(BaseModel):
    name: str
    category: str | None = None
    docs_url: str | None = None
    version: str | None = None
    purpose: str | None = None


class ProjectCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str | None = None  # None → name = slug (конвенция ADR-018)
    description: str | None = None
    kind: ProjectKind = "code"
    status: ProjectStatus = "active"
    local_path: str | None = None
    repo_url: str | None = None
    docs_url: str | None = None
    homepage_url: str | None = None
    default_branch: str = "main"
    links: list[ProjectLinkIn] | None = None
    technologies: list[ProjectTechnologyIn] | None = None


class ProjectPatch(BaseModel):
    """PATCH: None-поля не меняются; links/technologies — полный replace при передаче."""

    name: str | None = None
    description: str | None = None
    kind: ProjectKind | None = None
    status: ProjectStatus | None = None
    local_path: str | None = None
    repo_url: str | None = None
    docs_url: str | None = None
    homepage_url: str | None = None
    default_branch: str | None = None
    links: list[ProjectLinkIn] | None = None
    technologies: list[ProjectTechnologyIn] | None = None


@router.get("/projects")
async def list_projects() -> dict[str, Any]:
    """Реестр проектов (карточки без стека) — таблица UI «Проекты»."""
    return {"projects": await _call(TASK_PROJECT_LIST)}


@router.get("/projects/{slug}")
async def get_project(slug: str) -> dict[str, Any]:
    """Карточка проекта + стек (links/technologies)."""
    return await _call(TASK_PROJECT_GET, slug=slug)


@router.post("/projects", status_code=201)
async def create_project(req: ProjectCreate) -> dict[str, Any]:
    """Создать проект. Человеческий канал полного CRUD — машинный матчинг
    остаётся на POST /projects/register (ADR-018), контракты не смешаны."""
    return await _call(
        TASK_PROJECT_CREATE,
        slug=req.slug,
        name=req.name or req.slug,
        description=req.description,
        kind=req.kind,
        status=req.status,
        local_path=req.local_path,
        repo_url=req.repo_url,
        docs_url=req.docs_url,
        homepage_url=req.homepage_url,
        default_branch=req.default_branch,
        links=[link.model_dump() for link in req.links] if req.links else None,
        technologies=[tech.model_dump() for tech in req.technologies] if req.technologies else None,
    )


@router.patch("/projects/{slug}")
async def patch_project(slug: str, req: ProjectPatch) -> dict[str, Any]:
    """Частичное обновление карточки (None-поля не трогаем)."""
    return await _call(
        TASK_PROJECT_UPDATE,
        slug=slug,
        name=req.name,
        description=req.description,
        kind=req.kind,
        status=req.status,
        local_path=req.local_path,
        repo_url=req.repo_url,
        docs_url=req.docs_url,
        homepage_url=req.homepage_url,
        default_branch=req.default_branch,
        links=[link.model_dump() for link in req.links] if req.links else None,
        technologies=[tech.model_dump() for tech in req.technologies] if req.technologies else None,
    )
