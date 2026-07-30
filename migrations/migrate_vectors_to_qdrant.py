#!/usr/bin/env python3
"""
migrate_vectors_to_qdrant.py — Миграция эмбеддингов из PostgreSQL в Qdrant.

Использование:
    # 1. Экспорт + импорт
    python migrate_vectors_to_qdrant.py migrate

    # 2. Верификация (сравнение count)
    python migrate_vectors_to_qdrant.py verify

    # 3. Rollback (импорт обратно в PostgreSQL)
    python migrate_vectors_to_qdrant.py rollback

    # 4. Очистка (после успешной миграции)
    python migrate_vectors_to_qdrant.py cleanup

Конфигурация (env / .env):
    DATABASE_URL         — PostgreSQL DSN
    QDRANT_URL           — http://localhost:6333
    QDRANT_COLLECTION    — memories
    MIGRATE_BATCH_SIZE   — размер батча (по умолчанию 500)
    MIGRATE_WORKERS      — параллельные воркеры (по умолчанию 4)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import asyncpg
from dotenv import load_dotenv

# ── Lazy imports (установить pip install qdrant-client) ──
try:
    from qdrant_client import QdrantClient, models as qm
except ImportError:
    print("ERROR: pip install qdrant-client")
    sys.exit(1)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate")


# ════════════════════════════════════════════════════════════
# Конфигурация
# ════════════════════════════════════════════════════════════
@dataclass
class Config:
    database_url: str
    qdrant_url: str
    qdrant_collection: str
    batch_size: int
    workers: int
    embedding_dim: int = 4096

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL",
                "postgresql://athena:athena@localhost:5432/athene_memory",
            ),
            qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
            qdrant_collection=os.environ.get("QDRANT_COLLECTION", "memories"),
            batch_size=int(os.environ.get("MIGRATE_BATCH_SIZE", "500")),
            workers=int(os.environ.get("MIGRATE_WORKERS", "4")),
        )


# ════════════════════════════════════════════════════════════
# PostgreSQL: чтение
# ════════════════════════════════════════════════════════════
SQL_FETCH_BATCH = """
    SELECT
        m.id,
        m.user_id,
        m.content,
        m.namespace,
        m.metadata,
        m.importance,
        m.content_hash,
        m.created_at,
        m.updated_at,
        ms.status AS migration_status,
        m.embedding
    FROM memories m
    LEFT JOIN qdrant_migration_status ms ON ms.memory_id = m.id
    WHERE m.is_archived = false
      AND m.embedding IS NOT NULL
      AND (ms.status IS NULL OR ms.status = 'pending' OR ms.status = 'failed')
    ORDER BY m.id
    LIMIT $1
"""

SQL_COUNT_TOTAL = """
    SELECT count(*) FROM memories
    WHERE is_archived = false AND embedding IS NOT NULL
"""

SQL_COUNT_PENDING = """
    SELECT count(*) FROM memories m
    LEFT JOIN qdrant_migration_status ms ON ms.memory_id = m.id
    WHERE m.is_archived = false
      AND m.embedding IS NOT NULL
      AND (ms.status IS NULL OR ms.status = 'pending' OR ms.status = 'failed')
"""

SQL_MARK_MIGRATED = """
    INSERT INTO qdrant_migration_status (memory_id, status, qdrant_point_id, migrated_at)
    VALUES ($1, 'migrated', $1, now())
    ON CONFLICT (memory_id) DO UPDATE SET
        status = 'migrated',
        qdrant_point_id = EXCLUDED.qdrant_point_id,
        migrated_at = EXCLUDED.migrated_at,
        error_msg = NULL
"""

SQL_MARK_FAILED = """
    INSERT INTO qdrant_migration_status (memory_id, status, error_msg)
    VALUES ($1, 'failed', $2)
    ON CONFLICT (memory_id) DO UPDATE SET
        status = 'failed',
        error_msg = EXCLUDED.error_msg
