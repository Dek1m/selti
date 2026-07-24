"""
Migration runner for memory server.

Usage:
    python migrations/run.py          # Apply pending migrations
    python migrations/run.py --down   # Rollback last migration

Environment:
    DATABASE_URL — PostgreSQL connection string (default: postgresql://athena:athena@localhost:5432/athene_memory)
"""

import asyncio
import logging
import os
import sys

import asyncpg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DSN = "postgresql://athena:athena@localhost:5432/athene_memory"


async def ensure_migrations_table(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


async def get_applied_migrations(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM _migrations ORDER BY filename")
    return {row["filename"] for row in rows}


def parse_migration_file(path: str) -> tuple[str, str | None]:
    """Returns (up_sql, down_sql). down_sql is None if not present."""
    with open(path) as f:
        content = f.read()

    if "-- DOWN" in content:
        up_part, down_part = content.split("-- DOWN", 1)
        return up_part.strip(), down_part.strip()
    return content.strip(), None


async def apply_migration(conn: asyncpg.Connection, filename: str) -> None:
    path = os.path.join(MIGRATIONS_DIR, filename)
    up_sql, _ = parse_migration_file(path)

    logger.info("Applying migration: %s", filename)
    async with conn.transaction():
        await conn.execute(up_sql)
        await conn.execute(
            "INSERT INTO _migrations (filename) VALUES ($1)", filename
        )
    logger.info("Applied: %s", filename)


async def rollback_migration(conn: asyncpg.Connection, filename: str) -> None:
    path = os.path.join(MIGRATIONS_DIR, filename)
    _, down_sql = parse_migration_file(path)

    if down_sql is None:
        logger.warning("No DOWN section found in %s, skipping rollback", filename)
        return

    logger.info("Rolling back migration: %s", filename)
    async with conn.transaction():
        await conn.execute(down_sql)
        await conn.execute(
            "DELETE FROM _migrations WHERE filename = $1", filename
        )
    logger.info("Rolled back: %s", filename)


async def run_migrations() -> None:
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(dsn)
    try:
        await ensure_migrations_table(conn)
        applied = await get_applied_migrations(conn)

        files = sorted(
            f for f in os.listdir(MIGRATIONS_DIR)
            if f.endswith(".sql") and f not in applied
        )

        if not files:
            logger.info("No pending migrations")
            return

        for file in files:
            await apply_migration(conn, file)
    finally:
        await conn.close()


async def run_down() -> None:
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(dsn)
    try:
        await ensure_migrations_table(conn)
        applied = await get_applied_migrations(conn)

        if not applied:
            logger.info("No migrations to roll back")
            return

        last = sorted(applied)[-1]
        await rollback_migration(conn, last)
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if "--down" in sys.argv:
        asyncio.run(run_down())
    else:
        asyncio.run(run_migrations())
