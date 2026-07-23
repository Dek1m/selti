import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from prometheus_client import generate_latest, REGISTRY

from memory_server.config import settings
from memory_server.metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION,
)
from memory_server.server import mcp, request_id_var

# ============================================================
# Prometheus метрики — объявлены в memory_server/metrics.py
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp:
        yield


app = FastAPI(lifespan=lifespan, title=settings.mcp_server_name)


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
    return {"status": "ok", "server": settings.mcp_server_name}


# ---- Prometheus metrics endpoint ----
@app.get("/metrics")
async def metrics():
    return Response(
        content=generate_latest(REGISTRY),
        media_type="text/plain; charset=utf-8",
    )


# Mount MCP SSE transport — под своим префиксом, не перекрывает /health и /metrics
app.mount("/mcp", mcp.sse_app())


if __name__ == "__main__":
    uvicorn.run(
        "memory_server.__main__:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )
