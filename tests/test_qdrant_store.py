"""Юнит-тесты QdrantStore.build_filter — фильтр под payload-диету (D6).

build_filter — чистая функция: строит qdrant_client.models.Filter из
user_id/namespace_id/project_id/active_only. Qdrant-клиент не дёргается.
"""
from qdrant_client import models as qm

from memory_server.memory.qdrant_store import QdrantStore


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