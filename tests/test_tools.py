from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Namespace
from memory_server.memory.dedup import DedupAction
from memory_server.models import MemoryRecord, MemoryStatsItem, SearchResult
from memory_server.tools.memory_tools import (
    _validate_namespace,
    memory_find_similar,
    memory_ingest_batch,
    memory_stats,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_record(
    record_id: str,
    user_id: str = "u1",
    content: str = "test",
    namespace: str = "default",
) -> MemoryRecord:
    now = datetime.now(timezone.utc)
    return MemoryRecord(
        id=record_id,
        user_id=user_id,
        content=content,
        metadata={},
        namespace=namespace,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def mock_service():
    service = MagicMock()
    service.store = AsyncMock()
    service.search = AsyncMock()
    service.get_stats = AsyncMock()
    return service


@pytest.fixture
def mock_ctx(mock_service):
    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = {"service": mock_service}
    return ctx


# ---------------------------------------------------------------------------
# Namespace validation
# ---------------------------------------------------------------------------


class TestNamespaceValidation:
    def test_valid_namespace(self):
        """Валидный namespace проходит без ошибок."""
        _validate_namespace("default")
        _validate_namespace("user_facts")
        _validate_namespace("code_knowledge")
        _validate_namespace("dialogue_insights")
        _validate_namespace("project_meta")

    def test_invalid_namespace_raises(self):
        """Невалидный namespace выбрасывает ValueError."""
        with pytest.raises(ValueError, match="Invalid namespace"):
            _validate_namespace("i_do_not_exist")

    def test_none_namespace_passes(self):
        """None (дефолт) проходит без ошибок."""
        _validate_namespace(None)


# ---------------------------------------------------------------------------
# memory_ingest_batch
# ---------------------------------------------------------------------------


class TestMemoryIngestBatch:
    @pytest.mark.asyncio
    async def test_batch_empty_list(self, mock_ctx):
        """Пустой список entries → пустой результат."""
        result = await memory_ingest_batch(entries=[], user_id="u1", ctx=mock_ctx)

        assert result == {
            "results": [],
            "summary": {"insert": 0, "skip": 0, "update": 0},
        }

    @pytest.mark.asyncio
    async def test_batch_single_entry(self, mock_ctx, mock_service):
        """Один entry → результат как от обычного store."""
        record = _make_record("mem-1")
        mock_service.store.return_value = (record, DedupAction.INSERT)

        result = await memory_ingest_batch(
            entries=[{"content": "test"}],
            user_id="u1",
            ctx=mock_ctx,
        )

        assert len(result["results"]) == 1
        assert result["results"][0] == {
            "id": "mem-1",
            "action": "insert",
            "namespace": "default",
        }
        assert result["summary"] == {"insert": 1, "skip": 0, "update": 0}

        mock_service.store.assert_awaited_once_with(
            content="test",
            user_id="u1",
            metadata=None,
            namespace=None,
        )

    @pytest.mark.asyncio
    async def test_batch_multiple_entries(self, mock_ctx, mock_service):
        """Несколько entries → корректный summary (inserted/skipped)."""
        record1 = _make_record("mem-1")
        record2 = _make_record("mem-2")
        mock_service.store = AsyncMock(side_effect=[
            (record1, DedupAction.INSERT),
            (record2, DedupAction.SKIP),
        ])

        result = await memory_ingest_batch(
            entries=[
                {"content": "first", "namespace": "default"},
                {"content": "second", "namespace": "user_facts"},
            ],
            user_id="u1",
            ctx=mock_ctx,
        )

        assert result["summary"] == {"insert": 1, "skip": 1, "update": 0}
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_batch_invalid_namespace_raises(self, mock_ctx):
        """С невалидным namespace → ошибка ValueError."""
        with pytest.raises(ValueError, match="Invalid namespace"):
            await memory_ingest_batch(
                entries=[{"content": "test", "namespace": "bad_ns"}],
                user_id="u1",
                ctx=mock_ctx,
            )


# ---------------------------------------------------------------------------
# memory_stats
# ---------------------------------------------------------------------------


class TestMemoryStats:
    @pytest.mark.asyncio
    async def test_stats_empty(self, mock_ctx, mock_service):
        """Пустая статистика → пустой список."""
        mock_service.get_stats.return_value = []

        result = await memory_stats(user_id="u1", ctx=mock_ctx)

        assert result == []
        mock_service.get_stats.assert_awaited_once_with("u1")

    @pytest.mark.asyncio
    async def test_stats_with_data(self, mock_ctx, mock_service):
        """С записями → статистика группируется по namespace."""
        now = datetime.now(timezone.utc)
        items = [
            MemoryStatsItem(namespace="default", count=5, last_updated=now),
            MemoryStatsItem(namespace="user_facts", count=3, last_updated=now),
        ]
        mock_service.get_stats.return_value = items

        result = await memory_stats(user_id="u1", ctx=mock_ctx)

        assert len(result) == 2
        assert result[0]["namespace"] == "default"
        assert result[0]["count"] == 5
        assert result[1]["namespace"] == "user_facts"
        assert result[1]["count"] == 3

    @pytest.mark.asyncio
    async def test_stats_nonexistent_user(self, mock_ctx, mock_service):
        """Для несуществующего user_id → пустой список."""
        mock_service.get_stats.return_value = []

        result = await memory_stats(user_id="ghost", ctx=mock_ctx)

        assert result == []
        mock_service.get_stats.assert_awaited_once_with("ghost")


# ---------------------------------------------------------------------------
# memory_find_similar
# ---------------------------------------------------------------------------


class TestMemoryFindSimilar:
    @pytest.mark.asyncio
    async def test_find_similar_returns_results(self, mock_ctx, mock_service):
        """Ищет и возвращает результаты (как memory_search)."""
        results = [
            SearchResult(id="1", content="similar", metadata={}, score=0.95),
        ]
        mock_service.search.return_value = results

        result = await memory_find_similar(
            content="test query",
            user_id="u1",
            ctx=mock_ctx,
        )

        assert len(result) == 1
        assert result[0]["id"] == "1"
        assert result[0]["score"] == 0.95
        mock_service.search.assert_awaited_once_with(
            query="test query",
            user_id="u1",
            limit=10,
            threshold=0.7,
            namespace=None,
        )

    @pytest.mark.asyncio
    async def test_find_similar_invalid_namespace(self, mock_ctx):
        """С невалидным namespace → ошибка ValueError."""
        with pytest.raises(ValueError, match="Invalid namespace"):
            await memory_find_similar(
                content="test",
                user_id="u1",
                namespace="bad_ns",
                ctx=mock_ctx,
            )
