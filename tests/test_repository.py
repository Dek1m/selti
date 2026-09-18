from unittest.mock import AsyncMock

import pytest

from memory_server.db import queries as q
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.models import MemoryListResult, MemoryRecord, SearchResult
from tests.conftest import memory_row

# Фиксированный namespace_id из mock_ns_resolver (conftest)
NS_ID = "00000000-0000-0000-0000-0000000000aa"


@pytest.fixture
def repo(mock_pool, mock_ns_resolver):
    pg = PostgreSQLRepository(pool=mock_pool)
    return MemoryRepository(pg=pg, ns_repo=mock_ns_resolver)


@pytest.fixture
def conn(repo):
    """Shortcut to the mock connection inside the pool."""
    return repo.pg.pool.acquire.return_value.__aenter__.return_value


class TestInsert:
    @pytest.mark.asyncio
    async def test_insert_returns_id(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value={"id": "new-uuid-123"})

        result = await repo.insert(
            user_id="u1",
            content="Hello",
            embedding=[0.1, 0.2, 0.3],
            metadata={"source": "test"},
            namespace_id="ns1-uuid-456",
            project_id="11111111-1111-1111-1111-111111111111",
        )

        assert result == "new-uuid-123"
        conn.fetchrow.assert_awaited_once_with(
            q.INSERT_MEMORY,
            "u1",
            "Hello",
            {"source": "test"},
            "ns1-uuid-456",
            None,
            3,
            "11111111-1111-1111-1111-111111111111",
            None,
            False,
            None,
        )


class TestGetById:
    @pytest.mark.asyncio
    async def test_get_by_id_found(self, repo, conn):
        conn.fetchrow = AsyncMock(
            return_value=memory_row(
                id="550e8400-e29b-41d4-a716-446655440000",
                metadata={"k": "v"},
                content="data",
            )
        )

        record = await repo.get_by_id("550e8400-e29b-41d4-a716-446655440000")

        assert isinstance(record, MemoryRecord)
        assert record.id == "550e8400-e29b-41d4-a716-446655440000"
        assert record.content == "data"
        assert record.status == "asserted"
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
        conn.fetchrow = AsyncMock(return_value=memory_row(metadata=None))

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
                    "namespace": "ns",
                    "importance": 4,
                    "score": 0.95,
                    "project_id": None,
                    "status": "asserted",
                },
                {
                    "id": "2",
                    "content": "result b",
                    "metadata": {},
                    "namespace": "ns",
                    "importance": 2,
                    "score": 0.87,
                    "project_id": None,
                    "status": "asserted",
                },
            ]
        )

        results = await repo.search(
            query_embedding=[0.1, 0.2, 0.3],
            user_id="u1",
            limit=10,
            threshold=0.7,
            namespace="ns",
            query_text="search query",
        )

        assert len(results) == 2
        assert isinstance(results[0], SearchResult)
        assert results[0].id == "1"
        assert results[0].score == 0.95
        conn.fetch.assert_awaited_once_with(
            q.SEARCH_MEMORIES,
            "search query",
            "u1",
            NS_ID,
            None,
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
            query_text="search query",
        )

        conn.fetch.assert_awaited_once_with(
            q.SEARCH_MEMORIES,
            "search query",
            "u1",
            None,
            None,
            5,
        )

    @pytest.mark.asyncio
    async def test_search_no_query_text_returns_empty(self, repo):
        """Without query_text and without qdrant, search returns empty."""
        results = await repo.search(
            query_embedding=[0.1, 0.2, 0.3],
            user_id="u1",
            limit=10,
            threshold=0.7,
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_search_unknown_namespace_returns_empty(self, repo, mock_ns_resolver):
        """Незарегистрированный uid → пустой результат без SQL."""
        async def missing(uid):
            return None

        mock_ns_resolver.get_by_uid = missing
        results = await repo.search(
            query_embedding=[0.1, 0.2, 0.3],
            query_text="x",
            namespace="ghost",
        )
        assert results == []


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_full(self, repo, conn):
        conn.fetchrow = AsyncMock(
            return_value=memory_row(id="mem-1", content="new content", metadata={"k": "v"})
        )

        record = await repo.update(
            memory_id="mem-1",
            content="new content",
            metadata={"k": "v"},
        )

        assert isinstance(record, MemoryRecord)
        assert record.content == "new content"
        conn.fetchrow.assert_awaited_once_with(
            q.UPDATE_MEMORY,
            "mem-1",
            "new content",
            {"k": "v"},
            None,
            None,
            None,
            None,
            None,
        )

    @pytest.mark.asyncio
    async def test_update_with_supersedes_closes_old(self, repo, conn):
        """supersedes → второй запрос SUPERSEDE_MEMORY в той же транзакции."""
        conn.fetchrow = AsyncMock(return_value=memory_row(id="mem-new"))

        record = await repo.update(memory_id="mem-new", supersedes="mem-old")

        assert record is not None
        assert conn.fetchrow.await_count == 2
        conn.fetchrow.assert_any_await(q.SUPERSEDE_MEMORY, "mem-old", "mem-new")

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
        conn.fetch = AsyncMock(
            return_value=[memory_row(id="1", user_id="u1", content="a", total_count=5)]
        )

        result = await repo.list(user_id="u1", namespace="ns", limit=10, offset=0)

        assert isinstance(result, MemoryListResult)
        assert len(result.items) == 1
        assert result.total == 5
        conn.fetch.assert_awaited_once_with(q.LIST_MEMORIES, "u1", NS_ID, None, 10, 0)

    @pytest.mark.asyncio
    async def test_list_no_filters(self, repo, conn):
        conn.fetch = AsyncMock(return_value=[])

        result = await repo.list()
        assert result.total == 0
        conn.fetch.assert_awaited_once_with(q.LIST_MEMORIES, None, None, None, 50, 0)


class TestForget:
    @pytest.mark.asyncio
    async def test_forget_returns_count(self, repo, conn):
        conn.fetchval = AsyncMock(return_value=3)

        deleted = await repo.forget(user_id="u1", namespace="ns")

        assert deleted == 3
        conn.fetchval.assert_awaited_once_with(q.FORGET_MEMORIES, "u1", NS_ID)

    @pytest.mark.asyncio
    async def test_forget_without_namespace(self, repo, conn):
        conn.fetchval = AsyncMock(return_value=0)

        deleted = await repo.forget(user_id="u1", namespace=None)
        assert deleted == 0
        conn.fetchval.assert_awaited_once_with(q.FORGET_MEMORIES, "u1", None)
