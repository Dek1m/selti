"""Qdrant-only store for vector operations.

Хранит: вектора (4096-dim), payload (фильтры).
НЕ хранит метаданные — для этого PostgreSQLRepository.
"""
from __future__ import annotations

import time
from typing import Optional

from qdrant_client import models as qm

from memory_server.logger import get_logger
from memory_server.metrics import (
    QDRANT_OPS_DURATION_SECONDS,
    QDRANT_OPS_TOTAL,
    QDRANT_SEARCH_RESULTS,
)
from memory_server.vector.circuit_breaker import CircuitBreakerQdrantClient

logger = get_logger(__name__)


class QdrantStore:
    """Vector store — только Qdrant-операции (upsert, search, delete)."""

    def __init__(
        self,
        client: CircuitBreakerQdrantClient,
        collection: str = "memories",
    ):
        self.client = client
        self.collection = collection

    # ════════════════════════════════════════════════════════════
    # UPSERT
    # ════════════════════════════════════════════════════════════

    def upsert_vector(
        self,
        point_id: str,
        vector: list[float],
        payload: dict,
    ) -> None:
        qstart = time.monotonic()
        self.client.upsert(
            collection_name=self.collection,
            points=[
                qm.PointStruct(id=point_id, vector=vector, payload=payload),
            ],
        )
        QDRANT_OPS_TOTAL.labels(operation="upsert").inc()
        QDRANT_OPS_DURATION_SECONDS.labels(operation="upsert").observe(time.monotonic() - qstart)

    def upsert_batch(
        self,
        points: list[qm.PointStruct],
    ) -> None:
        if not points:
            return
        qstart = time.monotonic()
        batch_size = 1000
        for start in range(0, len(points), batch_size):
            self.client.upsert(
                collection_name=self.collection,
                points=points[start : start + batch_size],
            )
        QDRANT_OPS_TOTAL.labels(operation="batch_upsert").inc()
        QDRANT_OPS_DURATION_SECONDS.labels(operation="batch_upsert").observe(time.monotonic() - qstart)

    # ════════════════════════════════════════════════════════════
    # SEARCH
    # ════════════════════════════════════════════════════════════

    def search(
        self,
        query_vector: list[float],
        limit: int = 10,
        score_threshold: float | None = None,
        query_filter: Optional[qm.Filter] = None,
        with_vectors: bool = False,
    ) -> list[dict]:
        """Vector search. Возвращает [{id, score, payload, vector}].

        with_vectors=True — вектора кандидатов для MMR-реранкера (Фаза 1.1);
        payload-диета (D6) на вектора не распространяется, но без нужды
        (обычный поиск) их не запрашиваем.
        """
        qstart = time.monotonic()
        result = self.client.query_points(
            collection_name=self.collection,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
            with_vectors=with_vectors,
        )
        QDRANT_OPS_TOTAL.labels(operation="search").inc()
        QDRANT_OPS_DURATION_SECONDS.labels(operation="search").observe(time.monotonic() - qstart)
        QDRANT_SEARCH_RESULTS.observe(len(result.points))

        return [
            {"id": r.id, "score": r.score, "payload": r.payload, "vector": r.vector}
            for r in result.points
        ]

    # ════════════════════════════════════════════════════════════
    # UPDATE
    # ════════════════════════════════════════════════════════════

    def update_vector(
        self,
        point_id: str,
        vector: list[float],
    ) -> None:
        qstart = time.monotonic()
        self.client.update_vectors(
            collection_name=self.collection,
            points=[qm.PointVectors(id=point_id, vector=vector)],
        )
        QDRANT_OPS_TOTAL.labels(operation="update_vector").inc()
        QDRANT_OPS_DURATION_SECONDS.labels(operation="update_vector").observe(time.monotonic() - qstart)

    def set_payload(
        self,
        point_id: str,
        payload: dict,
    ) -> None:
        qstart = time.monotonic()
        self.client.set_payload(
            collection_name=self.collection,
            payload=payload,
            points=[point_id],
        )
        QDRANT_OPS_TOTAL.labels(operation="set_payload").inc()
        QDRANT_OPS_DURATION_SECONDS.labels(operation="set_payload").observe(time.monotonic() - qstart)

    # ════════════════════════════════════════════════════════════
    # DELETE
    # ════════════════════════════════════════════════════════════

    def delete(self, point_ids: list[str]) -> None:
        if not point_ids:
            return
        qstart = time.monotonic()
        batch_size = 1000
        for start in range(0, len(point_ids), batch_size):
            self.client.delete(
                collection_name=self.collection,
                points_selector=qm.PointIdsList(
                    points=point_ids[start : start + batch_size]
                ),
            )
        QDRANT_OPS_TOTAL.labels(operation="delete").inc()
        QDRANT_OPS_DURATION_SECONDS.labels(operation="delete").observe(time.monotonic() - qstart)

    def delete_by_filter(self, query_filter: qm.Filter) -> list[str]:
        """Scroll + delete по фильтру. Возвращает удалённые ID."""
        ids: list[str] = []
        offset = None
        while True:
            points, next_offset = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=query_filter,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.extend(p.id for p in points)
            if next_offset is None:
                break
            offset = next_offset

        if ids:
            self.delete(ids)
        return ids

    # ════════════════════════════════════════════════════════════
    # FILTER BUILDER
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def build_filter(
        user_id: str | None = None,
        namespace_id: str | None = None,
        project_id: str | None = None,
        active_only: bool = True,
    ) -> qm.Filter | None:
        """Фильтр под payload-диету (D6): user_id/namespace_id/project_id — UUID-строки.

        active_only=True добавляет фильтр актуальности status='asserted'
        (valid_to в payload не хранится: retracted/superseded закрывают окно
        и исключаются статусом). False — для массовых операций (forget),
        где статус не важен.
        """
        conditions: list[qm.Condition] = []
        if user_id:
            conditions.append(qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)))
        if namespace_id:
            conditions.append(qm.FieldCondition(key="namespace_id", match=qm.MatchValue(value=str(namespace_id))))
        if project_id:
            conditions.append(qm.FieldCondition(key="project_id", match=qm.MatchValue(value=str(project_id))))
        if active_only:
            conditions.append(qm.FieldCondition(key="status", match=qm.MatchValue(value="asserted")))
        return qm.Filter(must=conditions) if conditions else None
