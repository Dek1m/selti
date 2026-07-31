from __future__ import annotations

import time
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from memory_server.metrics import (
    QDRANT_OPS_TOTAL,
    QDRANT_OPS_DURATION_SECONDS,
    QDRANT_SEARCH_RESULTS,
)


@dataclass
class SearchResult:
    id: str
    score: float
    payload: dict


class QdrantVectorStore:
    def __init__(self, client: QdrantClient, collection: str):
        self.client = client
        self.collection = collection

    async def upsert(self, id: str, vector: list[float], payload: dict) -> None:
        start = time.monotonic()
        try:
            await self.client.upsert(
                collection_name=self.collection,
                points=[models.PointStruct(id=id, vector=vector, payload=payload)],
            )
            duration = time.monotonic() - start
            QDRANT_OPS_TOTAL.labels(operation="upsert").inc()
            QDRANT_OPS_DURATION_SECONDS.labels(operation="upsert").observe(duration)
        except Exception:
            QDRANT_OPS_TOTAL.labels(operation="upsert").inc()
            raise

    async def batch_upsert(self, ids: list[str], vectors: list[list[float]], payloads: list[dict]) -> None:
        points = [models.PointStruct(id=i, vector=v, payload=p) for i, v, p in zip(ids, vectors, payloads)]
        start = time.monotonic()
        try:
            for offset in range(0, len(points), 200):
                await self.client.upsert(collection_name=self.collection, points=points[offset:offset + 200])
            duration = time.monotonic() - start
            QDRANT_OPS_TOTAL.labels(operation="batch_upsert").inc()
            QDRANT_OPS_DURATION_SECONDS.labels(operation="batch_upsert").observe(duration)
        except Exception:
            QDRANT_OPS_TOTAL.labels(operation="batch_upsert").inc()
            raise

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        score_threshold: float | None = None,
        query_filter: models.Filter | None = None,
    ) -> list[SearchResult]:
        start = time.monotonic()
        try:
            results = await self.client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                score_threshold=score_threshold,
            )
            duration = time.monotonic() - start
            QDRANT_OPS_TOTAL.labels(operation="search").inc()
            QDRANT_OPS_DURATION_SECONDS.labels(operation="search").observe(duration)
            QDRANT_SEARCH_RESULTS.observe(len(results.points))
            return [SearchResult(id=str(p.id), score=p.score, payload=p.payload or {}) for p in results.points]
        except Exception:
            QDRANT_OPS_TOTAL.labels(operation="search").inc()
            raise

    async def delete(self, ids: list[str]) -> None:
        if ids:
            start = time.monotonic()
            try:
                await self.client.delete(
                    collection_name=self.collection,
                    points_selector=models.PointIdsList(points=ids),
                )
                duration = time.monotonic() - start
                QDRANT_OPS_TOTAL.labels(operation="delete").inc()
                QDRANT_OPS_DURATION_SECONDS.labels(operation="delete").observe(duration)
            except Exception:
                QDRANT_OPS_TOTAL.labels(operation="delete").inc()
                raise

    async def delete_by_filter(self, query_filter: models.Filter) -> None:
        start = time.monotonic()
        try:
            await self.client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(filter=query_filter),
            )
            duration = time.monotonic() - start
            QDRANT_OPS_TOTAL.labels(operation="delete").inc()
            QDRANT_OPS_DURATION_SECONDS.labels(operation="delete").observe(duration)
        except Exception:
            QDRANT_OPS_TOTAL.labels(operation="delete").inc()
            raise
