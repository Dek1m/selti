from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from memory_server.exceptions import NotFoundError
from memory_server.memory.service import MemoryService
from memory_server.models import MemoryListResult, MemoryRecord, SearchResult


@pytest.fixture
def service(mock_repository, mock_embedding_provider):
    return MemoryService(
        repository=mock_repository,
        embedding_provider=mock_embedding_provider,
    )


class TestStore:
    @pytest.mark.asyncio
    async def test_store_generates_embedding_and_returns_record(self, service):
        now = datetime.now(timezone.utc)
        expected_record = MemoryRecord(
            id="new-id",
            user_id="u1",
            content="Hello world",
            metadata={"source": "test"},
            namespace="ns1",
            created_at=now,
            updated_at=now,
        )

        service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        service.repository.insert = AsyncMock(return_value="new-id")
        service.repository.get_by_id = AsyncMock(return_value=expected_record)

        result = await service.store(
            content="Hello world",
            user_id="u1",
            metadata={"source": "test"},
            namespace="ns1",
        )

        service.embedding.embed.assert_awaited_once_with("Hello world")
        service.repository.insert.assert_awaited_once_with(
            user_id="u1",
            content="Hello world",
            embedding=[0.1, 0.2, 0.3],
            metadata={"source": "test"},
            namespace="ns1",
        )
        service.repository.get_by_id.assert_awaited_once_with("new-id")
        assert result == expected_record

    @pytest.mark.asyncio
    async def test_store_uses_default_metadata_and_namespace(self, service):
        now = datetime.now(timezone.utc)
        service.embedding.embed = AsyncMock(return_value=[0.0, 0.0, 0.0])
        service.repository.insert = AsyncMock(return_value="id-1")
        service.repository.get_by_id = AsyncMock(
            return_value=MemoryRecord(
                id="id-1",
                user_id="u1",
                content="x",
                created_at=now,
                updated_at=now,
            )
        )

        await service.store(content="x", user_id="u1")

        service.repository.insert.assert_awaited_once_with(
            user_id="u1",
            content="x",
            embedding=[0.0, 0.0, 0.0],
            metadata={},
            namespace="default",
        )

    @pytest.mark.asyncio
    async def test_store_raises_if_get_returns_none(self, service):
        service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        service.repository.insert = AsyncMock(return_value="ghost-id")
        service.repository.get_by_id = AsyncMock(return_value=None)

        with pytest.raises(RuntimeError, match="ghost-id"):
            await service.store(content="x", user_id="u1")


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_generates_query_embedding(self, service):
        results = [
            SearchResult(id="1", content="match", metadata={}, score=0.95),
        ]
        service.embedding.embed = AsyncMock(return_value=[0.5, 0.6, 0.7])
        service.repository.search = AsyncMock(return_value=results)

        result = await service.search(
            query="find this",
            user_id="u1",
            limit=5,
            threshold=0.8,
            namespace="ns",
        )

        service.embedding.embed.assert_awaited_once_with("find this")
        service.repository.search.assert_awaited_once_with(
            query_embedding=[0.5, 0.6, 0.7],
            user_id="u1",
            limit=5,
            threshold=0.8,
            namespace="ns",
        )
        assert result == results


class TestGet:
    @pytest.mark.asyncio
    async def test_get_returns_record(self, service):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="mem-1",
            user_id="u1",
            content="data",
            created_at=now,
            updated_at=now,
        )
        service.repository.get_by_id = AsyncMock(return_value=record)

        result = await service.get("mem-1")
        assert result == record
        service.repository.get_by_id.assert_awaited_once_with("mem-1")

    @pytest.mark.asyncio
    async def test_get_raises_not_found(self, service):
        service.repository.get_by_id = AsyncMock(return_value=None)

        with pytest.raises(NotFoundError) as exc_info:
            await service.get("missing-id")
        assert exc_info.value.id == "missing-id"


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_with_content_regenerates_embedding(self, service):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="mem-1",
            user_id="u1",
            content="updated",
            created_at=now,
            updated_at=now,
        )
        service.embedding.embed = AsyncMock(return_value=[0.9, 0.8, 0.7])
        service.repository.update = AsyncMock(return_value=record)

        result = await service.update(memory_id="mem-1", content="updated", metadata={"k": "v"})

        service.embedding.embed.assert_awaited_once_with("updated")
        service.repository.update.assert_awaited_once_with(
            memory_id="mem-1",
            content="updated",
            embedding=[0.9, 0.8, 0.7],
            metadata={"k": "v"},
        )
        assert result == record

    @pytest.mark.asyncio
    async def test_update_without_content_skips_embedding(self, service):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="mem-1",
            user_id="u1",
            content="old",
            created_at=now,
            updated_at=now,
        )
        service.repository.update = AsyncMock(return_value=record)

        result = await service.update(memory_id="mem-1", metadata={"k": "v"})

        service.embedding.embed.assert_not_awaited()
        service.repository.update.assert_awaited_once_with(
            memory_id="mem-1",
            content=None,
            embedding=None,
            metadata={"k": "v"},
        )
        assert result == record

    @pytest.mark.asyncio
    async def test_update_not_found_raises(self, service):
        service.repository.update = AsyncMock(return_value=None)

        with pytest.raises(NotFoundError) as exc_info:
            await service.update(memory_id="missing", content="x")
        assert exc_info.value.id == "missing"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_delegates(self, service):
        service.repository.delete = AsyncMock(return_value=True)

        result = await service.delete("mem-1")
        assert result is True
        service.repository.delete.assert_awaited_once_with("mem-1")

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self, service):
        service.repository.delete = AsyncMock(return_value=False)

        result = await service.delete("mem-1")
        assert result is False


class TestList:
    @pytest.mark.asyncio
    async def test_list_delegates(self, service):
        now = datetime.now(timezone.utc)
        items = [
            MemoryRecord(id="1", user_id="u1", content="a", created_at=now, updated_at=now),
        ]
        expected = MemoryListResult(items=items, total=1)
        service.repository.list = AsyncMock(return_value=expected)

        result = await service.list(user_id="u1", namespace="ns", limit=10, offset=5)

        service.repository.list.assert_awaited_once_with(
            user_id="u1",
            namespace="ns",
            limit=10,
            offset=5,
        )
        assert result == expected

    @pytest.mark.asyncio
    async def test_list_defaults(self, service):
        service.repository.list = AsyncMock(return_value=MemoryListResult(items=[], total=0))

        await service.list()

        service.repository.list.assert_awaited_once_with(
            user_id=None,
            namespace=None,
            limit=50,
            offset=0,
        )


class TestForget:
    @pytest.mark.asyncio
    async def test_forget_delegates(self, service):
        service.repository.forget = AsyncMock(return_value=7)

        result = await service.forget(user_id="u1", namespace="ns")

        service.repository.forget.assert_awaited_once_with(user_id="u1", namespace="ns")
        assert result == 7
