#!/usr/bin/env python3
"""
backfill_qdrant.py — Backfill Qdrant with missing embeddings.

Reads records from PostgreSQL that are NOT in Qdrant,
generates embeddings via the API, and upserts them.

Usage:
    python backfill_qdrant.py [--dry-run] [--batch-size 50]

Requires:
    pip install asyncpg httpx qdrant-client
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import asyncpg
import httpx

try:
    from qdrant_client import QdrantClient, models as qm
except ImportError:
    print("ERROR: pip install qdrant-client")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")

# ── Config from env ──
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/memory")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "memories")
EMBEDDING_API_URL = os.environ.get("EMBEDDING_API_URL", "http://10.0.0.21:8080/v1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding-8b")
BATCH_SIZE = int(os.environ.get("BACKFILL_BATCH_SIZE", "50"))
DRY_RUN = "--dry-run" in sys.argv


async def get_qdrant_ids(client: QdrantClient, collection: str) -> set[str]:
    """Scroll all Qdrant points and return their IDs."""
    ids = set()
    offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.update(str(p.id) for p in points)
        if next_offset is None:
            break
        offset = next_offset
    return ids


async def generate_embeddings(
    http: httpx.AsyncClient, texts: list[str]
) -> list[list[float]]:
    """Call embedding API for a batch of texts."""
    response = await http.post(
        f"{EMBEDDING_API_URL}/embeddings",
        json={"model": EMBEDDING_MODEL, "input": texts},
    )
    response.raise_for_status()
    data = response.json()
    # Sort by index to maintain order
    items = sorted(data["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in items]


async def backfill():
    log.info("=" * 60)
    log.info("Qdrant Backfill — starting")
    log.info("  PG:       %s", DATABASE_URL.split("@")[-1])
    log.info("  Qdrant:   %s", QDRANT_URL)
    log.info("  Batch:    %d", BATCH_SIZE)
    log.info("  Dry run:  %s", DRY_RUN)
    log.info("=" * 60)

    # Connect to PG
    pool = await asyncpg.create_pool(
        DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"),
        min_size=2,
        max_size=4,
    )

    # Connect to Qdrant
    qdrant = QdrantClient(url=QDRANT_URL, timeout=60)

    # Connect to embedding API
    async with httpx.AsyncClient(timeout=120.0) as http:
        # Step 1: Get Qdrant IDs
        log.info("Fetching Qdrant point IDs...")
        qdrant_ids = await get_qdrant_ids(qdrant, QDRANT_COLLECTION)
        log.info("Qdrant has %d points", len(qdrant_ids))

        # Step 2: Get all PG IDs (asserted: status='asserted' AND valid_to IS NULL)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id::text FROM memories "
                "WHERE status = 'asserted' AND valid_to IS NULL"
            )
        pg_ids = {str(r["id"]) for r in rows}
        log.info("PostgreSQL has %d active records", len(pg_ids))

        # Step 3: Find missing
        missing_ids = pg_ids - qdrant_ids
        log.info("Missing from Qdrant: %d records", len(missing_ids))

        if not missing_ids:
            log.info("Nothing to backfill!")
            await pool.close()
            return

        if DRY_RUN:
            log.info("DRY RUN — would backfill %d records", len(missing_ids))
            await pool.close()
            return

        # Step 4: Backfill in batches
        missing_list = sorted(missing_ids)
        start_time = time.time()
        total_backfilled = 0
        total_failed = 0

        for i in range(0, len(missing_list), BATCH_SIZE):
            batch_ids = missing_list[i : i + BATCH_SIZE]

            # Fetch content from PG
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT m.id::text, m.user_id, m.content, m.namespace_id::text,
                              m.project_id::text, m.status, m.importance, m.content_hash
                       FROM memories m
                       WHERE m.id::text = ANY($1)
                         AND m.status = 'asserted' AND m.valid_to IS NULL
                         AND m.content IS NOT NULL""",
                    batch_ids,
                )

            if not rows:
                continue

            texts = [r["content"] for r in rows]

            # Generate embeddings
            try:
                embeddings = await generate_embeddings(http, texts)
            except Exception as e:
                log.error("Embedding API failed for batch %d: %s", i // BATCH_SIZE, e)
                total_failed += len(rows)
                continue

            # Build Qdrant points: payload на диете (D6) — только фильтруемые
            # поля, БЕЗ content/metadata/namespace-строки. Полная перезаливка
            # = setup_qdrant_collection.py --recreate + этот скрипт.
            points = []
            for row, emb in zip(rows, embeddings):
                payload = {
                    "user_id": row["user_id"],
                    "namespace_id": row["namespace_id"],
                    "status": row["status"],
                    "importance": row["importance"] or 3,
                }
                if row["project_id"]:
                    payload["project_id"] = row["project_id"]
                if row["content_hash"]:
                    payload["content_hash"] = row["content_hash"]

                points.append(
                    qm.PointStruct(
                        id=row["id"],
                        vector=emb,
                        payload=payload,
                    )
                )

            # Upsert
            try:
                qdrant.upsert(
                    collection_name=QDRANT_COLLECTION,
                    points=points,
                )
                total_backfilled += len(points)
            except Exception as e:
                log.error("Qdrant upsert failed: %s", e)
                total_failed += len(points)

            # Progress
            elapsed = time.time() - start_time
            rate = total_backfilled / elapsed if elapsed > 0 else 0
            pct = (total_backfilled / len(missing_ids)) * 100
            log.info(
                "Progress: %d/%d (%.1f%%) | rate: %.0f/s | failed: %d",
                total_backfilled, len(missing_ids), pct, rate, total_failed,
            )

        # Final stats
        elapsed = time.time() - start_time
        log.info("=" * 60)
        log.info("Backfill complete!")
        log.info("  Backfilled: %d", total_backfilled)
        log.info("  Failed:     %d", total_failed)
        log.info("  Duration:   %.1fs", elapsed)
        log.info("  Rate:       %.0f records/s", total_backfilled / elapsed if elapsed > 0 else 0)

        # Verify
        final_qdrant = await get_qdrant_ids(qdrant, QDRANT_COLLECTION)
        log.info("  Qdrant now: %d points (was %d)", len(final_qdrant), len(qdrant_ids))
        log.info("=" * 60)

    await pool.close()


if __name__ == "__main__":
    asyncio.run(backfill())