"""

SQL_ROLLBACK_FETCH = """
    SELECT
        m.id,
        m.user_id,
        m.content,
        m.namespace,
        m.metadata,
        m.importance,
        m.content_hash,
        m.created_at,
        m.updated_at
    FROM memories m
    JOIN qdrant_migration_status ms ON ms.memory_id = m.id
    WHERE ms.status = 'migrated'
    ORDER BY m.id
    LIMIT $1
"""

SQL_ROLLBACK_MARK = """
    UPDATE qdrant_migration_status
    SET status = 'pending', migrated_at = NULL, error_msg = NULL
    WHERE memory_id = $1
"""


# ════════════════════════════════════════════════════════════
# Qdrant: запись
# ════════════════════════════════════════════════════════════
def ensure_collection(client: QdrantClient, collection: str, dim: int) -> None:
    """Создать коллекцию, если не существует."""
    collections = [c.name for c in client.get_collections().collections]
    if collection in collections:
        logger.info("Collection '%s' already exists", collection)
        return

    logger.info("Creating collection '%s' (dim=%d, cosine)", collection, dim)
    client.create_collection(
        collection_name=collection,
        vectors_config=qm.VectorParams(
            size=dim,
            distance=qm.Distance.COSINE,
            on_disk=True,  # 4096-dim × 1.2M ≈ 20GB → on_disk обязателен
        ),
        optimizers_config=qm.OptimizersConfigDiff(
            deleted_threshold=0.2,
            indexing_threshold=20,
            flush_interval_sec=30,
            max_optimization_threads=2,
        ),
        replication_factor=1,  # single-node
    )

    # Payload индексы для фильтрации
    logger.info("Creating payload indexes...")
    client.create_payload_index(
        collection_name=collection,
        field_name="namespace",
        field_schema=qm.PayloadSchemaType.KEYWORD,
    )
    client.create_payload_index(
        collection_name=collection,
        field_name="user_id",
        field_schema=qm.PayloadSchemaType.KEYWORD,
    )
    client.create_payload_index(
        collection_name=collection,
        field_name="importance",
        field_schema=qm.PayloadSchemaType.INTEGER,
    )
    logger.info("Collection '%s' ready", collection)


def build_point(row: asyncpg.Record) -> qm.PointStruct:
    """Преобразовать строку PostgreSQL → Qdrant PointStruct."""
    # embedding: asyncpg возвращает list[float] через pgvector codec
    embedding = list(row["embedding"])

    # Payload: всё кроме вектора
    payload: dict[str, Any] = {
        "user_id": row["user_id"],
        "content": row["content"],
        "namespace": row["namespace"],
        "metadata": row["metadata"] or {},
        "importance": row["importance"] or 3,
        "content_hash": row.get("content_hash"),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }

    # Убираем None из payload (Qdrant не любит null-значения)
    payload = {k: v for k, v in payload.items() if v is not None}

    return qm.PointStruct(
        id=str(row["id"]),  # UUID как строка → Qdrant сам конвертирует
        vector=embedding,
        payload=payload,
    )


# ════════════════════════════════════════════════════════════
# Миграция: экспорт + импорт
# ════════════════════════════════════════════════════════════
async def migrate(config: Config) -> None:
    """Основной процесс миграции."""
    pool = await asyncpg.create_pool(
        config.database_url.replace("postgresql+asyncpg://", "postgresql://"),
        min_size=2,
        max_size=config.workers + 2,
    )

    client = QdrantClient(url=config.qdrant_url, timeout=120)

    # Регистрируем pgvector codec
    async def init_conn(conn):
        from pgvector.asyncpg import register_vector
        await register_vector(conn)

    pool = await asyncpg.create_pool(
        config.database_url.replace("postgresql+asyncpg://", "postgresql://"),
        min_size=2,
        max_size=config.workers + 2,
        init=init_conn,
    )

    ensure_collection(client, config.qdrant_collection, config.embedding_dim)

    # Считаем общее количество
    async with pool.acquire() as conn:
        total_row = await conn.fetchrow(SQL_COUNT_TOTAL)
        total = total_row[0]
        pending_row = await conn.fetchrow(SQL_COUNT_PENDING)
        pending = pending_row[0]

    logger.info("Total vectors: %d | Pending: %d", total, pending)

    if pending == 0:
        logger.info("Nothing to migrate!")
        await pool.close()
        return

    migrated = 0
    failed = 0
    start_time = time.time()

    while True:
        # Читаем батч
        async with pool.acquire() as conn:
            rows = await conn.fetch(SQL_FETCH_BATCH, config.batch_size)

        if not rows:
            break

        # Строим точки
        points = []
        ids_to_mark = []
        for row in rows:
            try:
                point = build_point(row)
                points.append(point)
                ids_to_mark.append(row["id"])
            except Exception as e:
                failed += 1
                logger.error("Failed to build point for %s: %s", row["id"], e)
                async with pool.acquire() as conn:
                    await conn.execute(SQL_MARK_FAILED, str(row["id"]), str(e)[:500])

        # Upsert в Qdrant
        if points:
            try:
                client.upsert(
                    collection_name=config.qdrant_collection,
                    points=points,
                )
                # Помечаем как мигрированные
                async with pool.acquire() as conn:
                    for mid in ids_to_mark:
                        await conn.execute(SQL_MARK_MIGRATED, str(mid))

                migrated += len(points)
            except Exception as e:
                failed += len(points)
                logger.error("Qdrant upsert failed: %s", e)
                async with pool.acquire() as conn:
                    for mid in ids_to_mark:
                        await conn.execute(SQL_MARK_FAILED, str(mid), str(e)[:500])

        # Прогресс
        elapsed = time.time() - start_time
        rate = migrated / elapsed if elapsed > 0 else 0
        pct = (migrated / pending * 100) if pending > 0 else 100
        eta = (pending - migrated) / rate if rate > 0 else 0

        logger.info(
            "Progress: %d/%d (%.1f%%) | rate: %.0f/s | ETA: %.0fs | failed: %d",
            migrated, pending, pct, rate, eta, failed,
        )

    # Финальная статистика
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("Migration complete!")
    logger.info("  Migrated: %d", migrated)
    logger.info("  Failed:   %d", failed)
    logger.info("  Duration: %.1fs", elapsed)
    logger.info("  Rate:     %.0f records/s", migrated / elapsed if elapsed > 0 else 0)
    logger.info("=" * 60)

    # Верификация
    async with pool.acquire() as conn:
        verify_row = await conn.fetchrow("SELECT * FROM verify_qdrant_migration()")
        logger.info(
            "Verification: pending=%d migrated=%d failed=%d skipped=%d complete=%.1f%%",
            verify_row["total_pending"],
            verify_row["total_migrated"],
            verify_row["total_failed"],
            verify_row["total_skipped"],
            verify_row["pct_complete"],
        )

    await pool.close()


# ════════════════════════════════════════════════════════════
# Верификация
# ════════════════════════════════════════════════════════════
async def verify(config: Config) -> None:
    """Сравнение количества записей."""
    pool = await asyncpg.create_pool(
        config.database_url.replace("postgresql+asyncpg://", "postgresql://"),
        min_size=1,
        max_size=2,
    )

    client = QdrantClient(url=config.qdrant_url, timeout=30)

    async with pool.acquire() as conn:
        pg_row = await conn.fetchrow(SQL_COUNT_TOTAL)
        pg_count = pg_row[0]

        verify_row = await conn.fetchrow("SELECT * FROM verify_qdrant_migration()")
        migrated_count = verify_row["total_migrated"]

    try:
        qdrant_info = client.get_collection(config.qdrant_collection)
        qdrant_count = qdrant_info.points_count or 0
    except Exception as e:
        logger.error("Cannot reach Qdrant: %s", e)
        qdrant_count = -1

    logger.info("=" * 60)
    logger.info("Verification Report:")
    logger.info("  PostgreSQL vectors:   %d", pg_count)
    logger.info("  Migration status:     %d migrated, %d pending, %d failed",
                verify_row["total_migrated"], verify_row["total_pending"], verify_row["total_failed"])
    logger.info("  Qdrant points:        %d", qdrant_count)

    if qdrant_count == migrated_count == pg_count:
        logger.info("  ✓ PERFECT MATCH")
    elif qdrant_count == migrated_count:
        logger.info("  ✓ Migration status matches Qdrant (but PG has %d total)", pg_count)
    else:
        logger.warning("  ✗ MISMATCH: PG=%d, migrated=%d, Qdrant=%d", pg_count, migrated_count, qdrant_count)

    logger.info("=" * 60)

    await pool.close()


# ════════════════════════════════════════════════════════════
# Rollback
# ════════════════════════════════════════════════════════════
async def rollback(config: Config) -> None:
    """Откат: удалить все точки из Qdrant, сбросить статус в PG."""
    client = QdrantClient(url=config.qdrant_url, timeout=120)
    pool = await asyncpg.create_pool(
        config.database_url.replace("postgresql+asyncpg://", "postgresql://"),
        min_size=1,
        max_size=2,
    )

    logger.info("Deleting all points from Qdrant collection '%s'...", config.qdrant_collection)
    try:
        client.delete(
            collection_name=config.qdrant_migration_status,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(must=[])  # Все точки
            ),
        )
    except Exception:
        # Если коллекция не существует — просто логируем
        logger.warning("Qdrant collection not found or empty, skipping delete")

    # Сбрасываем статус миграции в PG
    logger.info("Resetting migration status in PostgreSQL...")
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE qdrant_migration_status SET status = 'pending', "
            "migrated_at = NULL, error_msg = NULL "
            "WHERE status = 'migrated'"
        )
        count = int(result.split()[-1])
        logger.info("Reset %d records to 'pending'", count)

    logger.info("Rollback complete!")
    await pool.close()


# ════════════════════════════════════════════════════════════
# Cleanup: удалить колонку embedding
# ════════════════════════════════════════════════════════════
async def cleanup(config: Config) -> None:
    """Удалить колонку embedding после успешной миграции."""
    pool = await asyncpg.create_pool(
        config.database_url.replace("postgresql+asyncpg://", "postgresql://"),
        min_size=1,
        max_size=2,
    )

    # Проверяем, что все мигрированы
    async with pool.acquire() as conn:
        verify_row = await conn.fetchrow("SELECT * FROM verify_qdrant_migration()")
        if verify_row["total_pending"] > 0:
            logger.error(
                "Cannot cleanup: %d records still pending!",
                verify_row["total_pending"],
            )
            await pool.close()
            return

    logger.info("All records migrated. Removing embedding column...")
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE memories DROP COLUMN IF EXISTS embedding")
        await conn.execute("DROP EXTENSION IF EXISTS vector CASCADE")
        await conn.execute("DROP TABLE IF EXISTS qdrant_migration_status CASCADE")
        await conn.execute("DROP FUNCTION IF EXISTS verify_qdrant_migration()")
        await conn.execute("DROP VIEW IF EXISTS qdrant_migration_by_namespace")

    logger.info("Cleanup complete! pgvector fully removed.")
    await pool.close()


# ════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════
COMMANDS = {
    "migrate": migrate,
    "verify": verify,
    "rollback": rollback,
    "cleanup": cleanup,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: python migrate_vectors_to_qdrant.py <command>")
        print("Commands: migrate, verify, rollback, cleanup")
        sys.exit(1)

    config = Config.from_env()
    command = sys.argv[1]

    logger.info("Running '%s' (batch_size=%d, workers=%d)", command, config.batch_size, config.workers)
    asyncio.run(COMMANDS[command](config))


if __name__ == "__main__":
    main()
