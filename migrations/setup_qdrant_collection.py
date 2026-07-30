#!/usr/bin/env python3
"""
setup_qdrant_collection.py — Создание и настройка коллекции Qdrant для memories.

Запускать ПЕРЕД миграцией данных.

Использование:
    python setup_qdrant_collection.py              # создать коллекцию
    python setup_qdrant_collection.py --recreate   # пересоздать (удалить + создать)
    python setup_qdrant_collection.py --info       # информация о коллекции
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models as qm

load_dotenv()

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "memories")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIMENSION", "4096"))


def create_collection(client: QdrantClient, recreate: bool = False) -> None:
    """Создать коллекцию memories с оптимальной конфигурацией."""
    collections = [c.name for c in client.get_collections().collections]

    if COLLECTION in collections:
        if recreate:
            logger.info("Deleting existing collection '%s'...", COLLECTION)
            client.delete_collection(COLLECTION)
        else:
            logger.info("Collection '%s' already exists. Use --recreate to reset.", COLLECTION)
            return

    logger.info("Creating collection '%s'", COLLECTION)
    logger.info("  Vector dim: %d", EMBEDDING_DIM)
    logger.info("  Distance:   COSINE")
    logger.info("  On-disk:    yes (recommended for 4096-dim × 1M+ points)")

    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=qm.VectorParams(
            size=EMBEDDING_DIM,
            distance=qm.Distance.COSINE,
            on_disk=True,
        ),
        optimizers_config=qm.OptimizersConfigDiff(
            # Порог удалённых точек для запуска оптимизации
            # 0.2 = оптимизация при 20% удалённых
            deleted_threshold=0.2,
            # Порог индексации: % сегментов для запуска HNSW
            indexing_threshold=20,
            # Интервал flush на диск
            flush_interval_sec=30,
            # Максимум потоков для оптимизации
            max_optimization_threads=2,
            # Количество сегментов на shard
            segments_count=2,
        ),
        # Single-node, replication=1
        replication_factor=1,
        # Размер страницы для HNSW
       hnsw_config=qm.HnswConfigDiff(
            m=16,            # Количество рёбер в графе (по умолчанию 16)
            ef_construct=100, # Размер candidate pool при построении
            full_scan_threshold=10000,  # Порог для переключения на full scan
        ),
    )

    # ── Payload индексы ──
    # Ускоряют фильтрацию: namespace=?, user_id=?, importance>=?
    # Без них Qdrant делает полный scan по payload
    logger.info("Creating payload indexes...")

    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="namespace",
        field_schema=qm.PayloadSchemaType.KEYWORD,
    )
    logger.info("  + namespace (KEYWORD)")

    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="user_id",
        field_schema=qm.PayloadSchemaType.KEYWORD,
    )
    logger.info("  + user_id (KEYWORD)")

    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="importance",
        field_schema=qm.PayloadSchemaType.INTEGER,
    )
    logger.info("  + importance (INTEGER)")

    logger.info("Collection '%s' created successfully!", COLLECTION)


def collection_info(client: QdrantClient) -> None:
    """Показать информацию о коллекции."""
    try:
        info = client.get_collection(COLLECTION)
        logger.info("Collection: %s", COLLECTION)
        logger.info("  Status:      %s", info.status)
        logger.info("  Points:      %d", info.points_count or 0)
        logger.info("  Vectors:     %s", info.vectors_count)
        logger.info("  Segments:    %s", info.segments_count)
        logger.info("  Optimizer:   %s", info.optimizer_status)

        # Payload indexes
        logger.info("  Payload indexes:")
        for name, idx in (info.payload_schema or {}).items():
            logger.info("    - %s: %s", name, idx.data_type)
    except Exception as e:
        logger.error("Collection '%s' not found: %s", COLLECTION, e)


import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("setup")


def main() -> None:
    client = QdrantClient(url=QDRANT_URL, timeout=30)

    if "--info" in sys.argv:
        collection_info(client)
    elif "--recreate" in sys.argv:
        create_collection(client, recreate=True)
    else:
        create_collection(client, recreate=False)


if __name__ == "__main__":
    main()
