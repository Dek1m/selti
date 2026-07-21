from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from memory_server.config import settings
from memory_server.server import mcp


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp:
        yield


app = FastAPI(lifespan=lifespan, title=settings.mcp_server_name)


@app.get("/health")
async def health():
    return {"status": "ok", "server": settings.mcp_server_name}


# Mount MCP SSE transport — под своим префиксом, не перекрывает /health
app.mount("/mcp", mcp.sse_app())


if __name__ == "__main__":
    uvicorn.run(
        "memory_server.__main__:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )
