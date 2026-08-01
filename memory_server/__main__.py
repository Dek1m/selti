import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from prometheus_client import generate_latest, REGISTRY, multiprocess, CollectorRegistry
from starlette.types import ASGIApp, Scope, Receive, Send

from memory_server.config import settings
from memory_server.db.pool import create_pool, close_pool
from memory_server.metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION,
    HEALTH_STATUS,
    HEALTH_CHECKS_TOTAL,
)
from memory_server.server import mcp, request_id_var

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
    """Lifespan: pool + MCP sub-app.

    Создаёт asyncpg pool для health check'ов и прочих sync-free операций.
    MCP lifespan управляет session_manager.
    """
    # Создаём пул для FastAPI process (отдельно от Celery workers)
    pool = None
    try:
        pool = await create_pool(
            dsn=settings.database_url,
            min_size=settings.db_min_connections,
            max_size=min(settings.db_max_connections, 4),
        )
        app.state.pool = pool
    except Exception:
        app.state.pool = None

    async with mcp_http_app.lifespan(app):
        try:
            yield
        finally:
            if pool is not None:
                await close_pool(pool)


app = FastAPI(lifespan=lifespan, title=settings.mcp_server_name)

# ---- REST API: tasks management ----
from memory_server.api.tasks import router as tasks_router
app.include_router(tasks_router)


# ---- Middleware: аутентификация ----
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path in ("/health", "/metrics"):
        return await call_next(request)

    if not settings.api_key:
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {settings.api_key}":
        return await call_next(request)

    return Response(status_code=403, content="Forbidden")


# ---- Middleware: correlation ID + HTTP-метрики ----
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
        HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status="500").inc()
        raise

    duration = time.monotonic() - start
    status = str(response.status_code)
    HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status=status).inc()
    HTTP_REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)

    response.headers["X-Correlation-ID"] = request_id
    return response


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

    # Redis — ping через async redis client
    HEALTH_CHECKS_TOTAL.labels(check="redis").inc()
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await asyncio.wait_for(r.ping(), timeout=3)
        await r.aclose()
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
        "version": "0.1.0",
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
app.mount("/mcp", AuthASGIMiddleware(mcp_http_app))


if __name__ == "__main__":
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
    )
