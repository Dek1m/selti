"""Tests for relations and archive functionality."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from memory_server.memory.repository import MemoryRepository
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.models import (
    GraphStats,
    Relation,
    RelationCreate,
    RelationListResult,
    TraverseResult,
)


@pytest.fixture
def repo(mock_pool):
    pg = PostgreSQLRepository(pool=mock_pool)
    return MemoryRepository(pg=pg)


@pytest.fixture
def conn(repo):
    """Shortcut to the mock connection inside the pool."""
    return repo.pg.pool.acquire.return_value.__aenter__.return_value


class TestAddRelation:
    @pytest.mark.asyncio
    async def test_add_relation_returns_id(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value={"id": "rel-uuid-123"})

        result = await repo.add_relation(
            source_id="src-uuid",
            target_id="tgt-uuid",
            link_type="depends_on",
            description="test relation",
        )

        assert result == "rel-uuid-123"

    @pytest.mark.asyncio
    async def test_add_relation_minimal(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value={"id": "rel-uuid-456"})

        result = await repo.add_relation(
            source_id="src-uuid",
            target_id="tgt-uuid",
            link_type="related_to",
        )

        assert result == "rel-uuid-456"


class TestGetRelations:
    @pytest.mark.asyncio
    async def test_get_relations_by_source(self, repo, conn):
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": "rel-1",
                    "source_id": "src-1",
                    "target_id": "tgt-1",
                    "target_name": None,
                    "link_type": "depends_on",
                    "description": None,
                    "weight": 1.0,
                    "metadata": {},
                    "created_at": now,
                },
                {
                    "id": "rel-2",
                    "source_id": "src-1",
                    "target_id": "tgt-2",
                    "target_name": None,
                    "link_type": "related_to",
                    "description": "related",
                    "weight": 1.0,
                    "metadata": {},
                    "created_at": now,
                },
            ]
        )

        result = await repo.get_relations_by_source("src-1")

        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], Relation)
        assert result[0].source_id == "src-1"
        assert result[0].link_type == "depends_on"

    @pytest.mark.asyncio
    async def test_get_relations_by_target(self, repo, conn):
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": "rel-3",
                    "source_id": "src-2",
                    "target_id": "tgt-1",
                    "target_name": None,
                    "link_type": "implements",
                    "description": None,
                    "weight": 1.0,
                    "metadata": {},
                    "created_at": now,
                }
            ]
        )

        result = await repo.get_relations_by_target("tgt-1")

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].target_id == "tgt-1"


class TestDeleteRelation:
    @pytest.mark.asyncio
    async def test_delete_relation(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value={"id": "rel-uuid"})

        result = await repo.delete_relation("src-uuid", "tgt-uuid", "depends_on")

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_relation_not_found(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value=None)

        result = await repo.delete_relation("non-existent", "tgt-uuid", "depends_on")

        assert result is False


class TestTraverse:
    @pytest.mark.asyncio
    async def test_traverse_returns_results(self, repo, conn):
        conn.fetchrow = AsyncMock(
            return_value={
                "nodes": [{"id": "tgt-1", "content": "test", "namespace": "ns", "importance": 3, "depth": 1}],
                "edges": [],
            }
        )

        result = await repo.traverse(
            start_id="src-1",
            depth=3,
        )

        assert isinstance(result, dict)
        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["id"] == "tgt-1"
        assert result["nodes"][0]["depth"] == 1


class TestGetGraphStats:
    @pytest.mark.asyncio
    async def test_get_graph_stats(self, repo, conn):
        conn.fetchrow = AsyncMock(
            return_value={
                "p_total_granules": 100,
                "p_total_relations": 50,
                "p_linked_granules": 70,
                "p_orphans": 30,
                "p_by_namespace": {
                    "code_knowledge": {"total": 60, "linked": 40, "orphans": 20},
                    "user_facts": {"total": 40, "linked": 30, "orphans": 10},
                },
                "p_by_link_type": {
                    "depends_on": 30,
                    "related_to": 20,
                },
            }
        )

        result = await repo.get_graph_stats()

        assert isinstance(result, GraphStats)
        assert result.total_granules == 100
        assert result.total_relations == 50
        assert result.linked_granules == 70
        assert result.orphans == 30


class TestArchive:
    @pytest.mark.asyncio
    async def test_archive_success(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value={"id": "mem-uuid"})

        result = await repo.archive("mem-uuid")

        assert result is True

    @pytest.mark.asyncio
    async def test_archive_not_found(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value=None)

        result = await repo.archive("non-existent")

        assert result is False


class TestBatchInsert:
    @pytest.mark.asyncio
    async def test_batch_insert(self, repo, conn):
        conn.fetch = AsyncMock(return_value=[{"id": "id1"}, {"id": "id2"}])

        result = await repo.insert_batch(
            user_ids=["u1", "u1"],
            contents=["text1", "text2"],
            embeddings=[[0.1, 0.2], [0.3, 0.4]],
            metadatas=[{"k": "v"}, {}],
            namespaces=["ns1", "ns2"],
            namespace_ids=["ns1-uuid", "ns2-uuid"],
            content_hashes=[None, None],
        )

        assert result == ["id1", "id2"]
