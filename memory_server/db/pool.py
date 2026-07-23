import asyncpg


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 20,
    hnsw_ef_search: int = 40,
) -> asyncpg.Pool:
    """Создаёт пул соединений к PostgreSQL с pgvector.

    Параметры:
        hnsw_ef_search: качество поиска HNSW (по умолч. 40).
            Для production с 8192-dim рекомендуется 80-120.
            Влияние: ×2 к ef_search ≈ ×1.5 к времени запроса,
            но recall растёт с 0.90 до 0.97+.
    """
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    async def init_conn(conn: asyncpg.Connection) -> None:
        """Инициализация каждого нового соединения."""
        await conn.execute(f"SET hnsw.ef_search = {hnsw_ef_search}")
        # Таймаут на запрос — предохранитель от зависших запросов
        await conn.execute("SET statement_timeout = '30s'")

    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        init=init_conn,
        # Таймаут на получение соединения из пула
        timeout=10.0,
    )


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
