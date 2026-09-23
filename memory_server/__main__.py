import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import generate_latest, REGISTRY, multiprocess, CollectorRegistry
from starlette.types import ASGIApp, Scope, Receive, Send

from memory_server.config import settings
from memory_server.logger import get_logger
from memory_server.metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION,
    HEALTH_STATUS,
    HEALTH_CHECKS_TOTAL,
)
from memory_server.server import mcp, request_id_var
from memory_server.state import get_state


@lru_cache(maxsize=1)
def _server_version() -> str:
    """Версия сервера — единственный источник правды: VERSION-файл.

    Ищем в корне проекта/образа (memory_server/../VERSION — Docker COPY VERSION ./VERSION,
    WORKDIR /app) и рядом с пакетом. lru_cache: файл читается один раз за процесс.
    """
    for candidate in (
        Path(__file__).resolve().parent.parent / "VERSION",
        Path(__file__).resolve().parent / "VERSION",
    ):
        try:
            version = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if version:
            return version
    return "unknown"

# Lazy import Celery app — может быть не установлен при первом запуске
_celery_app = None


def _get_celery_app():
    """Получить Celery app (lazy import)."""
    global _celery_app
    if _celery_app is None:
        try:
            from memory_server.celery_app import app as celery_app
            _celery_app = celery_app
        except ImportError:
            return None
    return _celery_app


class AuthASGIMiddleware:
    """ASGI middleware для защиты sub-приложений (mount /mcp).
    
    FastAPI middleware не работает для app.mount(), поэтому оборачиваем
    SSE app напрямую на уровне ASGI.
    """
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and settings.api_key:
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") != settings.api_key:
                response = Response(status_code=403, content="Forbidden")
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

# ============================================================
# Prometheus метрики — объявлены в memory_server/metrics.py
# ============================================================


