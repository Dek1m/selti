import json
import logging

import asyncpg

logger = logging.getLogger(__name__)


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 20,
) -> asyncpg.Pool:
    """Создаёт пул соединений к PostgreSQL."""
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    async def init_conn(conn: asyncpg.Connection) -> None:
        """Инициализация каждого нового соединения."""
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        # Таймаут на запрос — предохранитель от зависших запросов
        # Увеличен до 45s чтобы не конфликтовать с TOOL_TIMEOUT 60s
        await conn.execute("SET statement_timeout = '45s'")
        logger.debug("init_conn: statement_timeout=45s")

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
        "statement_timeout": "45s",
    })
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
