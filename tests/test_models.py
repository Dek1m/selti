from datetime import datetime, timezone

from memory_server.models import (
    DeleteResult,
    ForgetResult,
    MemoryInput,
    MemoryListResult,
    MemoryRecord,
    SearchResult,
)


def test_memory_record_create():
    now = datetime.now(timezone.utc)
    record = MemoryRecord(
        id="550e8400-e29b-41d4-a716-446655440000",
        user_id="user_1",
        content="Hello world",
        metadata={"source": "chat"},
        namespace="test",
        created_at=now,
        updated_at=now,
    )
    assert record.id == "550e8400-e29b-41d4-a716-446655440000"
    assert record.user_id == "user_1"
    assert record.content == "Hello world"
    assert record.metadata == {"source": "chat"}
    assert record.namespace == "test"
    assert record.created_at == now
    assert record.updated_at == now


def test_memory_record_defaults():
    now = datetime.now(timezone.utc)
    record = MemoryRecord(
        id="id-1",
        user_id="u1",
        content="c",
        created_at=now,
        updated_at=now,
    )
    assert record.metadata == {}
    assert record.namespace == "default"


def test_memory_record_serialization():
    now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    record = MemoryRecord(
        id="id-1",
        user_id="u1",
        content="Hello",
        metadata={"key": "val"},
        namespace="ns",
        created_at=now,
        updated_at=now,
    )
    dumped = record.model_dump(mode="json")
    assert dumped["id"] == "id-1"
    assert dumped["user_id"] == "u1"
    assert dumped["content"] == "Hello"
    assert dumped["metadata"] == {"key": "val"}
    assert dumped["namespace"] == "ns"
    assert dumped["created_at"] == "2025-06-15T12:00:00+00:00"
    assert dumped["updated_at"] == "2025-06-15T12:00:00+00:00"


def test_memory_input_create():
    inp = MemoryInput(
        content="Some content",
        user_id="u42",
        metadata={"type": "note"},
        namespace="work",
    )
    assert inp.content == "Some content"
    assert inp.user_id == "u42"
    assert inp.metadata == {"type": "note"}
    assert inp.namespace == "work"


def test_memory_input_defaults():
    inp = MemoryInput(content="Hi", user_id="u1")
    assert inp.metadata == {}
    assert inp.namespace == "default"


def test_search_result_create():
    sr = SearchResult(
        id="sr-1",
        content="Found item",
        metadata={"relevance": "high"},
        score=0.95,
    )
    assert sr.id == "sr-1"
    assert sr.content == "Found item"
    assert sr.metadata == {"relevance": "high"}
    assert sr.score == 0.95
    # default factory for metadata
    sr_no_meta = SearchResult(id="sr-2", content="x", score=0.5)
    assert sr_no_meta.metadata == {}


def test_search_result_serialization():
    sr = SearchResult(id="s1", content="text", metadata={"k": "v"}, score=0.8)
    dumped = sr.model_dump(mode="json")
    assert dumped == {"id": "s1", "content": "text", "metadata": {"k": "v"}, "score": 0.8}


def test_memory_list_result():
    now = datetime.now(timezone.utc)
    items = [
        MemoryRecord(id="1", user_id="u1", content="a", created_at=now, updated_at=now),
        MemoryRecord(id="2", user_id="u1", content="b", created_at=now, updated_at=now),
    ]
    result = MemoryListResult(items=items, total=2)
    assert len(result.items) == 2
    assert result.total == 2
    dumped = result.model_dump(mode="json")
    assert len(dumped["items"]) == 2
    assert dumped["total"] == 2


def test_delete_result():
    dr = DeleteResult()
    assert dr.success is True
    dr_false = DeleteResult(success=False)
    assert dr_false.success is False
    dumped = dr.model_dump(mode="json")
    assert dumped == {"success": True}


def test_forget_result():
    fr = ForgetResult(deleted_count=5)
    assert fr.deleted_count == 5
    dumped = fr.model_dump(mode="json")
    assert dumped == {"deleted_count": 5}