# MCP Streamable HTTP sub-app (создаём до lifespan, т.к. lifespan его использует)
mcp_http_app = mcp.http_app(path="/", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan: SeltiState (pool для health) + MCP sub-app session manager.

    Все зависимости процесса — в SeltiState (state.py): pool, Redis, Qdrant,
    сервисы. MCP tools работают через Celery (task_bridge.py), web-процессу
    из инфраструктуры нужен только pool для health-проверок.
    """
    state = get_state()
    try:
        app.state.pool = await state.get_pool()
    except Exception as exc:
        # Молчаливый отказ пула оставил бы /health красным без причины:
        # фиксируем, но процесс не роняем (пул может подняться позже)
        logger.exception(
            "lifespan: postgres pool unavailable at startup",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        app.state.pool = None

    # Прогрев runtime-снапшота (Ф2): env > БД > дефолт + LISTEN settings_changed.
    # БД недоступна → дефолты + WARN внутри get_runtime_config, процесс живёт
    try:
        await state.get_runtime_config()
    except Exception as exc:
        logger.warning(
            "lifespan: runtime config warmup failed (defaults in effect)",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )

    async with mcp_http_app.lifespan(app):
        try:
            yield
        finally:
            await state.aclose()


app = FastAPI(lifespan=lifespan, title=settings.mcp_server_name)

# ---- REST API: tasks management ----
from memory_server.api.tasks import router as tasks_router
app.include_router(tasks_router)

# ---- REST API: context cloud для ZCode-хука (Фаза 6.3) ----
from memory_server.api.context import router as context_router
app.include_router(context_router)

# ---- REST API: машинная регистрация проектов (ADR-018, плагин selti-sync) ----
from memory_server.api.projects import router as projects_router
app.include_router(projects_router)

# ---- REST API: веб-морда (Фаза 5.1) — все операции через celery_call-мост ----
from memory_server.api.web import is_api_authorized, router as web_router
app.include_router(web_router)

# ---- REST API: конфигурация (Ф2) — app_settings + профили ----
from memory_server.api.settings import router as settings_router
app.include_router(settings_router)


# ---- Middleware: аутентификация ----
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Публичные пути: health/live/metrics + context-облачко и регистрация
    # для ZCode-хуков (localhost-сервис как /live; хуки не умеют Bearer).
    # /projects/register защищён собственным X-SELTI-KEY (ADR-018, решение B)
    path = request.url.path
    if (
        path in ("/health", "/live", "/metrics", "/projects", "/projects/register")
        or path.startswith("/context/")
    ):
        return await call_next(request)

    if path.startswith("/api/"):
        if is_api_authorized(
            request.client.host if request.client else None,
            request.headers.get("Authorization", ""),
            settings.api_key,
        ):
            return await call_next(request)
        return Response(status_code=403, content="Forbidden")

    if not settings.api_key:
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {settings.api_key}":
        return await call_next(request)

    return Response(status_code=403, content="Forbidden")


# ---- Middleware: correlation ID + HTTP-метрики + access-лог ----
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request_id_var.set(request_id)

    method = request.method
    endpoint = request.url.path
    start = time.monotonic()

    try:
        response: Response = await call_next(request)
    except Exception:
        # Traceback залогирует uvicorn.error; здесь — только счётчик
        HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status="500").inc()
        raise

    duration = time.monotonic() - start
    duration_ms = round(duration * 1000, 1)
    status = str(response.status_code)
    HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status=status).inc()
    HTTP_REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)

    # Access-лог (стандарт §4.1: вход/выход API обязателен; uvicorn
    # access_log=False — единственный маркер запроса здесь).
    # Уровень: DEBUG служебные пути, WARN медленные (>500ms), INFO прочие
    log_extra = {
        "method": method,
        "path": endpoint,
        "status": response.status_code,
        "duration_ms": duration_ms,
    }
    if endpoint in _SERVICE_PATHS:
        logger.debug("http_request_completed", extra=log_extra)
    elif duration_ms > 500:
        logger.warning("http_request_completed: slow", extra=log_extra)
    else:
        logger.info("http_request_completed", extra=log_extra)

    response.headers["X-Correlation-ID"] = request_id
    return response


# ---- CORS под фронт-порт (Фаза 5.2) ----
# add_middleware добавляет наружу существующей цепочки: preflight OPTIONS
# отвечает CORS до auth-middleware (preflight не несёт Authorization)
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---- Liveness: процесс жив, без проверок зависимостей ----
# Readiness (PG/Redis/Celery) остаётся на /health — liveness не должен
# падать из-за медленного бэкенда, иначе оркестратор убивает живой процесс

logger = get_logger(__name__)

# Шумные служебные пути (скрапы Prometheus, health-check оркестратора):
# в access-логе — только DEBUG, чтобы не топить бизнес-события
_SERVICE_PATHS = ("/health", "/live", "/metrics")
@app.get("/live")
async def live():
    return {"status": "alive", "server": settings.mcp_server_name}


# ---- Health ----
@app.get("/health")
async def health():
    checks = {}

    # PostgreSQL — через пул соединений (не создаём новое соединение)
    HEALTH_CHECKS_TOTAL.labels(check="postgres").inc()
    try:
        pool = getattr(app.state, "pool", None)
        if pool is None:
            checks["postgres"] = "error: pool not available"
            HEALTH_STATUS.labels(check="postgres").set(0)
        else:
            async with pool.acquire() as conn:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=3)
            checks["postgres"] = "ok"
            HEALTH_STATUS.labels(check="postgres").set(1)
    except asyncio.TimeoutError:
        checks["postgres"] = "error: timeout"
        HEALTH_STATUS.labels(check="postgres").set(0)
    except Exception as e:
        checks["postgres"] = f"error: {e}"
        HEALTH_STATUS.labels(check="postgres").set(0)

    # Redis — ping через singleton-клиент из SeltiState
    HEALTH_CHECKS_TOTAL.labels(check="redis").inc()
    try:
        redis_client = await get_state().get_redis()
        await asyncio.wait_for(redis_client.ping(), timeout=3)
        checks["redis"] = "ok"
        HEALTH_STATUS.labels(check="redis").set(1)
    except asyncio.TimeoutError:
        checks["redis"] = "error: timeout"
        HEALTH_STATUS.labels(check="redis").set(0)
    except Exception as e:
        checks["redis"] = f"error: {e}"
        HEALTH_STATUS.labels(check="redis").set(0)

    # Celery — inspect ping через asyncio.to_thread (sync→async)
    HEALTH_CHECKS_TOTAL.labels(check="celery").inc()
    celery = _get_celery_app()
    if celery is not None:
        try:
            def _inspect_ping():
                insp = celery.control.inspect(timeout=5)
                return insp.ping()

            ping_result = await asyncio.to_thread(_inspect_ping)
            if ping_result:
                checks["celery"] = f"ok ({len(ping_result)} workers)"
                HEALTH_STATUS.labels(check="celery").set(1)
            else:
                checks["celery"] = "error: no workers responding"
                HEALTH_STATUS.labels(check="celery").set(0)
        except Exception as e:
            checks["celery"] = f"error: {e}"
            HEALTH_STATUS.labels(check="celery").set(0)
    else:
        checks["celery"] = "unavailable (celery_app not configured)"
        HEALTH_STATUS.labels(check="celery").set(0)

    overall_status = "ok" if all(
        v == "ok" or v.startswith("ok") for v in checks.values()
    ) else "degraded"

    return {
        "status": overall_status,
        "server": settings.mcp_server_name,
        "version": _server_version(),
        "checks": {
            "config": {
                "dedup_enabled": settings.dedup_enabled,
                "api_key_configured": bool(settings.api_key),
                "redis_configured": bool(settings.redis_url),
            },
            **checks,
        },
    }


# ---- Prometheus metrics endpoint ----
@app.get("/metrics")
async def metrics():
    # В multi-process режиме (PROMETHEUS_MULTIPROC_DIR установлен)
    # собираем метрики из файлов, а не из памяти процесса
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return Response(
            content=generate_latest(registry),
            media_type="text/plain; charset=utf-8",
        )
    else:
        return Response(
            content=generate_latest(REGISTRY),
            media_type="text/plain; charset=utf-8",
        )


# ---- MCP Streamable HTTP transport ----
# Streamable HTTP с stateless_http=True — не требует initialize handshake.
# opencode не шлёт SSE handshake (initialize), поэтому SSE не работает.
# path="/" внутри sub-app, монтируем на /mcp.
# GET/POST /mcp → 307 → /mcp/ → mount strips /mcp → / matches sub-app.
# Explicit redirect: /mcp -> /mcp/ (avoids 307 on every POST /mcp)
@app.api_route("/mcp", methods=["GET", "POST"])
async def redirect_mcp_trailing_slash(request: Request):
    from starlette.responses import RedirectResponse
    url = str(request.url)
    if not url.endswith("/"):
        return RedirectResponse(url=url + "/", status_code=307)
    return RedirectResponse(url=url, status_code=307)

app.mount("/mcp/", AuthASGIMiddleware(mcp_http_app))


if __name__ == "__main__":
    from memory_server.tasks.logging_config import UVICORN_LOG_CONFIG

    uvicorn.run(
        "memory_server.__main__:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
        workers=settings.uvicorn_workers,
        loop="uvloop",
        timeout_graceful_shutdown=30,
        backlog=2048,
        access_log=False,
        log_config=UVICORN_LOG_CONFIG,
    )
