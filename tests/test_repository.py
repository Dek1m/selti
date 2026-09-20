import os
import re
from unittest.mock import AsyncMock, MagicMock

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
            False,  # include_historical
            None, None, None, None,  # REST-фильтры 5.1/5.2: after/before, status, entity_type
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
            False,  # include_historical
            None, None, None, None,  # REST-фильтры 5.1/5.2: after/before, status, entity_type
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
    async def test_update_wrapper_fields(self, repo, conn):
        """V3.0: UPDATE_MEMORY правит только обвязку — metadata merge,
        importance, project_id, confidence, frozen, supersedes."""
        conn.fetchrow = AsyncMock(
            return_value=memory_row(id="mem-1", metadata={"k": "v"}, importance=5)
        )

        record = await repo.update(
            memory_id="mem-1",
            metadata={"k": "v"},
            importance=5,
            confidence=0.9,
        )

        assert isinstance(record, MemoryRecord)
        assert record.importance == 5
        conn.fetchrow.assert_awaited_once_with(
            q.UPDATE_MEMORY,
            "mem-1",
            {"k": "v"},
            5,
            None,   # project_id
            0.9,    # confidence
            None,   # frozen
            False,  # clear_project_id
            None,   # supersedes
        )
        # Регрессия immutable-content (E.2 ADR-019): в SET-части нет ни
        # content, ни content_hash (RETURNING-проекция содержит m.content —
        # это чтение, не запись)
        sql_set = q.UPDATE_MEMORY.split("RETURNING")[0]
        assert "content" not in sql_set

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

        record = await repo.update(memory_id="mem-1", metadata=None)
        assert record is None

    @pytest.mark.asyncio
    async def test_update_not_found(self, repo, conn):
        conn.fetchrow = AsyncMock(return_value=None)

        record = await repo.update(memory_id="missing", frozen=True)
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
        conn.fetchval.assert_awaited_once_with(q.FORGET_MEMORIES, "u1", NS_ID, None)

    @pytest.mark.asyncio
    async def test_forget_without_namespace(self, repo, conn):
        conn.fetchval = AsyncMock(return_value=0)

        deleted = await repo.forget(user_id="u1", namespace=None)
        assert deleted == 0
        conn.fetchval.assert_awaited_once_with(q.FORGET_MEMORIES, "u1", None, None)

    @pytest.mark.asyncio
    async def test_forget_project_scope(self, repo, conn):
        """Фаза 3.1: забвение в рамках проекта — project_id уходит в SQL."""
        conn.fetchval = AsyncMock(return_value=2)

        deleted = await repo.forget(user_id="u1", namespace=None, project_id="proj-uuid")

        assert deleted == 2
        conn.fetchval.assert_awaited_once_with(q.FORGET_MEMORIES, "u1", None, "proj-uuid")


# ---------------------------------------------------------------------------
# Hybrid search (Фаза 1.1): двухканальный сбор кандидатов
# ---------------------------------------------------------------------------

DENSE_POINTS = [
    {"id": "d1", "score": 0.91, "payload": {}, "vector": [1.0, 0.0]},
    {"id": "both", "score": 0.85, "payload": {}, "vector": [0.0, 1.0]},
]


def _hybrid_row(rid: str) -> dict:
    from tests.conftest import memory_row
    return memory_row(id=rid, content=f"content-{rid}")


@pytest.fixture
def hybrid_repo(mock_pool, mock_ns_resolver):
    pg = PostgreSQLRepository(pool=mock_pool)
    qdrant = MagicMock()
    qdrant.search = MagicMock(return_value=list(DENSE_POINTS))
    return MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=mock_ns_resolver)


@pytest.fixture
def hybrid_conn(hybrid_repo):
    return hybrid_repo.pg.pool.acquire.return_value.__aenter__.return_value


