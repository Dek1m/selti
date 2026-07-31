import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from prometheus_client import generate_latest, REGISTRY
from starlette.types import ASGIApp, Scope, Receive, Send

from memory_server.config import settings
from memory_server.metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION,
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
    """Lifespan: делегируем управление MCP sub-app'у.
    
    mcp_http_app.lifespan сам вызывает mcp._lifespan_manager()
    и session_manager.run() для streamable HTTP.
    """
    async with mcp_http_app.lifespan(app):
        yield


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

    return response


# ---- Health ----
@app.get("/health")
async def health():
    checks = {}

    # PostgreSQL — lightweight probe (не полагаемся на pool singleton)
    try:
        import asyncpg
        # asyncpg не понимает postgresql+asyncpg:// — трансформируем в postgresql://
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)
        await conn.fetchval("SELECT 1")
        await conn.close()
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"

    # Redis — ping через async redis client
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # Celery — inspect ping через asyncio.to_thread (sync→async)
    celery = _get_celery_app()
    if celery is not None:
        try:
            def _inspect_ping():
                insp = celery.control.inspect(timeout=5)
                return insp.ping()

            ping_result = await asyncio.to_thread(_inspect_ping)
            if ping_result:
                checks["celery"] = f"ok ({len(ping_result)} workers)"
            else:
                checks["celery"] = "error: no workers responding"
        except Exception as e:
            checks["celery"] = f"error: {e}"
    else:
        checks["celery"] = "unavailable (celery_app not configured)"

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
