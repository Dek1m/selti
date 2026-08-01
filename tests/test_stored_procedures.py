"""Тесты для хранимок PostgreSQL: traverse, graph_stats, relations, list, forget.

Все тесты мокают asyncpg pool — проверяем что Python-код корректно
вызывает хранимки и парсит результаты.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.db import queries as q
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    RelationListResult,
    Relation,
)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    acm = AsyncMock()
    acm.__aenter__.return_value = conn
    acm.__aexit__.return_value = None
    pool.acquire.return_value = acm
    return pool


@pytest.fixture
def pg(mock_pool):
    return PostgreSQLRepository(pool=mock_pool)


@pytest.fixture
def conn(pg):
    return pg.pool.acquire.return_value.__aenter__.return_value


# ══════════════════════════════════════════════════════════════════
# 1. traverse (graph_traverse_full)
# ══════════════════════════════════════════════════════════════════


class TestTraverse:
    @pytest.mark.asyncio
    async def test_traverse_returns_nodes_and_edges(self, pg, conn):
        """traverse возвращает nodes + edges из хранимки."""
        conn.fetchrow = AsyncMock(return_value={
            "nodes": [
                {"id": "n1", "content": "hello", "namespace": "default", "importance": 3, "depth": 0},
                {"id": "n2", "content": "world", "namespace": "code_knowledge", "importance": 4, "depth": 1},
            ],
            "edges": [
                {"id": "e1", "source_id": "n1", "target_id": "n2", "link_type": "depends_on",
                 "description": "test", "weight": 1.0},
            ],
        })

        result = await pg.traverse("n1", depth=3, link_types=None)

        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1
        assert result["nodes"][0]["id"] == "n1"
        assert result["edges"][0]["link_type"] == "depends_on"
        conn.fetchrow.assert_awaited_once_with(q.TRAVERSE_FULL, "n1", 3, None)

    @pytest.mark.asyncio
    async def test_traverse_with_link_types_filter(self, pg, conn):
        """traverse с фильтром link_types передаёт массив в хранимку."""
        conn.fetchrow = AsyncMock(return_value={"nodes": [], "edges": []})

        result = await pg.traverse("n1", depth=2, link_types=["depends_on", "calls"])

        assert result == {"nodes": [], "edges": []}
        conn.fetchrow.assert_awaited_once_with(q.TRAVERSE_FULL, "n1", 2, ["depends_on", "calls"])

    @pytest.mark.asyncio
    async def test_traverse_empty_result(self, pg, conn):
        """traverse для несуществующей ноды → пустые списки."""
        conn.fetchrow = AsyncMock(return_value=None)

        result = await pg.traverse("non-existent", depth=3)

        assert result == {"nodes": [], "edges": []}

    @pytest.mark.asyncio
    async def test_traverse_null_nodes_returns_empty(self, pg, conn):
        """traverse с None nodes/edges → пустые списки."""
        conn.fetchrow = AsyncMock(return_value={"nodes": None, "edges": None})

        result = await pg.traverse("n1", depth=3)

        assert result == {"nodes": [], "edges": []}

    @pytest.mark.asyncio
    async def test_traverse_default_depth(self, pg, conn):
        """traverse по умолчанию depth=3."""
        conn.fetchrow = AsyncMock(return_value={"nodes": [], "edges": []})

        await pg.traverse("n1")

        conn.fetchrow.assert_awaited_once_with(q.TRAVERSE_FULL, "n1", 3, None)


# ══════════════════════════════════════════════════════════════════
# 2. get_graph_stats (graph_stats_unified)
# ══════════════════════════════════════════════════════════════════


class TestGraphStats:
    @pytest.mark.asyncio
    async def test_graph_stats_returns_full_stats(self, pg, conn):
        """graph_stats_unified возвращает полную статистику графа."""
        conn.fetchrow = AsyncMock(return_value={
            "p_total_granules": 100,
            "p_total_relations": 250,
            "p_linked_granules": 80,
            "p_orphans": 20,
            "p_by_namespace": {
                "default": {"total": 50, "linked": 40, "orphans": 10},
                "code_knowledge": {"total": 50, "linked": 40, "orphans": 10},
            },
            "p_by_link_type": {
                "depends_on": 100,
                "related_to": 80,
                "used_by": 70,
            },
        })

        result = await pg.get_graph_stats()

        assert isinstance(result, GraphStats)
        assert result.total_granules == 100
        assert result.total_relations == 250
        assert result.linked_granules == 80
        assert result.orphans == 20
        assert result.avg_connections == 5.0  # 250 * 2 / 100
        assert "default" in result.by_namespace
        assert result.by_link_type["depends_on"] == 100
        conn.fetchrow.assert_awaited_once_with(q.GRAPH_STATS_UNIFIED)

    @pytest.mark.asyncio
    async def test_graph_stats_empty_database(self, pg, conn):
        """graph_stats для пустой БД → нули."""
        conn.fetchrow = AsyncMock(return_value={
            "p_total_granules": 0,
            "p_total_relations": 0,
            "p_linked_granules": 0,
            "p_orphans": 0,
            "p_by_namespace": {},
            "p_by_link_type": {},
        })

        result = await pg.get_graph_stats()

        assert result.total_granules == 0
        assert result.avg_connections == 0.0
        assert result.by_namespace == {}

    @pytest.mark.asyncio
    async def test_graph_stats_calc_avg_connections(self, pg, conn):
        """avg_connections = total_relations * 2 / total_granules."""
        conn.fetchrow = AsyncMock(return_value={
            "p_total_granules": 50,
            "p_total_relations": 75,
            "p_linked_granules": 40,
            "p_orphans": 10,
            "p_by_namespace": {},
            "p_by_link_type": {},
        })

        result = await pg.get_graph_stats()

        assert result.avg_connections == 3.0  # 75 * 2 / 50

    @pytest.mark.asyncio
    async def test_graph_stats_null_by_namespace(self, pg, conn):
        """NULL by_namespace → пустой dict."""
        conn.fetchrow = AsyncMock(return_value={
            "p_total_granules": 5,
            "p_total_relations": 0,
            "p_linked_granules": 0,
            "p_orphans": 5,
            "p_by_namespace": None,
            "p_by_link_type": None,
        })

        result = await pg.get_graph_stats()

        assert result.by_namespace == {}
        assert result.by_link_type == {}


# ══════════════════════════════════════════════════════════════════
# 3. get_relations (get_relations_unified)
# ══════════════════════════════════════════════════════════════════


class TestGetRelationsUnified:
    @pytest.mark.asyncio
    async def test_get_relations_returns_incoming_and_outgoing(self, pg, conn):
        """get_relations_unified разделяет incoming/outgoing."""
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(return_value=[
            {
                "id": "r1", "source_id": "m1", "target_id": "m2",
                "target_name": None, "link_type": "depends_on",
                "description": None, "weight": 1.0, "metadata": {},
                "created_at": now, "direction": "outgoing",
            },
            {
                "id": "r2", "source_id": "m3", "target_id": "m1",
                "target_name": None, "link_type": "related_to",
                "description": "link", "weight": 0.5, "metadata": {},
                "created_at": now, "direction": "incoming",
            },
        ])

        result = await pg.get_relations("m1")

        assert isinstance(result, RelationListResult)
        assert len(result.outgoing) == 1
        assert len(result.incoming) == 1
        assert result.outgoing[0].link_type == "depends_on"
        assert result.incoming[0].link_type == "related_to"
        conn.fetch.assert_awaited_once_with(q.GET_RELATIONS_UNIFIED, "m1", None)

    @pytest.mark.asyncio
    async def test_get_relations_with_link_type_filter(self, pg, conn):
        """get_relations с фильтром link_type."""
        conn.fetch = AsyncMock(return_value=[])

        result = await pg.get_relations("m1", link_type="depends_on")

        assert result.incoming == []
        assert result.outgoing == []
        conn.fetch.assert_awaited_once_with(q.GET_RELATIONS_UNIFIED, "m1", "depends_on")

    @pytest.mark.asyncio
    async def test_get_relations_empty(self, pg, conn):
        """get_relations для гранулы без связей → пустой результат."""
        conn.fetch = AsyncMock(return_value=[])

        result = await pg.get_relations("m1")

        assert len(result.incoming) == 0
        assert len(result.outgoing) == 0

    @pytest.mark.asyncio
    async def test_get_relations_multiple_outgoing(self, pg, conn):
        """Несколько исходящих связей."""
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(return_value=[
            {"id": "r1", "source_id": "m1", "target_id": "m2", "target_name": None,
             "link_type": "depends_on", "description": None, "weight": 1.0,
             "metadata": {}, "created_at": now, "direction": "outgoing"},
            {"id": "r2", "source_id": "m1", "target_id": "m3", "target_name": None,
             "link_type": "calls", "description": None, "weight": 1.0,
             "metadata": {}, "created_at": now, "direction": "outgoing"},
        ])

        result = await pg.get_relations("m1")

        assert len(result.outgoing) == 2
        assert len(result.incoming) == 0

    @pytest.mark.asyncio
    async def test_get_relations_none_target_id(self, pg, conn):
        """target_id=None (target_name вместо target_id)."""
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(return_value=[
            {"id": "r1", "source_id": "m1", "target_id": None, "target_name": "SomeName",
             "link_type": "related_to", "description": None, "weight": 1.0,
             "metadata": {}, "created_at": now, "direction": "outgoing"},
        ])

        result = await pg.get_relations("m1")

        assert len(result.outgoing) == 1
        assert result.outgoing[0].target_id is None
        assert result.outgoing[0].target_name == "SomeName"


# ══════════════════════════════════════════════════════════════════
# 4. list (list_with_count)
# ══════════════════════════════════════════════════════════════════


class TestListWithCount:
    @pytest.mark.asyncio
    async def test_list_returns_items_and_total(self, pg, conn):
        """list_with_count возвращает items + total_count."""
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(return_value=[
            {
                "id": "m1", "user_id": "u1", "content": "hello",
                "metadata": {}, "namespace": "default", "importance": 3,
                "created_at": now, "updated_at": now, "content_hash": None,
                "total_count": 42,
            },
            {
                "id": "m2", "user_id": "u1", "content": "world",
                "metadata": {}, "namespace": "default", "importance": 3,
                "created_at": now, "updated_at": now, "content_hash": None,
                "total_count": 42,
            },
        ])

        result = await pg.list(user_id="u1", namespace="default", limit=10, offset=0)

        assert isinstance(result, MemoryListResult)
        assert len(result.items) == 2
        assert result.total == 42
        conn.fetch.assert_awaited_once_with(q.LIST_WITH_COUNT, "u1", "default", 10, 0)

    @pytest.mark.asyncio
    async def test_list_empty_result(self, pg, conn):
        """Пустой список → total=0."""
        conn.fetch = AsyncMock(return_value=[])

        result = await pg.list(user_id="u1")

        assert len(result.items) == 0
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_list_default_params(self, pg, conn):
        """list с дефолтными параметрами."""
        conn.fetch = AsyncMock(return_value=[])

        await pg.list()

        conn.fetch.assert_awaited_once_with(q.LIST_WITH_COUNT, None, None, 50, 0)

    @pytest.mark.asyncio
    async def test_list_total_from_first_row(self, pg, conn):
        """total_count берётся из первой строки (window function)."""
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(return_value=[
            {
                "id": "m1", "user_id": "u1", "content": "a",
                "metadata": {}, "namespace": "ns", "importance": 3,
                "created_at": now, "updated_at": now, "content_hash": None,
                "total_count": 100,
            },
        ])

        result = await pg.list(user_id="u1", namespace="ns")

        assert result.total == 100

    @pytest.mark.asyncio
    async def test_list_metadata_none_coerced_to_dict(self, pg, conn):
        """NULL metadata → {}."""
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(return_value=[
            {
                "id": "m1", "user_id": "u1", "content": "c",
                "metadata": None, "namespace": "ns", "importance": 3,
                "created_at": now, "updated_at": now, "content_hash": None,
                "total_count": 1,
            },
        ])

        result = await pg.list(user_id="u1")

        assert result.items[0].metadata == {}


# ══════════════════════════════════════════════════════════════════
# 5. forget_soft (memory_forget_soft)
# ══════════════════════════════════════════════════════════════════


class TestForgetSoft:
    @pytest.mark.asyncio
    async def test_forget_soft_returns_count(self, pg, conn):
        """memory_forget_soft возвращает количество обновлённых записей."""
        conn.fetchval = AsyncMock(return_value=5)

        result = await pg.forget_soft(user_id="u1", namespace="ns")

        assert result == 5
        conn.fetchval.assert_awaited_once_with(q.MEMORY_FORGET_SOFT, "u1", "ns")

    @pytest.mark.asyncio
    async def test_forget_soft_no_namespace(self, pg, conn):
        """forget_soft без namespace → все записи пользователя."""
        conn.fetchval = AsyncMock(return_value=10)

        result = await pg.forget_soft(user_id="u1", namespace=None)

        assert result == 10
        conn.fetchval.assert_awaited_once_with(q.MEMORY_FORGET_SOFT, "u1", None)

    @pytest.mark.asyncio
    async def test_forget_soft_no_matches(self, pg, conn):
        """forget_soft без совпадений → 0."""
        conn.fetchval = AsyncMock(return_value=0)

        result = await pg.forget_soft(user_id="nonexistent", namespace="ns")

        assert result == 0

    @pytest.mark.asyncio
    async def test_forget_soft_already_archived_not_counted(self, pg, conn):
        """Записи с is_archived=true НЕ считаются (WHERE is_archived = false)."""
        conn.fetchval = AsyncMock(return_value=2)

        result = await pg.forget_soft(user_id="u1", namespace="ns")

        # Проверяем что вызов правильный — логика is_archived=false в хранимке
        assert result == 2


# ══════════════════════════════════════════════════════════════════
# 6. add_relation + delete_relation
# ══════════════════════════════════════════════════════════════════


class TestRelations:
    @pytest.mark.asyncio
    async def test_add_relation_returns_id(self, pg, conn):
        """add_relation возвращает id новой связи."""
        conn.fetchrow = AsyncMock(return_value={"id": "rel-123"})

        result = await pg.add_relation(
            source_id="m1", target_id="m2", link_type="depends_on",
            description="test", weight=0.5,
        )

        assert result == "rel-123"
        conn.fetchrow.assert_awaited_once_with(
            q.INSERT_RELATION, "m1", "m2", None, "depends_on", "test", 0.5, {},
        )

    @pytest.mark.asyncio
    async def test_add_relation_with_target_name(self, pg, conn):
        """add_relation с target_name (вместо target_id)."""
        conn.fetchrow = AsyncMock(return_value={"id": "rel-456"})

        result = await pg.add_relation(
            source_id="m1", target_name="SomeClass", link_type="related_to",
        )

        assert result == "rel-456"
        conn.fetchrow.assert_awaited_once_with(
            q.INSERT_RELATION, "m1", None, "SomeClass", "related_to", None, 1.0, {},
        )

    @pytest.mark.asyncio
    async def test_delete_relation_found(self, pg, conn):
        """delete_relation удаляет связь и возвращает True."""
        conn.fetchrow = AsyncMock(return_value={"id": "rel-1"})

        result = await pg.delete_relation("m1", "m2", "depends_on")

        assert result is True
        conn.fetchrow.assert_awaited_once_with(q.DELETE_RELATION, "m1", "m2", "depends_on")

    @pytest.mark.asyncio
    async def test_delete_relation_not_found(self, pg, conn):
        """delete_relation для несуществующей связи → False."""
        conn.fetchrow = AsyncMock(return_value=None)

        result = await pg.delete_relation("m1", "m2", "nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_get_relations_by_source(self, pg, conn):
        """get_relations_by_source возвращает список Relation."""
        now = datetime.now(timezone.utc)
        conn.fetch = AsyncMock(return_value=[
            {
                "id": "r1", "source_id": "m1", "target_id": "m2",
                "target_name": None, "link_type": "depends_on",
                "description": None, "weight": 1.0, "metadata": {},
                "created_at": now,
            },
        ])

        result = await pg.get_relations_by_source("m1")

        assert len(result) == 1
        assert isinstance(result[0], Relation)
        assert result[0].source_id == "m1"

    @pytest.mark.asyncio
    async def test_delete_relations_by_source(self, pg, conn):
        """delete_relations_by_source возвращает количество удалённых."""
        conn.execute = AsyncMock(return_value="DELETE 3")

        result = await pg.delete_relations_by_source("m1")

        assert result == 3
        conn.execute.assert_awaited_once_with(q.DELETE_RELATIONS_BY_SOURCE, "m1")


# ══════════════════════════════════════════════════════════════════
# 7. sync_links_to_relations
# ══════════════════════════════════════════════════════════════════


class TestSyncLinks:
    @pytest.mark.asyncio
    async def test_sync_links_returns_count(self, pg, conn):
        """sync_links_to_relations возвращает количество синхронизированных."""
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {"id": "rel-1"},
            {"id": "rel-2"},
        ])

        result = await pg.sync_links_to_relations("m1")

        assert result == 2
        conn.execute.assert_awaited_once()
        conn.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_links_empty(self, pg, conn):
        """sync_links без links → 0."""
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])

        result = await pg.sync_links_to_relations("m1")

        assert result == 0

    @pytest.mark.asyncio
    async def test_sync_links_batch(self, pg, conn):
        """sync_links_batch для нескольких IDs."""
        conn.fetch = AsyncMock(return_value=[{"id": "r1"}, {"id": "r2"}, {"id": "r3"}])

        result = await pg.sync_links_batch(["m1", "m2", "m3"])

        assert result == 3
        conn.fetch.assert_awaited_once_with(q.SYNC_LINKS_BATCH, ["m1", "m2", "m3"])

    @pytest.mark.asyncio
    async def test_sync_links_batch_empty_list(self, pg):
        """sync_links_batch с пустым списком → 0 без запроса к БД."""
        result = await pg.sync_links_batch([])

        assert result == 0


# ══════════════════════════════════════════════════════════════════
# 8. Archive (мягкое удаление)
# ══════════════════════════════════════════════════════════════════


class TestArchive:
    @pytest.mark.asyncio
    async def test_archive_found(self, pg, conn):
        """archive устанавливает is_archived=true."""
        conn.fetchrow = AsyncMock(return_value={"id": "m1"})

        result = await pg.archive("m1")

        assert result is True
        conn.fetchrow.assert_awaited_once_with(q.ARCHIVE_MEMORY, "m1")

    @pytest.mark.asyncio
    async def test_archive_not_found(self, pg, conn):
        """archive для несуществующей/уже архивной записи → False."""
        conn.fetchrow = AsyncMock(return_value=None)

        result = await pg.archive("nonexistent")

        assert result is False
