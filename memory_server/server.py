import logging
import multiprocessing
from contextlib import asynccontextmanager

import structlog
from fastmcp import FastMCP

from memory_server.config import settings
from memory_server.tasks.logging_config import setup_server_logging
from argenta_logging import request_id_var
from migrations.run import run_migrations

# Инициализация логирования — каждый воркер должен иметь свой logger
setup_server_logging(level=settings.log_level, service=settings.mcp_server_name)

logger = structlog.get_logger()

# Подавляем шум MCP SDK (Terminating session, StreamableHTTP lifecycle)
_MCP_SUPPRESSED = ("Terminating session", "StreamableHTTP session manager")


class _MCPSdkFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(s in msg for s in _MCP_SUPPRESSED)


logging.getLogger().addFilter(_MCPSdkFilter())


@asynccontextmanager
async def lifespan(server: FastMCP):
    # Миграции — создают extension vector, таблицы, индексы
    await run_migrations()

    # MemoryService, EmbeddingClient, QdrantClient, pool —
    # теперь worker-scoped singletons в connections.py.
    # MCP tools отправляют задачи через Celery (task_bridge.py).
    if multiprocessing.current_process().name == "MainProcess":
        logger.info("Memory server started", extra={"model": settings.embedding_model})

    try:
        yield
    finally:
        if multiprocessing.current_process().name == "MainProcess":
            logger.info("Memory server shutdown complete")


mcp = FastMCP(
    name=settings.mcp_server_name,
    lifespan=lifespan,
)

# Import tools to register them (decorators execute on import)
import memory_server.tools.memory_tools  # noqa: F401, E402
import memory_server.tools.hash_tools  # noqa: F401, E402
