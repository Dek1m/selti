import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from memory_server.config import settings
from memory_server.db.pool import close_pool, create_pool
from memory_server.embedding.client import EmbeddingClient
from memory_server.memory.repository import MemoryRepository
from memory_server.memory.service import MemoryService

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(server: FastMCP):
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    pool = await create_pool(
        dsn=dsn,
        min_size=settings.db_min_connections,
        max_size=settings.db_max_connections,
    )

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
        await embedding_client.aclose()
        await close_pool(pool)
        logger.info("Memory server shutdown complete")


mcp = FastMCP(
    name=settings.mcp_server_name,
    lifespan=lifespan,
)

# Import tools to register them (decorators execute on import)
import memory_server.tools.memory_tools  # noqa: F401, E402
