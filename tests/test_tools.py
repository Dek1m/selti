"""Tests for memory_tools.py MCP tools.

All tools now delegate to Celery via celery_call().
Tests mock celery_call to avoid needing a real Celery broker.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_server.models import MemoryRecord, MemoryStatsItem, SearchResult
from memory_server.tools.memory_tools import (
    memory_find_similar,
    memory_ingest_batch,
    memory_stats,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_celery_call():
    """Mock celery_call to return controlled results."""
    with patch(
        "memory_server.tools.memory_tools.celery_call", new_callable=AsyncMock
    ) as m:
        yield m


@pytest.fixture
def mock_metrics():
    """Mock all metrics to avoid prometheus errors."""
    with patch("memory_server.tools.memory_tools.SEARCH_RESULTS") as search, \
         patch("memory_server.tools.memory_tools.MEMORY_COUNT") as count, \
         patch("memory_server.tools.memory_tools.DEDUP_SKIPPED_TOTAL") as skipped, \
         patch("memory_server.tools.memory_tools.DEDUP_INSERTED_TOTAL") as inserted:
        yield {
            "search": search,
            "count": count,
            "skipped": skipped,
            "inserted": inserted,
        }


# ---------------------------------------------------------------------------
# memory_ingest_batch
# ---------------------------------------------------------------------------


class TestMemoryIngestBatch:
    @pytest.mark.asyncio
    async def test_batch_empty_list(self, mock_celery_call, mock_metrics):
        """Пустой список entries → пустой результат."""
        mock_celery_call.return_value = {
            "results": [],
            "summary": {"insert": 0, "skip": 0, "update": 0},
        }

        result = await memory_ingest_batch(entries=[], user_id="u1")

        assert result == {
            "results": [],
            "summary": {"insert": 0, "skip": 0, "update": 0},
        }
        mock_celery_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_batch_single_entry(self, mock_celery_call, mock_metrics):
        """Один entry → insert через batch."""
        mock_celery_call.return_value = {
            "results": [
                {"id": "mem-1", "action": "insert", "namespace": "default"}
            ],
            "summary": {"insert": 1, "skip": 0, "update": 0},
        }

        result = await memory_ingest_batch(
            entries=[{"content": "test"}],
            user_id="u1",
        )

        assert len(result["results"]) == 1
        assert result["results"][0] == {
            "id": "mem-1",
            "action": "insert",
            "namespace": "default",
        }
        assert result["summary"] == {"insert": 1, "skip": 0, "update": 0}

    @pytest.mark.asyncio
    async def test_batch_multiple_entries(self, mock_celery_call, mock_metrics):
        """Несколько entries → batch insert."""
        mock_celery_call.return_value = {
            "results": [
                {"id": "mem-1", "action": "insert", "namespace": "default"},
                {"id": "mem-2", "action": "insert", "namespace": "user_facts"},
            ],
            "summary": {"insert": 2, "skip": 0, "update": 0},
        }

        result = await memory_ingest_batch(
            entries=[
                {"content": "first", "namespace": "default"},
                {"content": "second", "namespace": "user_facts"},
            ],
            user_id="u1",
        )

        assert result["summary"] == {"insert": 2, "skip": 0, "update": 0}
        assert len(result["results"]) == 2


# ---------------------------------------------------------------------------
# memory_stats
# ---------------------------------------------------------------------------


class TestMemoryStats:
    @pytest.mark.asyncio
    async def test_stats_empty(self, mock_celery_call, mock_metrics):
        """Пустая статистика → пустой список."""
        mock_celery_call.return_value = []

        result = await memory_stats(user_id="u1")

        assert result == []
        mock_celery_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stats_with_data(self, mock_celery_call, mock_metrics):
        """С записями → статистика группируется по namespace."""
        mock_celery_call.return_value = [
            {"namespace": "default", "count": 5, "last_updated": None},
            {"namespace": "user_facts", "count": 3, "last_updated": None},
        ]

        result = await memory_stats(user_id="u1")

        assert len(result) == 2
        assert result[0]["namespace"] == "default"
        assert result[0]["count"] == 5
        assert result[1]["namespace"] == "user_facts"
        assert result[1]["count"] == 3

    @pytest.mark.asyncio
    async def test_stats_nonexistent_user(self, mock_celery_call, mock_metrics):
        """Для несуществующего user_id → пустой список."""
        mock_celery_call.return_value = []

        result = await memory_stats(user_id="ghost")

        assert result == []
        mock_celery_call.assert_awaited_once()


# ---------------------------------------------------------------------------
# memory_find_similar
# ---------------------------------------------------------------------------


class TestMemoryFindSimilar:
    @pytest.mark.asyncio
    async def test_find_similar_returns_results(self, mock_celery_call, mock_metrics):
        """Ищет и возвращает результаты (как memory_search)."""
        mock_celery_call.return_value = [
            {"id": "1", "content": "similar", "metadata": {}, "score": 0.95}
        ]

        result = await memory_find_similar(
            content="test query",
            user_id="u1",
        )

        assert len(result) == 1
        assert result[0]["id"] == "1"
        assert result[0]["score"] == 0.95
        mock_celery_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_find_similar_empty_content(self, mock_celery_call, mock_metrics):
        """Пустой контент → celery_call всё равно вызывается (валидация на уровне task)."""
        mock_celery_call.return_value = []

        result = await memory_find_similar(content="", user_id="u1")

        # Tool-level doesn't validate; Celery task will raise ValidationError
        # but with task_always_eager it will propagate
        # For this test we just check it was called
        mock_celery_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_find_similar_with_namespace(self, mock_celery_call, mock_metrics):
        """Фильтр по namespace передаётся в celery_call."""
        mock_celery_call.return_value = []

        await memory_find_similar(
            content="query",
            user_id="u1",
            namespace="code_knowledge",
            limit=5,
            threshold=0.8,
        )

        call_kwargs = mock_celery_call.call_args[1]
        assert call_kwargs["namespace"] == "code_knowledge"
        assert call_kwargs["limit"] == 5
        assert call_kwargs["threshold"] == 0.8
