from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.memory.dedup import DedupAction
from memory_server.models import MemoryRecord, MemoryStatsItem, SearchResult
from memory_server.tools.memory_tools import (
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
    service.dedup = MagicMock()
    service.dedup.check = AsyncMock()
    service.config = MagicMock()
    service.config.dedup_enabled = False
    service.ns_repo = MagicMock()
    service.ns_repo.get_or_create = AsyncMock()
    service.embedding = MagicMock()
    service.embedding.embed_many = AsyncMock()
    service.repository = MagicMock()
    service.repository.insert_batch = AsyncMock()
    return service


@pytest.fixture
def mock_ctx(mock_service):
    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = {"service": mock_service}
    return ctx


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
        """Один entry → insert через batch."""
        mock_service.config.dedup_enabled = False
        mock_service.repository.insert_batch = AsyncMock(return_value=["mem-1"])
        mock_service.ns_repo.get_or_create = AsyncMock(return_value=MagicMock(id="ns-1"))
        mock_service.embedding.embed_many = AsyncMock(return_value=[[0.1, 0.2]])

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

    @pytest.mark.asyncio
    async def test_batch_multiple_entries(self, mock_ctx, mock_service):
        """Несколько entries → batch insert."""
        mock_service.config.dedup_enabled = False
        mock_service.repository.insert_batch = AsyncMock(return_value=["mem-1", "mem-2"])
        mock_service.ns_repo.get_or_create = AsyncMock(return_value=MagicMock(id="ns-1"))
        mock_service.embedding.embed_many = AsyncMock(return_value=[[0.1], [0.2]])

        result = await memory_ingest_batch(
            entries=[
                {"content": "first", "namespace": "default"},
                {"content": "second", "namespace": "user_facts"},
            ],
            user_id="u1",
            ctx=mock_ctx,
        )

        assert result["summary"] == {"insert": 2, "skip": 0, "update": 0}
        assert len(result["results"]) == 2


# ---------------------------------------------------------------------------
# memory_stats
# ---------------------------------------------------------------------------


class TestMemoryStats:
    @pytest.mark.asyncio
    async def test_stats_empty(self, mock_ctx, mock_service):
        """Пустая статистика → пустой список."""
        mock_service.repository.get_stats = AsyncMock(return_value=[])

        result = await memory_stats(user_id="u1", ctx=mock_ctx)

        assert result == []
        mock_service.repository.get_stats.assert_awaited_once_with("u1")

    @pytest.mark.asyncio
    async def test_stats_with_data(self, mock_ctx, mock_service):
        """С записями → статистика группируется по namespace."""
        now = datetime.now(timezone.utc)
        items = [
            MemoryStatsItem(namespace="default", count=5, last_updated=now),
            MemoryStatsItem(namespace="user_facts", count=3, last_updated=now),
        ]
        mock_service.repository.get_stats = AsyncMock(return_value=items)

        result = await memory_stats(user_id="u1", ctx=mock_ctx)

        assert len(result) == 2
        assert result[0]["namespace"] == "default"
        assert result[0]["count"] == 5
        assert result[1]["namespace"] == "user_facts"
        assert result[1]["count"] == 3

    @pytest.mark.asyncio
    async def test_stats_nonexistent_user(self, mock_ctx, mock_service):
        """Для несуществующего user_id → пустой список."""
        mock_service.repository.get_stats = AsyncMock(return_value=[])

        result = await memory_stats(user_id="ghost", ctx=mock_ctx)

        assert result == []
        mock_service.repository.get_stats.assert_awaited_once_with("ghost")


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
        mock_service.embedding.embed = AsyncMock(return_value=[0.1, 0.2])
        mock_service.repository.search = AsyncMock(return_value=results)

        result = await memory_find_similar(
            content="test query",
            user_id="u1",
            ctx=mock_ctx,
        )

        assert len(result) == 1
        assert result[0]["id"] == "1"
        assert result[0]["score"] == 0.95
        mock_service.embedding.embed.assert_awaited_once_with("test query")
        mock_service.repository.search.assert_awaited_once_with(
            query_embedding=[0.1, 0.2],
            user_id="u1",
            limit=10,
            threshold=0.7,
            namespace=None,
        )