class TestSearchHybrid:
    @pytest.mark.asyncio
    async def test_channels_merged_with_ranks(self, hybrid_repo, hybrid_conn):
        """Кандидаты обоих каналов собираются; пересечение получает оба ранга."""
        fts_rows = [
            {**_hybrid_row("f1"), "score": 0.8},
            {**_hybrid_row("both"), "score": 0.7},
        ]
        pg_rows = [_hybrid_row("d1"), _hybrid_row("both"), _hybrid_row("f1")]
        hybrid_conn.fetch = AsyncMock(side_effect=[fts_rows, pg_rows])

        candidates = await hybrid_repo.search_hybrid(
            query_embedding=[0.1, 0.2],
            query_text="запрос",
            user_id="u1",
        )

        assert len(candidates) == 3
        by_id = {c.id: c for c in candidates}
        assert by_id["both"].rank_dense == 1
        assert by_id["both"].rank_fts == 1
        assert by_id["d1"].rank_fts is None
        assert by_id["f1"].rank_dense is None
        # вектора канала A доезжают для MMR
        assert by_id["d1"].vector == [1.0, 0.0]

    @pytest.mark.asyncio
    async def test_dense_failure_degrades_to_fts(self, hybrid_repo, hybrid_conn):
        """Отказ Qdrant-канала не роняет поиск — FTS-only."""
        hybrid_repo.qdrant.search = MagicMock(side_effect=RuntimeError("circuit open"))
        hybrid_conn.fetch = AsyncMock(return_value=[{**_hybrid_row("f1"), "score": 0.9}])

        candidates = await hybrid_repo.search_hybrid(
            query_embedding=[0.1], query_text="запрос"
        )

        assert [c.id for c in candidates] == ["f1"]

    @pytest.mark.asyncio
    async def test_fts_failure_degrades_to_dense(self, hybrid_repo, hybrid_conn):
        """Отказ FTS-канала не роняет поиск — dense-only."""
        async def fetch_side_effect(query, *args):
            if query == q.SEARCH_MEMORIES:
                raise RuntimeError("pg down")
            return [_hybrid_row("d1"), _hybrid_row("both")]

        hybrid_conn.fetch = AsyncMock(side_effect=fetch_side_effect)

        candidates = await hybrid_repo.search_hybrid(
            query_embedding=[0.1], query_text="запрос"
        )

        assert {c.id for c in candidates} == {"d1", "both"}

    @pytest.mark.asyncio
    async def test_unknown_namespace_returns_empty(self, hybrid_repo, mock_ns_resolver):
        async def missing(uid):
            return None

        mock_ns_resolver.get_by_uid = missing
        candidates = await hybrid_repo.search_hybrid(
            query_embedding=[0.1], query_text="x", namespace="ghost"
        )
        assert candidates == []

    @pytest.mark.asyncio
    async def test_include_historical_disables_activity_filters(self, hybrid_repo, hybrid_conn):
        """time-travel (Фаза 1.3): active_only=False в оба канала."""
        from memory_server.memory.qdrant_store import QdrantStore

        hybrid_conn.fetch = AsyncMock(return_value=[])

        await hybrid_repo.search_hybrid(
            query_embedding=[0.1], query_text="x", include_historical=True
        )

        # канал A: build_filter с active_only=False
        build_kwargs = hybrid_repo.qdrant.search.call_args.kwargs
        assert build_kwargs["query_filter"] == QdrantStore.build_filter(active_only=False)
        # канал B: SEARCH_MEMORIES с include_historical=True
        fts_call = hybrid_conn.fetch.await_args_list[0]
        assert fts_call.args == (
            q.SEARCH_MEMORIES, "x", None, None, None, 100, True, None, None, None, None,
        )
        # догрузка: FETCH_MEMORIES_BY_IDS с include_historical=True
        # (args = (SQL, ids, include_historical)) — без инверсии
        fetch_call = hybrid_conn.fetch.await_args_list[1]
        assert fetch_call.args[0] == q.FETCH_MEMORIES_BY_IDS
        assert fetch_call.args[2] is True


# ---------------------------------------------------------------------------
# PG batch-операции Фазы 1.2/1.4: batch-dedup + bump_access
# ---------------------------------------------------------------------------

class TestPgBatchOperations:
    @pytest.mark.asyncio
    async def test_find_by_content_hashes_maps_pairs(self, repo, conn):
        """Batch exact-dedup: {(ns, hash): record} одним запросом (Фаза 1.4)."""
        row = memory_row(id="mem-1", content="A", content_hash="ha")
        row["ns_uid"] = "default"
        row["matched_hash"] = "ha"
        conn.fetch = AsyncMock(return_value=[row])

        found = await repo.pg.find_by_content_hashes(["default", "default"], ["ha", "hb"])

        assert set(found.keys()) == {("default", "ha")}
        assert found[("default", "ha")].id == "mem-1"
        conn.fetch.assert_awaited_once_with(
            q.SELECT_MEMORY_BY_CONTENT_HASHES, ["default", "default"], ["ha", "hb"]
        )

    @pytest.mark.asyncio
    async def test_find_by_content_hashes_empty_input(self, repo, conn):
        found = await repo.pg.find_by_content_hashes([], [])
        assert found == {}
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bump_access_counts_updated(self, repo, conn):
        conn.execute = AsyncMock(return_value="UPDATE 3")

        updated = await repo.pg.bump_access(["a", "b", "c"])

        assert updated == 3
        conn.execute.assert_awaited_once_with(q.BUMP_ACCESS_MEMORIES, ["a", "b", "c"])

    @pytest.mark.asyncio
    async def test_bump_access_empty_skips_query(self, repo, conn):
        assert await repo.pg.bump_access([]) == 0
        conn.execute.assert_not_awaited()


