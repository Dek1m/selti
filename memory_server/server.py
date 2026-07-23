import asyncio
import json
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastmcp import FastMCP

from memory_server.config import settings
from memory_server.db.pool import close_pool, create_pool
from memory_server.embedding.client import EmbeddingClient
from memory_server.memory.repository import MemoryRepository
from memory_server.memory.service import MemoryService
from memory_server.metrics import DB_POOL_SIZE, DB_POOL_AVAILABLE

# Correlation ID через contextvars — пробрасывается из middleware __main__.py
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JSONFormatter(logging.Formatter):
    """Структурированный JSON-формат для логов."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get(None) or getattr(record, "request_id", None)
        if request_id:
            log_data["request_id"] = request_id
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        if record.exc_info and record.exc_info[0]:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data, ensure_ascii=False)


_handler = logging.StreamHandler()
_handler.setFormatter(JSONFormatter())
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    handlers=[_handler],
)

logger = logging.getLogger(__name__)


async def _pool_metrics_updater(pool):
    """Фоновая задача: обновляет метрики пула раз в 15 секунд."""
    try:
        while True:
            DB_POOL_SIZE.set(pool.get_size())
            DB_POOL_AVAILABLE.set(pool.get_idle_size())
            await asyncio.sleep(15)
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(server: FastMCP):
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    pool = await create_pool(
        dsn=dsn,
        min_size=settings.db_min_connections,
        max_size=settings.db_max_connections,
    )

    DB_POOL_SIZE.set(pool.get_size())
    DB_POOL_AVAILABLE.set(pool.get_idle_size())

    metrics_task = asyncio.create_task(_pool_metrics_updater(pool))

    embedding_client = EmbeddingClient(
        api_url=settings.embedding_api_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
    )

    repository = MemoryRepository(pool=pool)
    service = MemoryService(
        repository=repository,
        embedding_provider=embedding_client,
    )

    logger.info(
        "Memory server started: pool=%s, model=%s",
        settings.database_url,
        settings.embedding_model,
    )

    try:
        yield {"service": service}
    finally:
        metrics_task.cancel()
        await metrics_task
        await embedding_client.aclose()
        await close_pool(pool)
        logger.info("Memory server shutdown complete")


mcp = FastMCP(
    name=settings.mcp_server_name,
    lifespan=lifespan,
)

# Import tools to register them (decorators execute on import)
import memory_server.tools.memory_tools  # noqa: F401, E402
