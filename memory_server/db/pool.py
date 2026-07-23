import asyncpg


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 20,
) -> asyncpg.Pool:
    """Создаёт пул соединений к PostgreSQL с pgvector.

    Используем точный поиск (без индекса), т.к. pgvector ограничивает
    индексы 2000 измерениями, а у нас 4096-dim векторы.

    Для датасета <100K записей точный поиск даёт latency ~50-500ms.
    """
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    async def init_conn(conn: asyncpg.Connection) -> None:
        """Инициализация каждого нового соединения."""
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
