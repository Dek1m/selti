import importlib
import logging
import multiprocessing
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from memory_server.config import settings
from memory_server.logger import get_logger, request_id_var
from memory_server.tasks.logging_config import setup_server_logging
from memory_server.tools import TOOL_MODULES
from migrations.run import run_migrations

# Инициализация логирования — каждый воркер должен иметь свой logger
setup_server_logging(level=settings.log_level, service=settings.mcp_server_name)

logger = get_logger(__name__)

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

    # Зависимости процесса — в SeltiState (state.py).
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

# Регистрация тулов через реестр модулей (декораторы срабатывают на импорте)
for _tool_module in TOOL_MODULES:
    importlib.import_module(_tool_module)
