"""Circuit Breaker wrapper for QdrantClient.

Паттерн: декоратор-обёртка над QdrantClient.
При circuit open — возвращает fallback-значения (пустые результаты).
Единообразен с тем, что будет для EmbeddingClient.
"""

import logging
from typing import Optional

from circuitbreaker import CircuitBreaker, CircuitBreakerError
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from qdrant_client.http.models.models import QueryResponse as HttpQueryResponse
from qdrant_client.http.models.models import UpdateStatus

from memory_server.metrics import QDRANT_CB_STATE

logger = logging.getLogger(__name__)

# ── Circuit Breaker настройки ──
# failure_threshold: сколько ошибок подряд → opening
# recovery_timeout: секунд до попытки half-open
# half_open_max_calls: сколько вызовов в half-open для проверки
_cb = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30,
    half_open_max_calls=2,
    name="qdrant",
)


def _on_state_change(cb: CircuitBreaker, old_state: str, new_state: str) -> None:
    QDRANT_CB_STATE.set(1 if new_state == "open" else 0)
    logger.warning(
        "Qdrant circuit breaker state changed",
        extra={"old": old_state, "new": new_state},
    )


_cb.add_state_change_listener(_on_state_change)


class CircuitBreakerQdrantClient:
    """Обёртка над QdrantClient с Circuit Breaker.

    При circuit open:
    - search → пустой список результатов
    - upsert/delete/scroll → skip (no-op)
    - update_vectors/set_payload → skip
    """

    def __init__(self, client: QdrantClient):
        self._client = client
        # Инициализируем gauge текущим состоянием
        QDRANT_CB_STATE.set(0)

    @property
    def state(self) -> str:
        return self._cb.state

    @property
    def _cb(self) -> CircuitBreaker:
        return _cb

    # ════════════════════════════════════════════════════════════
    # Query (search)
    # ════════════════════════════════════════════════════════════

    def query_points(
        self,
        collection_name: str,
        query: list[float],
        query_filter: Optional[qm.Filter] = None,
        limit: int = 10,
        score_threshold: Optional[float] = None,
        **kwargs,
    ) -> qm.QueryResponse:
        try:
            with _cb:
                return self._client.query_points(
                    collection_name=collection_name,
                    query=query,
                    query_filter=query_filter,
                    limit=limit,
                    score_threshold=score_threshold,
                    **kwargs,
                )
        except CircuitBreakerError:
            logger.warning("Qdrant query_points: circuit open, returning empty results")
            return HttpQueryResponse(points=[])

    # ════════════════════════════════════════════════════════════
    # Upsert
    # ════════════════════════════════════════════════════════════

    def upsert(
        self,
        collection_name: str,
        points: list[qm.PointStruct],
        **kwargs,
    ) -> qm.UpdateResult:
        try:
            with _cb:
                return self._client.upsert(
                    collection_name=collection_name,
                    points=points,
                    **kwargs,
                )
        except CircuitBreakerError:
            logger.warning("Qdrant upsert: circuit open, skipping")
            return qm.UpdateResult(operation_id=0, status=UpdateStatus.ACKNOWLEDGED)

    # ════════════════════════════════════════════════════════════
    # Delete
    # ════════════════════════════════════════════════════════════

    def delete(
        self,
        collection_name: str,
        points_selector: qm.PointsSelector,
        **kwargs,
    ) -> qm.UpdateResult:
        try:
            with _cb:
                return self._client.delete(
                    collection_name=collection_name,
                    points_selector=points_selector,
                    **kwargs,
                )
        except CircuitBreakerError:
            logger.warning("Qdrant delete: circuit open, skipping")
            return qm.UpdateResult(operation_id=0, status=UpdateStatus.ACKNOWLEDGED)

    # ════════════════════════════════════════════════════════════
    # Update vectors
    # ════════════════════════════════════════════════════════════

    def update_vectors(
        self,
        collection_name: str,
        points: list[qm.PointVectors],
        **kwargs,
    ) -> qm.UpdateResult:
        try:
            with _cb:
                return self._client.update_vectors(
                    collection_name=collection_name,
                    points=points,
                    **kwargs,
                )
        except CircuitBreakerError:
            logger.warning("Qdrant update_vectors: circuit open, skipping")
            return qm.UpdateResult(operation_id=0, status=UpdateStatus.ACKNOWLEDGED)

    # ════════════════════════════════════════════════════════════
    # Set payload
    # ════════════════════════════════════════════════════════════

    def set_payload(
        self,
        collection_name: str,
        payload: dict,
        points: list[qm.PointId],
        **kwargs,
    ) -> qm.UpdateResult:
        try:
            with _cb:
                return self._client.set_payload(
                    collection_name=collection_name,
                    payload=payload,
                    points=points,
                    **kwargs,
                )
        except CircuitBreakerError:
            logger.warning("Qdrant set_payload: circuit open, skipping")
            return qm.UpdateResult(operation_id=0, status=UpdateStatus.ACKNOWLEDGED)

    # ════════════════════════════════════════════════════════════
    # Scroll
    # ════════════════════════════════════════════════════════════

    def scroll(
        self,
        collection_name: str,
        scroll_filter: Optional[qm.Filter] = None,
        limit: int = 10,
        offset: Optional[qm.PointId] = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        **kwargs,
    ) -> tuple[list[qm.PointStruct], Optional[qm.PointId]]:
        try:
            with _cb:
                return self._client.scroll(
                    collection_name=collection_name,
                    scroll_filter=scroll_filter,
                    limit=limit,
                    offset=offset,
                    with_payload=with_payload,
                    with_vectors=with_vectors,
                    **kwargs,
                )
        except CircuitBreakerError:
            logger.warning("Qdrant scroll: circuit open, returning empty")
            return ([], None)

    # ════════════════════════════════════════════════════════════
    # Proxy methods (без CB, для совместимости)
    # ════════════════════════════════════════════════════════════

    def close(self) -> None:
        self._client.close()

    def __getattr__(self, name: str):
        """Прокси для остальных методов QdrantClient (create_collection и т.д.)."""
        return getattr(self._client, name)
