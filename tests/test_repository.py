from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from memory_server.db import queries as q
from memory_server.memory.repository import MemoryRepository
from memory_server.models import MemoryListResult, MemoryRecord, SearchResult


@pytest.fixture
def repo(mock_pool):
    return MemoryRepository(pool=mock_pool)


@pytest.fixture
def conn(repo):
    """Shortcut to the mock connection inside the pool."""
    return repo.pool.acquire.return_value.__aenter__.return_value


class TestInsert:
    @pytest.mark.asyncio
    async def test_insert_returns_id(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value={"id": "new-uuid-123"})

        result = await repo.insert(
            user_id="u1",
            content="Hello",
            embedding=[0.1, 0.2, 0.3],
            metadata={"source": "test"},
            namespace="ns1",
        )

        assert result == "new-uuid-123"
        conn.fetchrow.assert_awaited_once_with(
            q.INSERT_MEMORY,
            "u1",
            "Hello",
            [0.1, 0.2, 0.3],
            {"source": "test"},
            "ns1",
            None,
        )


class TestGetById:
    @pytest.mark.asyncio
    async def test_get_by_id_found(self, repo, conn):
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "user_id": "u1",
                "content": "data",
                "metadata": {"k": "v"},
                "namespace": "default",
                "created_at": now,
                "updated_at": now,
                "content_hash": None,
            }
        )

        record = await repo.get_by_id("550e8400-e29b-41d4-a716-446655440000")

        assert isinstance(record, MemoryRecord)
        assert record.id == "550e8400-e29b-41d4-a716-446655440000"
        assert record.content == "data"
        conn.fetchrow.assert_awaited_once_with(
            q.SELECT_MEMORY_BY_ID,
            "550e8400-e29b-41d4-a716-446655440000",
        )

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value=None)

        record = await repo.get_by_id("non-existent")
        assert record is None

    @pytest.mark.asyncio
    async def test_get_by_id_null_metadata(self, repo, conn):
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "id-1",
                "user_id": "u1",
                "content": "c",
                "metadata": None,
                "namespace": "default",
                "created_at": now,
                "updated_at": now,
                "content_hash": None,
            }
        )

        record = await repo.get_by_id("id-1")
        assert record is not None
        assert record.metadata == {}


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self, repo, conn):
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": "1",
                    "content": "result a",
                    "metadata": {"score": 0.95},
                    "score": 0.95,
                },
                {
                    "id": "2",
                    "content": "result b",
                    "metadata": {},
                    "score": 0.87,
                },
            ]
        )

        results = await repo.search(
            query_embedding=[0.1, 0.2, 0.3],
            user_id="u1",
            limit=10,
            threshold=0.7,
            namespace="ns",
        )

        assert len(results) == 2
        assert isinstance(results[0], SearchResult)
        assert results[0].id == "1"
        assert results[0].score == 0.95
        conn.fetch.assert_awaited_once_with(
            q.SEARCH_MEMORIES,
            [0.1, 0.2, 0.3],
            "u1",
            "ns",
            0.7,
            10,
        )

    @pytest.mark.asyncio
    async def test_search_without_namespace(self, repo, conn):
        conn.fetch = AsyncMock(return_value=[])

        await repo.search(
            query_embedding=[0.1, 0.2, 0.3],
            user_id="u1",
            limit=5,
            threshold=0.5,
            namespace=None,
        )

        conn.fetch.assert_awaited_once_with(
            q.SEARCH_MEMORIES,
            [0.1, 0.2, 0.3],
            "u1",
            None,
            0.5,
            5,
        )


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_full(self, repo, conn):
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "mem-1",
                "user_id": "u1",
                "content": "new content",
                "metadata": {"k": "v"},
                "namespace": "default",
                "created_at": now,
                "updated_at": now,
                "content_hash": None,
            }
        )

        record = await repo.update(
            memory_id="mem-1",
            content="new content",
            embedding=[0.5, 0.6, 0.7],
            metadata={"k": "v"},
        )

        assert isinstance(record, MemoryRecord)
        assert record.content == "new content"
        conn.fetchrow.assert_awaited_once_with(
            q.UPDATE_MEMORY,
            "mem-1",
            "new content",
            [0.5, 0.6, 0.7],
            {"k": "v"},
        )

    @pytest.mark.asyncio
    async def test_update_partial(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value=None)

        record = await repo.update(memory_id="mem-1", content=None, embedding=None, metadata=None)
        assert record is None

    @pytest.mark.asyncio
    async def test_update_not_found(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value=None)

        record = await repo.update(memory_id="missing", content="x")
        assert record is None


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_found(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value={"id": "mem-1"})

        deleted = await repo.delete("mem-1")
        assert deleted is True
        conn.fetchrow.assert_awaited_once_with(q.DELETE_MEMORY, "mem-1")

    @pytest.mark.asyncio
    async def test_delete_not_found(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value=None)

        deleted = await repo.delete("mem-1")
        assert deleted is False


class TestList:
    @pytest.mark.asyncio
    async def test_list_returns_paginated_result(self, repo, conn):
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": "1",
                    "user_id": "u1",
                    "content": "a",
                    "metadata": {},
                    "namespace": "default",
                    "created_at": now,
                    "updated_at": now,
                    "content_hash": None,
                },
            ]
        )
        conn.fetchrow = AsyncMock(return_value=[5])  # total count

        result = await repo.list(user_id="u1", namespace="ns", limit=10, offset=0)

        assert isinstance(result, MemoryListResult)
        assert len(result.items) == 1
        assert result.total == 5
        conn.fetch.assert_awaited_once_with(q.LIST_MEMORIES, "u1", "ns", 10, 0)
        conn.fetchrow.assert_awaited_with(q.COUNT_MEMORIES, "u1", "ns")

    @pytest.mark.asyncio
    async def test_list_no_filters(self, repo, conn):
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchrow = AsyncMock(return_value=[0])

        result = await repo.list()
        assert result.total == 0
        conn.fetch.assert_awaited_once_with(q.LIST_MEMORIES, None, None, 50, 0)
        conn.fetchrow.assert_awaited_with(q.COUNT_MEMORIES, None, None)


class TestForget:
    @pytest.mark.asyncio
    async def test_forget_returns_count(self, repo, conn):
        conn.execute = AsyncMock(return_value="DELETE 3")

        deleted = await repo.forget(user_id="u1", namespace="ns")

        assert deleted == 3
        conn.execute.assert_awaited_once_with(q.FORGET_MEMORIES, "u1", "ns")

    @pytest.mark.asyncio
    async def test_forget_without_namespace(self, repo, conn):
        conn.execute = AsyncMock(return_value="DELETE 0")

        deleted = await repo.forget(user_id="u1", namespace=None)
        assert deleted == 0
        conn.execute.assert_awaited_once_with(q.FORGET_MEMORIES, "u1", None)
