import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from prometheus_client import generate_latest, REGISTRY
from starlette.routing import Mount
from starlette.types import ASGIApp, Scope, Receive, Send

from memory_server.config import settings
from memory_server.metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION,
)
from memory_server.server import mcp, request_id_var


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp._lifespan_manager():
        yield


app = FastAPI(lifespan=lifespan, title=settings.mcp_server_name)


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
    return {
        "status": "ok",
        "server": settings.mcp_server_name,
        "version": "0.1.0",
        "checks": {
            "config": {
                "dedup_enabled": settings.dedup_enabled,
                "api_key_configured": bool(settings.api_key),
                "redis_configured": bool(settings.redis_url),
            }
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
# Используем Streamable HTTP с stateless_http=True — opencode не шлёт
# initialize handshake (требуется для SSE), поэтому SSE не работает.
# Streamable HTTP самодостаточен: каждый запрос содержит всё необходимое.
#
# path="/" — роут внутри sub-app, монтируем на /mcp.
# Монтируем напрямую через Mount (как Starlette) с redirect_slashes=False,
# чтобы POST /mcp не редиректился на /mcp/.
@app.get("/mcp", include_in_schema=False)
async def mcp_entry():
    """MCP endpoint — GET возвращает информацию о сервере."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "server": settings.mcp_server_name,
        "transport": "streamable-http",
        "stateless": True,
    })

# Mount — обёрнут в auth middleware (mount не проходит через @app.middleware)
mcp_app = AuthASGIMiddleware(mcp.http_app(path="/", stateless_http=True))
app.router.routes.append(
    Mount("/mcp", app=mcp_app, redirect_slashes=False)
)


if __name__ == "__main__":
    uvicorn.run(
        "memory_server.__main__:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )
