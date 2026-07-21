import asyncpg


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 20,
) -> asyncpg.Pool:
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
    )


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
