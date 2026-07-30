from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import QdrantClient, models


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
        await self.client.upsert(
            collection_name=self.collection,
            points=[models.PointStruct(id=id, vector=vector, payload=payload)],
        )

    async def batch_upsert(self, ids: list[str], vectors: list[list[float]], payloads: list[dict]) -> None:
        points = [models.PointStruct(id=i, vector=v, payload=p) for i, v, p in zip(ids, vectors, payloads)]
        for offset in range(0, len(points), 200):
            await self.client.upsert(collection_name=self.collection, points=points[offset:offset + 200])

    async def search(
        self,
        vector: list[float],
        limit: int = 10,
        score_threshold: float | None = None,
        query_filter: models.Filter | None = None,
    ) -> list[SearchResult]:
        results = await self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
        )
        return [SearchResult(id=str(p.id), score=p.score, payload=p.payload or {}) for p in results.points]

    async def delete(self, ids: list[str]) -> None:
        if ids:
            await self.client.delete(
                collection_name=self.collection,
                points_selector=models.PointIdsList(points=ids),
            )

    async def delete_by_filter(self, query_filter: models.Filter) -> None:
        await self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(filter=query_filter),
        )
