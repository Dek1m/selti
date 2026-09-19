"""Юнит-тесты QdrantStore.build_filter — фильтр под payload-диету (D6).

build_filter — чистая функция: строит qdrant_client.models.Filter из
user_id/namespace_id/project_id/active_only. Qdrant-клиент не дёргается.

Batch-операции кластеризации v2 (search_batch/retrieve_vectors) — на
моке CircuitBreakerQdrantClient: проверяем нарезку батчей, порядок
выдач и метрики; исключения наружу (см. docstring search_batch).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from qdrant_client import models as qm

from memory_server.memory.qdrant_store import (
    QUERY_BATCH_SIZE,
    RETRIEVE_BATCH_SIZE,
    QdrantStore,
)


def test_build_filter_empty_returns_none():
    """Пустой вызов без active_only → нет условий, фильтр отсутствует (None)."""
    result = QdrantStore.build_filter(active_only=False)
    assert result is None


def test_build_filter_user_id():
    result = QdrantStore.build_filter(user_id="u42", active_only=False)
    assert isinstance(result, qm.Filter)
    assert len(result.must) == 1
    cond = result.must[0]
    assert cond.key == "user_id"
    assert isinstance(cond.match, qm.MatchValue)
    assert cond.match.value == "u42"


def test_build_filter_namespace_id():
    result = QdrantStore.build_filter(namespace_id="ns-1", active_only=False)
    assert len(result.must) == 1
    cond = result.must[0]
    assert cond.key == "namespace_id"
    assert isinstance(cond.match, qm.MatchValue)
    assert cond.match.value == "ns-1"


def test_build_filter_project_id():
    result = QdrantStore.build_filter(project_id="prj-1", active_only=False)
    assert len(result.must) == 1
    cond = result.must[0]
    assert cond.key == "project_id"
    assert isinstance(cond.match, qm.MatchValue)
    assert cond.match.value == "prj-1"


def test_build_filter_active_only_true():
    """active_only=True добавляет условие актуальности status='asserted'."""
    result = QdrantStore.build_filter(active_only=True)
    assert isinstance(result, qm.Filter)
    assert len(result.must) == 1
    cond = result.must[0]
    assert cond.key == "status"
    assert isinstance(cond.match, qm.MatchValue)
    assert cond.match.value == "asserted"


def test_build_filter_all_with_active_only():
    """Комбинация user_id+namespace_id+project_id+active_only → 4 условия."""
    result = QdrantStore.build_filter(
        user_id="u1",
        namespace_id="ns-1",
        project_id="prj-1",
        active_only=True,
    )
    assert isinstance(result, qm.Filter)
    assert len(result.must) == 4
    by_key = {c.key: c.match.value for c in result.must}
    assert by_key == {
        "user_id": "u1",
        "namespace_id": "ns-1",
        "project_id": "prj-1",
        "status": "asserted",
    }
    # каждый д.б. FieldCondition с MatchValue — не range/geo и пр.
    for c in result.must:
        assert isinstance(c, qm.FieldCondition)
        assert isinstance(c.match, qm.MatchValue)


# ══════════════════════════════════════════════════════════════════
# Batch-операции кластеризации v2 (Фаза 2.3): search_batch / retrieve_vectors
# ══════════════════════════════════════════════════════════════════


def _response(point_ids_scores: list[tuple[str, float]]):
    """QueryResponse-подобный объект с .points (id, score, payload)."""
    return SimpleNamespace(
        points=[
            SimpleNamespace(id=pid, score=score, payload=None)
            for pid, score in point_ids_scores
        ]
    )


class TestSearchBatch:
    def test_splits_into_query_batches_preserving_order(self):
        """QUERY_BATCH_SIZE+1 запросов → 2 вызова query_batch_points;
        выдачи возвращаются строго в порядке query_vectors."""
        client = MagicMock()
        client.query_batch_points = MagicMock(
            side_effect=[
                [_response([("a", 0.9)])],
                [_response([("b", 0.8)])],
            ]
        )
        store = QdrantStore(client=client, collection="memories")

        results = store.search_batch(
            query_vectors=[[0.1]] * (QUERY_BATCH_SIZE + 1),
            limit=11,
            score_threshold=0.92,
        )

        assert client.query_batch_points.call_count == 2
        first_chunk, second_chunk = (
            c.kwargs["requests"] for c in client.query_batch_points.call_args_list
        )
        assert len(first_chunk) == QUERY_BATCH_SIZE
        assert len(second_chunk) == 1
        # порядок выдач = порядок входных векторов
        assert [[p["id"] for p in batch] for batch in results] == [["a"], ["b"]]

    def test_request_params_passed_through(self):
        client = MagicMock()
        client.query_batch_points = MagicMock(return_value=[_response([("a", 0.99)])])
        store = QdrantStore(client=client, collection="memories")
        query_filter = QdrantStore.build_filter(namespace_id="ns-1", active_only=True)

        store.search_batch(
            query_vectors=[[0.2, 0.3]],
            limit=11,
            score_threshold=0.92,
            query_filter=query_filter,
        )

        call = client.query_batch_points.call_args
        assert call.kwargs["collection_name"] == "memories"
        request = call.kwargs["requests"][0]
        assert isinstance(request, qm.QueryRequest)
        assert request.query == [0.2, 0.3]
        assert request.limit == 11
        assert request.score_threshold == 0.92
        assert request.filter is query_filter
        assert request.with_payload is False  # payload-диета: id и score достаточно

    def test_empty_input_no_calls(self):
        client = MagicMock()
        store = QdrantStore(client=client, collection="memories")

        assert store.search_batch(query_vectors=[]) == []
        client.query_batch_points.assert_not_called()

    def test_exception_propagates_not_swallowed(self):
        """Кластеризация v2 обязана отличать «Qdrant недоступен» от пустого
        результата — исключение не глотается (VectorStoreError поднимет фасад)."""
        client = MagicMock()
        client.query_batch_points = MagicMock(
            side_effect=ConnectionError("qdrant unreachable")
        )
        store = QdrantStore(client=client, collection="memories")

        try:
            store.search_batch(query_vectors=[[0.1]])
        except ConnectionError:
            pass
        else:
            raise AssertionError("exception must propagate")


class TestRetrieveVectors:
    def test_batches_and_maps_id_to_vector(self):
        client = MagicMock()
        # RETRIEVE_BATCH_SIZE+1 id → 2 вызова; b — точка без вектора (в карту не идёт)
        ids = [f"p{i}" for i in range(RETRIEVE_BATCH_SIZE)] + ["tail"]
        client.retrieve = MagicMock(
            side_effect=[
                [SimpleNamespace(id=i, vector=[0.1, 0.2]) for i in ids[:-1]]
                + [SimpleNamespace(id="b", vector=None)],
                [SimpleNamespace(id="tail", vector=[0.3])],
            ]
        )
        store = QdrantStore(client=client, collection="memories")

        vectors = store.retrieve_vectors(ids)  # порядок сохранён

        assert client.retrieve.call_count == 2
        first, second = (
            c.kwargs["ids"] for c in client.retrieve.call_args_list
        )
        assert first == ids[:-1]
        assert second == ["tail"]  # дожали остаток меньше RETRIEVE_BATCH_SIZE
        assert vectors["p0"] == [0.1, 0.2]
        assert vectors["tail"] == [0.3]
        assert "b" not in vectors  # точка без вектора — missing, не None

    def test_retrieve_params(self):
        client = MagicMock()
        client.retrieve = MagicMock(return_value=[])
        store = QdrantStore(client=client, collection="memories")

        store.retrieve_vectors(["a"])

        kwargs = client.retrieve.call_args.kwargs
        assert kwargs["collection_name"] == "memories"
        assert kwargs["with_payload"] is False
        assert kwargs["with_vectors"] is True

    def test_empty_input_no_calls(self):
        client = MagicMock()
        store = QdrantStore(client=client, collection="memories")

        assert store.retrieve_vectors([]) == {}
        client.retrieve.assert_not_called()

    def test_batch_size_upper_bound(self):
        """Контракт нарезки: батч не длиннее RETRIEVE_BATCH_SIZE (512M-воркер)."""
        assert RETRIEVE_BATCH_SIZE == 256
        assert QUERY_BATCH_SIZE == 64