class TestFetchByIdsSemantics:
    """Регрессия инверсии актив-фильтра FETCH_MEMORIES_BY_IDS (приёмка Фазы 1).

    SQL: ($2::bool OR (status='asserted' AND valid_to IS NULL)) — $2=True
    ОТКЛЮЧАЕТ фильтр, т.е. $2 = include_historical (зеркально $6 в
    SEARCH_MEMORIES, куда search_fts передаёт его же). Python обязан
    передавать параметр напрямую, без not-инверсии.
    """

    def test_sql_param_disables_activity_filter_via_or(self):
        """$2 связан с фильтром актуальности через OR: True = без фильтра."""
        assert re.search(
            r"\(\s*\$2::bool\s+OR\s+\("
            r"\s*m\.status\s*=\s*'asserted'\s+AND\s+m\.valid_to\s+IS\s+NULL\s*\)",
            q.FETCH_MEMORIES_BY_IDS,
        ), "SQL-структура $2 изменилась — проверь семантику include_historical"

    @pytest.mark.asyncio
    async def test_include_historical_true_passes_true(self, repo, conn):
        """time-travel: include_historical=True → в SQL уходит True (без инверсии)."""
        conn.fetch = AsyncMock(return_value=[])

        await repo.pg.fetch_by_ids(["mem-1"], include_historical=True)

        # 5.1/5.2: +created_after/created_before/status/entity_type (NULL = фильтр выключен)
        conn.fetch.assert_awaited_once_with(
            q.FETCH_MEMORIES_BY_IDS, ["mem-1"], True, None, None, None, None
        )

    @pytest.mark.asyncio
    async def test_default_passes_false_active_only(self, repo, conn):
        """Дефолт — только актуальные гранулы: в SQL уходит False."""
        conn.fetch = AsyncMock(return_value=[])

        await repo.pg.fetch_by_ids(["mem-1"])

        conn.fetch.assert_awaited_once_with(
            q.FETCH_MEMORIES_BY_IDS, ["mem-1"], False, None, None, None, None
        )

    @pytest.mark.asyncio
    async def test_dense_search_passes_include_historical_verbatim(
        self, repo, conn, mock_embedding_provider
    ):
        """Фасад search(): include_historical доходит до SQL без not-инверсии."""
        conn.fetch = AsyncMock(return_value=[])
        repo.qdrant = MagicMock()
        repo.qdrant.search = MagicMock(return_value=[{"id": "mem-1", "score": 0.9}])

        await repo.search(
            query_embedding=[0.1, 0.2, 0.3],
            query_text="x",
            include_historical=True,
        )

        sql_args = [
            c for c in conn.fetch.await_args_list if c.args[0] == q.FETCH_MEMORIES_BY_IDS
        ]
        assert sql_args and sql_args[0].args[2] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not os.getenv("SELTI_TEST_DATABASE_URL"),
        reason=(
            "Сценарный интеграционный прогон на реальном PG (ai.atom.ui): "
            "задать SELTI_TEST_DATABASE_URL=postgresql://... — без него SKIP"
        ),
    )
    async def test_real_pg_retracted_visible_only_with_historical(self):
        """Реальный SQL: retracted-гранула видна только при include_historical=True."""
        import asyncpg

        dsn = os.environ["SELTI_TEST_DATABASE_URL"]
        conn = await asyncpg.connect(dsn=dsn)
        try:
            probe_id = await conn.fetchval(
                """
                INSERT INTO memories (
                    user_id, content, metadata, namespace_id,
                    content_hash, importance, status, valid_to
                )
                VALUES (
                    'selti_regression_test',
                    'fetch_by_ids semantics probe',
                    '{}'::jsonb,
                    (SELECT id FROM namespaces ORDER BY id LIMIT 1),
                    'probe-' || gen_random_uuid()::text,
                    1,
                    'retracted',
                    now()
                )
                RETURNING id::text
                """
            )
            try:
                active = await conn.fetch(
                    q.FETCH_MEMORIES_BY_IDS, [probe_id], False, None, None, None
                )
                historical = await conn.fetch(
                    q.FETCH_MEMORIES_BY_IDS, [probe_id], True, None, None, None
                )
                assert active == [], (
                    "include_historical=False должен фильтровать retracted"
                )
                assert len(historical) == 1, (
                    "include_historical=True (time-travel) должен видеть retracted"
                )
            finally:
                await conn.execute("DELETE FROM memories WHERE id = $1", probe_id)
        finally:
            await conn.close()
