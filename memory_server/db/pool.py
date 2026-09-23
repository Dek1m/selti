import json

import asyncpg

from memory_server.config import settings
from memory_server.logger import get_logger

logger = get_logger(__name__)


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 20,
) -> asyncpg.Pool:
    """Создаёт пул соединений к PostgreSQL."""
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    # Таймаут на запрос — предохранитель от зависших запросов; фундамент
    # env DB_STATEMENT_TIMEOUT (был зашит '45s', аудит §5 реестра)
    statement_timeout = settings.db_statement_timeout

    async def init_conn(conn: asyncpg.Connection) -> None:
        """Инициализация каждого нового соединения."""
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        await conn.execute(f"SET statement_timeout = '{statement_timeout}'")
        logger.debug("init_conn: statement_timeout=%s", statement_timeout)

    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        init=init_conn,
        # Таймаут на получение соединения из пула
        timeout=15.0,
    )
    logger.info("create_pool", extra={
        "min": min_size,
        "max": max_size,
        "acquire_timeout": 15.0,
        "statement_timeout": statement_timeout,
    })
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
