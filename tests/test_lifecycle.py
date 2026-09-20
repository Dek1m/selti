"""Фаза 2 плана редизайна — жизненный цикл гранул (supersession/decay/GC/clusters).

Слои: service (моки repository), pg_repository (моки pool), фасад
(Qdrant-синхронизация), Celery-задачи (eager + мок _get_service),
MCP- tools (мок celery_call), beat-расписание.

Сценарный тест плана (критерий приёмки Фазы 2): store A → supersede →
A.status='superseded', окно A закрыто valid_from A′, get_history=[A, A′];
decay не трогает frozen; GC dry-run/real; идемпотентность.
"""

import hashlib
import uuid as uuid_module
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from memory_server.exceptions import (
    ConflictError,
    DatabaseError,
    NotFoundError,
    SchemaPendingError,
    VectorStoreError,
)
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.memory.repository import MemoryRepository, collect_cluster_pairs
from memory_server.memory.service import MemoryService
from memory_server.models import MemoryHistory, MemoryRecord
from tests.conftest import memory_row

OLD_ID = "00000000-0000-0000-0000-0000000000a1"
NEW_ID = "00000000-0000-0000-0000-0000000000a2"
FOREIGN_ID = "00000000-0000-0000-0000-0000000000a3"
NS_ID = "00000000-0000-0000-0000-0000000000aa"


def _record(**overrides) -> MemoryRecord:
    return MemoryRecord(**memory_row(**overrides))


# ══════════════════════════════════════════════════════════════════
# Service: create_version / get_history / retract / freeze
# ══════════════════════════════════════════════════════════════════


class TestCreateVersion:
    @pytest.mark.asyncio
    async def test_supersede_closes_old_and_inherits(self, mock_service):
        old = _record(
            id=OLD_ID, confidence=1.0, importance=4,
            metadata={"entity_name": "selti", "keep": "yes"},
        )
        new = _record(id=NEW_ID, content="v2", supersedes=OLD_ID, confidence=0.9)
        mock_service.repository.get_by_id = AsyncMock(return_value=old)
        mock_service.repository.find_by_content_hash = AsyncMock(return_value=None)
        mock_service.repository.create_version = AsyncMock(return_value=new)

        result = await mock_service.create_version(
            OLD_ID, "v2", metadata_merge={"entity_name": "selti-v2"}
        )

        assert result.id == NEW_ID
        # confidence наследуется ×0.9 от старой
        kwargs = mock_service.repository.create_version.await_args.kwargs
        assert kwargs["confidence"] == pytest.approx(0.9)
        # metadata = merge(старая, новая) — ключи новой затирают
        assert kwargs["metadata"] == {"entity_name": "selti-v2", "keep": "yes"}
        # content_hash пересчитан
        assert kwargs["content_hash"] == hashlib.sha256(b"v2").hexdigest()
        # importance без override → None (SQL унаследует old.importance)
        assert kwargs["importance"] is None
        assert kwargs["old_id"] == OLD_ID

    @pytest.mark.asyncio
    async def test_supersede_confidence_capped_to_1(self, mock_service):
        old = _record(id=OLD_ID, confidence=1.0)
        mock_service.repository.get_by_id = AsyncMock(return_value=old)
        mock_service.repository.find_by_content_hash = AsyncMock(return_value=None)
        mock_service.repository.create_version = AsyncMock(return_value=_record(id=NEW_ID))

        await mock_service.create_version(OLD_ID, "v2")

        kwargs = mock_service.repository.create_version.await_args.kwargs
        assert kwargs["confidence"] == pytest.approx(0.9)
        assert 0.0 <= kwargs["confidence"] <= 1.0

    @pytest.mark.asyncio
    async def test_supersede_low_confidence_factor_keeps_floor_zero(self, mock_service):
        old = _record(id=OLD_ID, confidence=0.05)
        mock_service.repository.get_by_id = AsyncMock(return_value=old)
        mock_service.repository.find_by_content_hash = AsyncMock(return_value=None)
        mock_service.repository.create_version = AsyncMock(return_value=_record(id=NEW_ID))

        await mock_service.create_version(OLD_ID, "v2")

        kwargs = mock_service.repository.create_version.await_args.kwargs
        assert kwargs["confidence"] == pytest.approx(0.045)
        assert kwargs["confidence"] >= 0.0

    @pytest.mark.asyncio
    async def test_supersede_identical_content_conflict(self, mock_service):
        same_hash = hashlib.sha256(b"same").hexdigest()
        old = _record(id=OLD_ID, content="same", content_hash=same_hash)
        mock_service.repository.get_by_id = AsyncMock(return_value=old)

        with pytest.raises(ConflictError, match="identical"):
            await mock_service.create_version(OLD_ID, "same")

    @pytest.mark.asyncio
    async def test_supersede_foreign_active_hash_conflict(self, mock_service):
        """Контент чужой активной гранулы: ConflictError с id виновника ДО SQL.

        Без pre-check unique-индекс idx_memories_content_hash_active (020)
        абортирует INSERT сырым UniqueViolationError.
        """
        old = _record(id=OLD_ID, content="v1", content_hash=hashlib.sha256(b"v1").hexdigest())
        twin = _record(
            id=FOREIGN_ID, content="v2", status="asserted", valid_to=None,
            content_hash=hashlib.sha256(b"v2").hexdigest(),
        )
        mock_service.repository.get_by_id = AsyncMock(return_value=old)
        mock_service.repository.find_by_content_hash = AsyncMock(return_value=twin)
        mock_service.repository.create_version = AsyncMock()

        with pytest.raises(ConflictError, match="already active") as exc_info:
            await mock_service.create_version(OLD_ID, "v2")

        assert exc_info.value.id == FOREIGN_ID  # id найденной гранулы, не base
        mock_service.repository.find_by_content_hash.assert_awaited_once_with(
            old.namespace, hashlib.sha256(b"v2").hexdigest()
        )
        mock_service.repository.create_version.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_supersede_own_hash_lookup_not_a_conflict(self, mock_service):
        """Lookup вернул саму base-гранулу (hash-рассинхрон старой строки) —
        это не чужой дубль, версия создаётся.
        """
        old = _record(id=OLD_ID, content="v1", content_hash=hashlib.sha256(b"v1").hexdigest())
        mock_service.repository.get_by_id = AsyncMock(return_value=old)
        mock_service.repository.find_by_content_hash = AsyncMock(return_value=old)
        mock_service.repository.create_version = AsyncMock(return_value=_record(id=NEW_ID))

        result = await mock_service.create_version(OLD_ID, "v2")

        assert result.id == NEW_ID
        mock_service.repository.create_version.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_supersede_non_asserted_conflict(self, mock_service):
        old = _record(id=OLD_ID, status="superseded")
        mock_service.repository.get_by_id = AsyncMock(return_value=old)

        with pytest.raises(ConflictError, match="asserted"):
            await mock_service.create_version(OLD_ID, "v2")

    @pytest.mark.asyncio
    async def test_supersede_not_found(self, mock_service):
        mock_service.repository.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await mock_service.create_version(OLD_ID, "v2")


class TestGetHistory:
    @pytest.mark.asyncio
    async def test_history_ordered_current_marked(self, mock_service):
        oldest = _record(id=OLD_ID, status="superseded", superseded_by=NEW_ID)
        newest = _record(id=NEW_ID, supersedes=OLD_ID, status="asserted", valid_to=None)
        mock_service.repository.get_by_id = AsyncMock(return_value=newest)
        mock_service.repository.get_history = AsyncMock(return_value=[oldest, newest])

        history = await mock_service.get_history(NEW_ID)

        assert isinstance(history, MemoryHistory)
        assert [r.id for r in history.items] == [OLD_ID, NEW_ID]  # старейшая → новейшая
        assert history.current_id == NEW_ID

    @pytest.mark.asyncio
    async def test_history_all_closed_current_none(self, mock_service):
        closed = _record(id=OLD_ID, status="superseded", superseded_by=NEW_ID)
        retracted = _record(id=NEW_ID, status="retracted", valid_to=datetime.now(timezone.utc))
        mock_service.repository.get_by_id = AsyncMock(return_value=closed)
        mock_service.repository.get_history = AsyncMock(return_value=[closed, retracted])

        history = await mock_service.get_history(OLD_ID)
        assert history.current_id is None

    @pytest.mark.asyncio
    async def test_history_not_found(self, mock_service):
        mock_service.repository.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await mock_service.get_history(OLD_ID)


class TestRetractAndFreeze:
    @pytest.mark.asyncio
    async def test_retract_merges_reason(self, mock_service):
        record = _record(id=OLD_ID)
        mock_service.repository.get_by_id = AsyncMock(return_value=record)
        mock_service.repository.archive = AsyncMock(return_value=True)

        assert await mock_service.retract(OLD_ID, reason="obsolete") is True
        mock_service.repository.archive.assert_awaited_once_with(OLD_ID, reason="obsolete")

    @pytest.mark.asyncio
    async def test_archive_tool_path_delegates_to_retract(self, mock_service):
        record = _record(id=OLD_ID)
        mock_service.repository.get_by_id = AsyncMock(return_value=record)
        mock_service.repository.archive = AsyncMock(return_value=True)

        assert await mock_service.archive(OLD_ID) is True
        mock_service.repository.archive.assert_awaited_once_with(OLD_ID, reason=None)

    @pytest.mark.asyncio
    async def test_retract_not_found(self, mock_service):
        mock_service.repository.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await mock_service.retract(OLD_ID, reason="x")

    @pytest.mark.asyncio
    async def test_freeze_sets_flag(self, mock_service):
        frozen = _record(id=OLD_ID, frozen=True)
        mock_service.repository.update = AsyncMock(return_value=frozen)

        result = await mock_service.freeze(OLD_ID, frozen=True)

        assert result.frozen is True
        mock_service.repository.update.assert_awaited_once_with(memory_id=OLD_ID, frozen=True)

    @pytest.mark.asyncio
    async def test_freeze_not_found(self, mock_service):
        mock_service.repository.update = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await mock_service.freeze(OLD_ID, frozen=True)


# ══════════════════════════════════════════════════════════════════
# Service: decay / stale / gc / orphans / clusters
# ══════════════════════════════════════════════════════════════════


class TestServiceLifecycle:
    @pytest.mark.asyncio
    async def test_decay_passes_configured_rates(self, mock_service):
        mock_service.repository.decay_confidence = AsyncMock(
            return_value={"default": 3, "user_facts": 1}
        )

        touched = await mock_service.decay_confidence()

        assert touched == {"default": 3, "user_facts": 1}
        kwargs = mock_service.repository.decay_confidence.await_args.kwargs
        assert kwargs["default_rate"] == mock_service.config.recency_decay_rate
        assert set(kwargs["ns_uids"]) == set(mock_service.config.recency_decay_rates.keys())
        assert kwargs["floor"] == mock_service.config.confidence_decay_floor

    @pytest.mark.asyncio
    async def test_mark_stale_counts_without_status_change(self, mock_service):
        mock_service.repository.count_stale = AsyncMock(return_value=7)

        assert await mock_service.mark_stale() == 7
        mock_service.repository.count_stale.assert_awaited_once_with(
            mock_service.config.stale_threshold, mock_service.config.stale_days
        )

    @pytest.mark.asyncio
    async def test_gc_disabled_by_default_never_deletes(self, mock_service):
        """V3.1 стоп-кран (F ADR-019, дыра 7): дефолт — purge выключен,
        только счётчик кандидатов; мина FK обезврежена."""
        assert mock_service.config.gc_purge_enabled is False
        assert mock_service.config.gc_mode == "disabled"
        mock_service.repository.select_gc_superseded = AsyncMock(return_value=[OLD_ID])
        mock_service.repository.purge_memories = AsyncMock()

        result = await mock_service.gc_superseded()

        assert result["deleted"] == 0
        assert result["selected"] == 1
        assert result["mode"] == "disabled"
        mock_service.repository.purge_memories.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gc_master_switch_blocks_even_hard_mode(self, mock_service):
        """gc_mode='hard', но gc_purge_enabled=False — мастер-кран выше
        режимов: удаление невозможно в принципе."""
        mock_service.config.gc_mode = "hard"
        mock_service.config.gc_purge_enabled = False
        mock_service.repository.select_gc_superseded = AsyncMock(return_value=[OLD_ID])
        mock_service.repository.purge_memories = AsyncMock()

        result = await mock_service.gc_superseded()

        assert result["deleted"] == 0
        mock_service.repository.purge_memories.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gc_hard_mode_with_enabled_purge_deletes(self, mock_service):
        """hard + gc_purge_enabled=True — как раньше: hard delete кандидатов."""
        mock_service.config.gc_mode = "hard"
        mock_service.config.gc_purge_enabled = True
        mock_service.repository.select_gc_superseded = AsyncMock(
            return_value=[OLD_ID, NEW_ID]
        )
        mock_service.repository.purge_memories = AsyncMock(return_value=2)

        result = await mock_service.gc_superseded()

        assert result["selected"] == 2
        assert result["deleted"] == 2
        mock_service.repository.purge_memories.assert_awaited_once_with([OLD_ID, NEW_ID])

    @pytest.mark.asyncio
    async def test_gc_no_candidates_idempotent(self, mock_service):
        mock_service.repository.select_gc_superseded = AsyncMock(return_value=[])
        mock_service.repository.purge_memories = AsyncMock()

        result = await mock_service.gc_superseded()
        assert result["deleted"] == 0
        mock_service.repository.purge_memories.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_orphans_cleanup(self, mock_service):
        mock_service.repository.delete_orphan_relations = AsyncMock(return_value=5)
        assert await mock_service.orphans_cleanup() == 5

    @pytest.mark.asyncio
    async def test_cluster_list_graceful_when_migration_pending(self, mock_service):
        mock_service.repository.list_clusters = AsyncMock(
            side_effect=SchemaPendingError("clusters: undefined table")
        )

        result = await mock_service.cluster_list()

        assert result["ok"] is False
        assert "022" in result["reason"]
        assert result["clusters"] == []

    @pytest.mark.asyncio
    async def test_refresh_clusters_passes_v2_config(self, mock_service):
        """Сервис пробрасывает порог/top_k/min_members из конфига в фасад v2."""
        cluster_row = {"cluster_id": OLD_ID, "member_count": 3, "coherence": 0.95}
        mock_service.repository.refresh_clusters = AsyncMock(return_value=[cluster_row])

        result = await mock_service.refresh_clusters("default")

        assert result == {"ok": True, "clusters": [cluster_row]}
        kwargs = mock_service.repository.refresh_clusters.await_args.kwargs
        assert kwargs["threshold"] == mock_service.config.cluster_threshold
        assert kwargs["top_k"] == mock_service.config.cluster_top_k
        assert kwargs["min_members"] == mock_service.config.cluster_min_members

    @pytest.mark.asyncio
    async def test_refresh_clusters_graceful_when_migration_pending(self, mock_service):
        mock_service.repository.refresh_clusters = AsyncMock(
            side_effect=SchemaPendingError("assign_clusters_from_pairs: undefined")
        )

        result = await mock_service.refresh_clusters("default")

        assert result == {"ok": False, "reason": "migration 022 pending", "clusters": []}

    @pytest.mark.asyncio
    async def test_refresh_clusters_graceful_when_qdrant_unavailable(self, mock_service):
        """Граничный: Qdrant недоступен → ok=False qdrant_unavailable, разметка не тронута."""
        mock_service.repository.refresh_clusters = AsyncMock(
            side_effect=VectorStoreError("qdrant ann failed at 512/8130 granules")
        )

        result = await mock_service.refresh_clusters("default")

        assert result == {"ok": False, "reason": "qdrant_unavailable", "clusters": []}

    @pytest.mark.asyncio
    async def test_refresh_clusters_unknown_namespace(self, mock_service):
        mock_service.ns_repo.get_by_uid = AsyncMock(return_value=None)
        mock_service.repository.refresh_clusters = AsyncMock()

        with pytest.raises(NotFoundError):
            await mock_service.refresh_clusters("no-such-namespace")

        mock_service.repository.refresh_clusters.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════
# PG repository: SQL-контракты Фазы 2
# ══════════════════════════════════════════════════════════════════


class TestPgCreateVersion:
    @pytest.mark.asyncio
    async def test_atomic_insert_supersede_and_rewire(self, mock_pool):
        from memory_server.db import queries as q

        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        inserted = {"id": NEW_ID, "namespace_id": NS_ID}
        closed = {"id": OLD_ID}
        record_row = memory_row(id=NEW_ID, supersedes=OLD_ID)
        conn.fetchrow = AsyncMock(side_effect=[inserted, closed, record_row])

        created = await pg.create_version(
            old_id=OLD_ID, content="v2", metadata={"k": 1},
            content_hash="h2", importance=None, confidence=0.9,
        )

        record, ns_id = created
        assert record.id == NEW_ID
        assert ns_id == NS_ID
        calls = conn.fetchrow.await_args_list
        assert calls[0].args[0] == q.INSERT_MEMORY_VERSION
        assert calls[0].args[1:] == (OLD_ID, "v2", {"k": 1}, "h2", None, 0.9)
        # SUPERSEDE(old, new): окно старой закрывается valid_from новой — одним SQL
        assert calls[1].args == (q.SUPERSEDE_MEMORY, OLD_ID, NEW_ID)
        assert calls[2].args == (q.SELECT_MEMORY_BY_ID, NEW_ID)
        # Инварианты правила Graphiti и версионирования — на уровне SQL
        assert "valid_to     = new.valid_from" in q.SUPERSEDE_MEMORY
        assert "old.version + 1" in q.INSERT_MEMORY_VERSION
        # V3.1 (дыра 2): cluster_id наследуется INSERT-SELECT'ом
        assert "old.cluster_id" in q.INSERT_MEMORY_VERSION
        # V3.1 (дыра 1): REWIRE рёбер в той же транзакции — обе стороны
        executes = conn.execute.await_args_list
        assert [c.args[0] for c in executes] == [
            q.REWIRE_RELATIONS_SOURCE, q.REWIRE_RELATIONS_TARGET,
        ]
        assert executes[0].args[1:] == (OLD_ID, NEW_ID)
        assert executes[1].args[1:] == (OLD_ID, NEW_ID)

    @pytest.mark.asyncio
    async def test_supersede_conflict_rolls_back(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(
            side_effect=[{"id": NEW_ID, "namespace_id": NS_ID}, None]
        )

        with pytest.raises(DatabaseError, match="not asserted"):
            await pg.create_version(
                old_id=OLD_ID, content="v2", metadata=None,
                content_hash="h2", importance=None, confidence=0.9,
            )

        # REWIRE не выполнялся: откат до переноса рёбер
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_old_missing_returns_none(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(return_value=None)

        assert await pg.create_version(
            old_id=OLD_ID, content="v2", metadata=None,
            content_hash="h2", importance=None, confidence=0.9,
        ) is None

    @pytest.mark.asyncio
    async def test_unique_violation_maps_to_conflict(self, mock_pool):
        """Страховка от гонки: UniqueViolationError индекса 020 → доменный
        ConflictError, а не сырое asyncpg-исключение наружу.
        """
        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(
            side_effect=asyncpg.exceptions.UniqueViolationError(
                "duplicate key value violates unique constraint "
                "\"idx_memories_content_hash_active\""
            )
        )

        with pytest.raises(ConflictError, match="content hash conflict"):
            await pg.create_version(
                old_id=OLD_ID, content="v2", metadata=None,
                content_hash="h2", importance=None, confidence=0.9,
            )


class TestPgHistory:
    @pytest.mark.asyncio
    async def test_history_recursive_cte(self, mock_pool):
        from memory_server.db import queries as q

        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(
            return_value=[memory_row(id=OLD_ID), memory_row(id=NEW_ID)]
        )

        items = await pg.get_history(NEW_ID)

        assert [r.id for r in items] == [OLD_ID, NEW_ID]
        sql = conn.fetch.await_args.args[0]
        assert sql == q.GET_HISTORY
        assert "RECURSIVE" in sql
        assert "backwards" in sql and "forwards" in sql


class TestGetHistorySqlSemantics:
    """Регрессия битого JOIN рекурсивных CTE GET_HISTORY (приёмка Фазы 2).

    Якорь CTE переименовал колонку (supersedes AS next_id) — у CТЕ НЕТ
    выходных колонок supersedes/superseded_by, поэтому рекурсивный шаг
    обязан джойниться по b.next_id / f.next_id. Обращения b.supersedes /
    f.superseded_by = UndefinedColumnError на живой БД при каждом вызове.
    """

    def test_recursive_steps_join_on_next_id(self):
        import re

        from memory_server.db import queries as q

        assert re.search(
            r"JOIN\s+backwards\s+b\s+ON\s+m\.id\s*=\s*b\.next_id", q.GET_HISTORY
        ), "backwards-шаг обязан джойниться по b.next_id (колонка якоря)"
        assert re.search(
            r"JOIN\s+forwards\s+f\s+ON\s+m\.id\s*=\s*f\.next_id", q.GET_HISTORY
        ), "forwards-шаг обязан джойниться по f.next_id (колонка якоря)"

    def test_no_stale_supersedes_references_on_cte_aliases(self):
        import re

        from memory_server.db import queries as q

        assert not re.search(r"\bb\.supersedes\b", q.GET_HISTORY)
        assert not re.search(r"\bf\.superseded_by\b", q.GET_HISTORY)


class TestPgLifecycleSql:
    @pytest.mark.asyncio
    async def test_decay_counts_per_namespace(self, mock_pool):
        from memory_server.db import queries as q

        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(
            return_value=[{"namespace": "default"}, {"namespace": "default"}, {"namespace": "user_facts"}]
        )

        touched = await pg.decay_confidence(
            ns_uids=["default", "user_facts"], rates=[0.995, 0.999],
            default_rate=0.995, floor=0.1,
        )

        assert touched == {"default": 2, "user_facts": 1}
        args = conn.fetch.await_args.args
        assert args[0] == q.DECAY_CONFIDENCE
        assert args[1:] == (["default", "user_facts"], [0.995, 0.999], 0.995, 0.1)
        # frozen и floor защищены на уровне SQL (D4)
        assert "NOT m.frozen" in args[0]
        assert "m.confidence > $4" in args[0]

    def test_decay_floor_greatest_guards_single_step_undershoot(self):
        """Одношаговое проседание под floor: 0.1005 × 0.995 = 0.0999975 < 0.1.

        WHERE confidence > floor не спасает (строка отбирается ДО умножения)
        — GREATEST(..., $4) прижимает результат к floor на уровне SQL.
        """
        import re

        from memory_server.db import queries as q

        assert 0.1005 * 0.995 < 0.1  # сам кейс: без GREATEST утонет под floor
        assert re.search(
            r"confidence\s*=\s*GREATEST\(\s*m\.confidence\s*\*\s*"
            r"COALESCE\(r\.rate,\s*\$3::float8\),\s*\$4::float8\s*\)",
            q.DECAY_CONFIDENCE,
        ), "floor обязан прижиматься GREATEST'ом в SET, не только WHERE-фильтром"

    @pytest.mark.asyncio
    async def test_count_and_list_stale(self, mock_pool):
        from memory_server.db import queries as q

        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchval = AsyncMock(return_value=4)
        conn.fetch = AsyncMock(return_value=[memory_row(id=OLD_ID)])

        assert await pg.count_stale(0.3, 30) == 4
        assert conn.fetchval.await_args.args == (q.COUNT_STALE, 0.3, 30)

        records = await pg.list_stale(0.3, 30, user_id="u1", limit=10)
        assert [r.id for r in records] == [OLD_ID]
        assert conn.fetch.await_args.args == (
            q.LIST_STALE, 0.3, 30, "u1", None, None, 10,
        )

    @pytest.mark.asyncio
    async def test_gc_select_and_delete(self, mock_pool):
        from memory_server.db import queries as q

        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value

        conn.fetch = AsyncMock(return_value=[{"id": OLD_ID}, {"id": NEW_ID}])
        ids = await pg.select_gc_superseded(90)
        assert ids == [OLD_ID, NEW_ID]
        assert conn.fetch.await_args.args == (q.SELECT_GC_SUPERSEDED, 90)
        # frozen защищён и от decay, и от GC (D4) — на уровне SQL
        assert "NOT frozen" in q.SELECT_GC_SUPERSEDED
        assert "NOT frozen" in q.DELETE_GC_SUPERSEDED

        # DELETE: dangling relations → memories, одной транзакцией
        conn.fetch = AsyncMock(side_effect=[[], [{"id": OLD_ID}, {"id": NEW_ID}]])
        deleted = await pg.delete_gc_superseded([OLD_ID, NEW_ID])
        assert deleted == 2
        calls = conn.fetch.await_args_list
        assert calls[0].args[0] == q.DELETE_GC_DANGLING_RELATIONS
        assert calls[1].args[0] == q.DELETE_GC_SUPERSEDED

    @pytest.mark.asyncio
    async def test_gc_delete_empty_is_noop(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        assert await pg.delete_gc_superseded([]) == 0
        mock_pool.acquire.assert_not_called()

    @pytest.mark.asyncio
    async def test_orphan_relations_delete(self, mock_pool):
        from memory_server.db import queries as q

        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": "r1"}, {"id": "r2"}])

        assert await pg.delete_orphan_relations() == 2
        assert conn.fetch.await_args.args[0] == q.DELETE_ORPHAN_RELATIONS


class TestPgClustersV2:
    """PG-слой кластеризации v2: fetch входа ANN + apply готовых пар."""

    @pytest.mark.asyncio
    async def test_fetch_asserted_ids_contract(self, mock_pool):
        from memory_server.db import queries as q

        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": OLD_ID}, {"id": NEW_ID}])

        ids = await pg.fetch_asserted_ids(NS_ID)

        assert ids == [OLD_ID, NEW_ID]
        sql = conn.fetch.await_args.args[0]
        assert sql == q.SELECT_ASSERTED_CLUSTER_IDS
        # канонические грани входа — те же, что у Qdrant active_only
        assert "status = 'asserted'" in sql
        assert "valid_to IS NULL" in sql

    @pytest.mark.asyncio
    async def test_apply_cluster_pairs_copy_and_call(self, mock_pool):
        from memory_server.db import queries as q

        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        cluster_row = {"cluster_id": OLD_ID, "member_count": 2, "coherence": 0.97}
        conn.fetch = AsyncMock(return_value=[cluster_row])

        rows = await pg.apply_cluster_pairs(NS_ID, [(OLD_ID, NEW_ID, 0.97)], min_members=2)

        assert rows == [cluster_row]
        assert conn.execute.await_args.args[0] == q.CREATE_CLUSTER_PAIRS_TEMP
        assert "ON COMMIT DROP" in q.CREATE_CLUSTER_PAIRS_TEMP
        # COPY в temp-таблицу — без подготовленных планов
        copy_kwargs = conn.copy_records_to_table.await_args.kwargs
        assert copy_kwargs["records"] == [
            (
                uuid_module.UUID(OLD_ID),
                uuid_module.UUID(NEW_ID),
                0.97,
            )
        ]
        assert copy_kwargs["columns"] == ("a_id", "b_id", "similarity")
        assert conn.fetch.await_args.args == (q.REFRESH_CLUSTERS, NS_ID, 2)
        assert "assign_clusters_from_pairs" in q.REFRESH_CLUSTERS

    @pytest.mark.asyncio
    async def test_apply_cluster_pairs_empty_still_calls_proc(self, mock_pool):
        """Пустые пары — identity-пересчёт: хранимка снимет протухшую разметку."""
        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[])

        rows = await pg.apply_cluster_pairs(NS_ID, [])

        assert rows == []
        conn.copy_records_to_table.assert_not_awaited()
        conn.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_apply_cluster_pairs_schema_pending(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(
            side_effect=asyncpg.exceptions.UndefinedFunctionError(
                'function assign_clusters_from_pairs(uuid, integer) does not exist'
            )
        )

        with pytest.raises(SchemaPendingError):
            await pg.apply_cluster_pairs(NS_ID, [(OLD_ID, NEW_ID, 0.97)])

    @pytest.mark.asyncio
    async def test_list_clusters_undefined_table_pending(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(
            side_effect=asyncpg.exceptions.UndefinedTableError('relation "clusters" does not exist')
        )

        with pytest.raises(SchemaPendingError):
            await pg.list_clusters()


class TestCollectClusterPairs:
    """Юнит разбора ANN-выдач → канонические пары (без Qdrant, чистая функция)."""

    def test_self_match_excluded_and_dedup_symmetric(self):
        seen: set[tuple[str, str]] = set()

        # A нашла соседей: себя (score 1.0) и B; B нашла A — симметричный дубль
        pairs_a = collect_cluster_pairs(
            [OLD_ID],
            [[{"id": OLD_ID, "score": 1.0}, {"id": NEW_ID, "score": 0.97}]],
            seen,
        )
        pairs_b = collect_cluster_pairs(
            [NEW_ID], [[{"id": OLD_ID, "score": 0.97}]], seen
        )

        assert pairs_a == [(OLD_ID, NEW_ID, 0.97)]  # self отброшен, канонич. a < b
        assert pairs_b == []  # дубль (B,A) не дублируется

    def test_canonical_order_regardless_of_direction(self):
        # id_b < id_a лексикографически: пара нормализована в (b, a)
        seen: set[tuple[str, str]] = set()
        pairs = collect_cluster_pairs(
            [NEW_ID], [[{"id": OLD_ID, "score": 0.93}]], seen
        )
        assert pairs == [(OLD_ID, NEW_ID, 0.93)]

    def test_batch_alignment_and_chain(self):
        """Выдачи строго по порядку origin'ов: A-B, B-C → цепочка двумя парами."""
        c_id = "00000000-0000-0000-0000-0000000000c1"
        seen: set[tuple[str, str]] = set()
        pairs = collect_cluster_pairs(
            [OLD_ID, NEW_ID, c_id],
            [
                [{"id": NEW_ID, "score": 0.95}],
                [{"id": OLD_ID, "score": 0.95}, {"id": c_id, "score": 0.90}],
                [{"id": NEW_ID, "score": 0.90}],
            ],
            seen,
        )
        assert pairs == [(OLD_ID, NEW_ID, 0.95), (NEW_ID, c_id, 0.90)]

    def test_empty_inputs(self):
        assert collect_cluster_pairs([], [], set()) == []


class TestFacadeRefreshClustersV2:
    """Оркестрация v2: scroll 256 → retrieve → batch ANN → пары → apply."""

    def _repo(self, pg, qdrant) -> MemoryRepository:
        return MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=MagicMock())

    @pytest.mark.asyncio
    async def test_ann_pairs_flow_to_apply(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.fetch_asserted_ids = AsyncMock(return_value=[OLD_ID, NEW_ID])
        cluster_row = {"cluster_id": OLD_ID, "member_count": 2, "coherence": 0.97}
        pg.apply_cluster_pairs = AsyncMock(return_value=[cluster_row])

        qdrant = MagicMock()
        qdrant.retrieve_vectors = MagicMock(
            return_value={OLD_ID: [0.1, 0.0], NEW_ID: [0.0, 0.1]}
        )
        qdrant.search_batch = MagicMock(
            return_value=[
                [{"id": OLD_ID, "score": 1.0}, {"id": NEW_ID, "score": 0.97}],
                [{"id": OLD_ID, "score": 0.97}],
            ]
        )
        repo = self._repo(pg, qdrant)

        rows = await repo.refresh_clusters(NS_ID, threshold=0.92, top_k=10)

        assert rows == [cluster_row]
        # пары дедупнуты, self исключён — в apply ушла одна каноническая пара
        pg.apply_cluster_pairs.assert_awaited_once_with(NS_ID, [(OLD_ID, NEW_ID, 0.97)], 2)
        # ANN-параметры: limit = top_k + 1 (self-match), порог, фильтр namespace
        kwargs = qdrant.search_batch.call_args.kwargs
        assert kwargs["limit"] == 11
        assert kwargs["score_threshold"] == 0.92
        from qdrant_client import models as qm

        assert isinstance(kwargs["query_filter"], qm.Filter)
        keys = {c.key for c in kwargs["query_filter"].must}
        assert keys == {"namespace_id", "status"}  # границы namespace + asserted

    @pytest.mark.asyncio
    async def test_granules_without_vector_skipped(self, mock_pool):
        """Точка без вектора (рассинхрон PG ↔ Qdrant) не участвует в ANN,
        но и не роняет пересчёт остальных."""
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.fetch_asserted_ids = AsyncMock(return_value=[OLD_ID, NEW_ID])
        pg.apply_cluster_pairs = AsyncMock(return_value=[])
        qdrant = MagicMock()
        qdrant.retrieve_vectors = MagicMock(return_value={OLD_ID: [0.1]})  # NEW_ID missing
        qdrant.search_batch = MagicMock(return_value=[[{"id": OLD_ID, "score": 1.0}]])
        repo = self._repo(pg, qdrant)

        await repo.refresh_clusters(NS_ID, threshold=0.9)

        # искали только для одной точки; пар нет — apply с пустым списком
        assert len(qdrant.search_batch.call_args.kwargs["query_vectors"]) == 1
        pg.apply_cluster_pairs.assert_awaited_once_with(NS_ID, [], 2)

    @pytest.mark.asyncio
    async def test_fewer_than_two_granules_skips_ann(self, mock_pool):
        """Меньше 2 гранул — ANN не зовём вовсе; пустой apply снимает
        протухшую разметку (кластеров из <2 гранул быть не может)."""
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.fetch_asserted_ids = AsyncMock(return_value=[OLD_ID])
        pg.apply_cluster_pairs = AsyncMock(return_value=[])
        qdrant = MagicMock()
        repo = self._repo(pg, qdrant)

        await repo.refresh_clusters(NS_ID, threshold=0.9)

        qdrant.retrieve_vectors.assert_not_called()
        qdrant.search_batch.assert_not_called()
        pg.apply_cluster_pairs.assert_awaited_once_with(NS_ID, [], 2)

    @pytest.mark.asyncio
    async def test_qdrant_failure_wrapped_and_no_apply(self, mock_pool):
        """Отказ Qdrant mid-scroll → VectorStoreError; apply НЕ вызван —
        существующая разметка кластеров не стирается пустыми парами."""
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.fetch_asserted_ids = AsyncMock(return_value=[OLD_ID, NEW_ID])
        pg.apply_cluster_pairs = AsyncMock()
        qdrant = MagicMock()
        qdrant.retrieve_vectors = MagicMock(
            side_effect=ConnectionError("qdrant unreachable")
        )
        repo = self._repo(pg, qdrant)

        with pytest.raises(VectorStoreError, match="qdrant ann failed"):
            await repo.refresh_clusters(NS_ID, threshold=0.9)

        pg.apply_cluster_pairs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_qdrant_not_configured_raises(self, mock_pool):
        """Qdrant-стора нет вовсе — та же честная ошибка, не тихая заливка пустоты."""
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.fetch_asserted_ids = AsyncMock(return_value=[OLD_ID, NEW_ID])
        pg.apply_cluster_pairs = AsyncMock()
        repo = MemoryRepository(pg=pg, qdrant=None, ns_repo=MagicMock())

        with pytest.raises(VectorStoreError, match="not configured"):
            await repo.refresh_clusters(NS_ID, threshold=0.9)

        pg.apply_cluster_pairs.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════
# Facade: Qdrant-синхронизация supersession/GC
# ══════════════════════════════════════════════════════════════════


class TestFacadeSupersessionSync:
    @pytest.mark.asyncio
    async def test_create_version_upserts_new_and_closes_old(self, mock_pool):
        new_record = _record(id=NEW_ID, importance=4)
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.create_version = AsyncMock(return_value=(new_record, NS_ID))
        qdrant = MagicMock()
        repo = MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=MagicMock())

        result = await repo.create_version(
            old_id=OLD_ID, content="v2", embedding=[0.1, 0.2],
            metadata={"a": 1}, content_hash="h2", confidence=0.9,
        )

        assert result.id == NEW_ID
        qdrant.upsert_vector.assert_called_once()
        upsert_kwargs = qdrant.upsert_vector.call_args.kwargs
        assert upsert_kwargs["point_id"] == NEW_ID
        assert upsert_kwargs["vector"] == [0.1, 0.2]
        assert upsert_kwargs["payload"]["status"] == "asserted"
        assert upsert_kwargs["payload"]["namespace_id"] == NS_ID
        assert upsert_kwargs["payload"]["importance"] == 4
        # старая версия уходит из выдачи фильтром status
        qdrant.set_payload.assert_called_once_with(
            point_id=OLD_ID, payload={"status": "superseded"}
        )

    @pytest.mark.asyncio
    async def test_create_version_without_embedding_only_marks_old(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.create_version = AsyncMock(return_value=(_record(id=NEW_ID), NS_ID))
        qdrant = MagicMock()
        repo = MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=MagicMock())

        await repo.create_version(old_id=OLD_ID, content="v2")

        qdrant.upsert_vector.assert_not_called()
        qdrant.set_payload.assert_called_once_with(
            point_id=OLD_ID, payload={"status": "superseded"}
        )

    @pytest.mark.asyncio
    async def test_archive_marks_qdrant_retracted(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.archive = AsyncMock(return_value=True)
        qdrant = MagicMock()
        repo = MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=MagicMock())

        assert await repo.archive(OLD_ID, reason="obsolete") is True
        qdrant.set_payload.assert_called_once_with(
            point_id=OLD_ID, payload={"status": "retracted"}
        )

    @pytest.mark.asyncio
    async def test_purge_memories_deletes_qdrant_points(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.delete_gc_superseded = AsyncMock(return_value=2)
        qdrant = MagicMock()
        repo = MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=MagicMock())

        deleted = await repo.purge_memories([OLD_ID, NEW_ID])

        assert deleted == 2
        qdrant.delete.assert_called_once_with(point_ids=[OLD_ID, NEW_ID])

    @pytest.mark.asyncio
    async def test_purge_nothing_keeps_qdrant(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        pg.delete_gc_superseded = AsyncMock(return_value=0)
        qdrant = MagicMock()
        repo = MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=MagicMock())

        assert await repo.purge_memories([OLD_ID]) == 0
        qdrant.delete.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# Celery tasks (eager)
# ══════════════════════════════════════════════════════════════════


def _task_service():
    svc = MagicMock()
    svc.create_version = AsyncMock(return_value=_record(id=NEW_ID, supersedes=OLD_ID))
    svc.get_history = AsyncMock(return_value=MemoryHistory(
        items=[_record(id=OLD_ID), _record(id=NEW_ID)], current_id=NEW_ID,
    ))
    svc.retract = AsyncMock(return_value=True)
    svc.freeze = AsyncMock(return_value=_record(id=OLD_ID, frozen=True))
    svc.stale_list = AsyncMock(return_value=[_record(id=OLD_ID, confidence=0.2)])
    svc.cluster_list = AsyncMock(return_value={"ok": True, "clusters": []})
    svc.decay_confidence = AsyncMock(return_value={"default": 5})
    svc.mark_stale = AsyncMock(return_value=3)
    svc.gc_superseded = AsyncMock(
        return_value={"mode": "disabled", "purge_enabled": False, "selected": 2, "deleted": 0}
    )
    svc.orphans_cleanup = AsyncMock(return_value=1)
    svc.refresh_clusters = AsyncMock(return_value={"ok": True, "clusters": []})
    return svc


@pytest.fixture
def patch_lifecycle_service():
    svc = _task_service()
    with patch(
        "memory_server.tasks.memory_tasks._get_service", return_value=svc
    ), patch(
        "memory_server.tasks.lifecycle_tasks._get_service", return_value=svc
    ):
        yield svc


class TestMemoryTasksPhase2:
    def test_supersede_memory(self, patch_lifecycle_service):
        from memory_server.tasks.memory_tasks import supersede_memory

        result = supersede_memory(granule_id=OLD_ID, content="v2")
        assert result["id"] == NEW_ID
        patch_lifecycle_service.create_version.assert_awaited_once_with(
            granule_id=OLD_ID, new_content="v2", metadata_merge=None, importance=None
        )

    def test_supersede_memory_validation(self, patch_lifecycle_service):
        from memory_server.tasks.memory_tasks import supersede_memory
        from memory_server.tasks.errors import ValidationError

        with pytest.raises(ValidationError):
            supersede_memory(granule_id=OLD_ID, content=" ")

    def test_get_memory_history(self, patch_lifecycle_service):
        from memory_server.tasks.memory_tasks import get_memory_history

        result = get_memory_history(granule_id=OLD_ID)
        assert result["current_id"] == NEW_ID
        assert [r["id"] for r in result["items"]] == [OLD_ID, NEW_ID]

    def test_archive_memory_uses_retract(self, patch_lifecycle_service):
        from memory_server.tasks.memory_tasks import archive_memory

        assert archive_memory(memory_id=OLD_ID) == {"success": True}
        patch_lifecycle_service.retract.assert_awaited_once_with(memory_id=OLD_ID)

    def test_freeze_memory(self, patch_lifecycle_service):
        from memory_server.tasks.memory_tasks import freeze_memory

        result = freeze_memory(granule_id=OLD_ID, frozen=True)
        assert result["frozen"] is True

    def test_stale_list(self, patch_lifecycle_service):
        from memory_server.tasks.memory_tasks import stale_list

        result = stale_list(namespace="code_knowledge")
        assert result[0]["id"] == OLD_ID
        patch_lifecycle_service.stale_list.assert_awaited_once_with(
            user_id=None, namespace="code_knowledge", project_id=None, limit=100
        )

    def test_cluster_list(self, patch_lifecycle_service):
        from memory_server.tasks.memory_tasks import cluster_list

        result = cluster_list(namespace="code_knowledge")
        assert result["ok"] is True


class TestLifecycleTasks:
    def test_confidence_decay(self, patch_lifecycle_service):
        from memory_server.tasks.lifecycle_tasks import confidence_decay

        result = confidence_decay()
        assert result == {"touched": {"default": 5}, "total": 5}

    def test_mark_stale(self, patch_lifecycle_service):
        from memory_server.tasks.lifecycle_tasks import mark_stale

        assert mark_stale() == {"stale_candidates": 3}

    def test_gc_superseded(self, patch_lifecycle_service):
        from memory_server.tasks.lifecycle_tasks import gc_superseded

        assert gc_superseded() == {
            "mode": "disabled", "purge_enabled": False, "selected": 2, "deleted": 0,
        }

    def test_orphans_cleanup(self, patch_lifecycle_service):
        from memory_server.tasks.lifecycle_tasks import orphans_cleanup

        assert orphans_cleanup() == {"orphan_relations_removed": 1}

    def test_refresh_clusters_single_namespace(self, patch_lifecycle_service):
        from memory_server.tasks.lifecycle_tasks import refresh_clusters

        result = refresh_clusters(namespace="code_knowledge")
        assert result["ok"] is True
        patch_lifecycle_service.refresh_clusters.assert_awaited_once_with(
            namespace="code_knowledge"
        )

    def test_refresh_clusters_all_namespaces(self, patch_lifecycle_service):
        from memory_server.tasks.lifecycle_tasks import refresh_clusters

        ns_repo = MagicMock()
        ns_repo.list_all = AsyncMock(
            return_value=[MagicMock(uid="default"), MagicMock(uid="code_knowledge")]
        )
        patch_lifecycle_service.ns_repo = ns_repo

        result = refresh_clusters()

        assert result["ok"] is True
        assert set(result["namespaces"].keys()) == {"default", "code_knowledge"}
        assert patch_lifecycle_service.refresh_clusters.await_count == 2

    def test_refresh_clusters_all_graceful_on_pending(self, patch_lifecycle_service):
        from memory_server.tasks.lifecycle_tasks import refresh_clusters

        ns_repo = MagicMock()
        ns_repo.list_all = AsyncMock(
            return_value=[MagicMock(uid="default"), MagicMock(uid="code_knowledge")]
        )
        patch_lifecycle_service.ns_repo = ns_repo
        patch_lifecycle_service.refresh_clusters = AsyncMock(
            return_value={"ok": False, "reason": "migration 022 pending", "clusters": []}
        )

        result = refresh_clusters()

        assert result["ok"] is False
        assert result["reason"] == "migration 022 pending"
        # первый же ns сообщил об отсутствии хранимки — остальные не дёргаем
        assert patch_lifecycle_service.refresh_clusters.await_count == 1

    def test_refresh_clusters_all_graceful_on_qdrant_down(self, patch_lifecycle_service):
        """Граничный: Qdrant недоступен — beat не падает, обход останавливается
        на первом ns (retry подхватит весь прогон)."""
        from memory_server.tasks.lifecycle_tasks import refresh_clusters

        ns_repo = MagicMock()
        ns_repo.list_all = AsyncMock(
            return_value=[MagicMock(uid="default"), MagicMock(uid="code_knowledge")]
        )
        patch_lifecycle_service.ns_repo = ns_repo
        patch_lifecycle_service.refresh_clusters = AsyncMock(
            return_value={"ok": False, "reason": "qdrant_unavailable", "clusters": []}
        )

        result = refresh_clusters()

        assert result["ok"] is False
        assert result["reason"] == "qdrant_unavailable"
        assert patch_lifecycle_service.refresh_clusters.await_count == 1


class TestBeatSchedule:
    def test_lifecycle_entries_registered(self):
        from memory_server.celery_app import app
        from celery.schedules import crontab

        schedule = app.conf.beat_schedule
        tasks = {entry["task"] for entry in schedule.values()}
        lifecycle = "memory_server.tasks.lifecycle_tasks."
        for name in ("refresh_clusters", "confidence_decay", "mark_stale",
                     "gc_superseded", "orphans_cleanup"):
            assert lifecycle + name in tasks

        assert schedule["refresh-clusters"]["schedule"] == crontab(hour=2, minute=0)
        assert schedule["confidence-decay"]["schedule"] == crontab(hour=3, minute=0)
        assert schedule["mark-stale"]["schedule"] == crontab(hour=4, minute=0)
        assert schedule["gc-superseded"]["schedule"] == crontab(
            day_of_week="sun", hour=5, minute=0
        )
        assert schedule["orphans-cleanup"]["schedule"] == crontab(
            day_of_week="sun", hour=5, minute=30
        )

    def test_lifecycle_tasks_routed_to_memory_queue(self):
        from memory_server.celery_app import app

        routes = app.conf.task_routes
        assert routes["memory_server.tasks.lifecycle_tasks.*"] == {"queue": "memory"}


# ══════════════════════════════════════════════════════════════════
# MCP tools (мок celery_call)
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_celery_call():
    with patch(
        "memory_server.tools.memory_tools.celery_call", new_callable=AsyncMock
    ) as m:
        yield m


class TestLifecycleTools:
    @pytest.mark.asyncio
    async def test_memory_supersede(self, mock_celery_call):
        from memory_server.tools.memory_tools import memory_supersede

        mock_celery_call.return_value = {"id": NEW_ID}
        result = await memory_supersede(granule_id=OLD_ID, content="v2")

        assert result == {"id": NEW_ID}
        mock_celery_call.assert_awaited_once_with(
            "memory_server.tasks.memory_tasks.supersede_memory",
            granule_id=OLD_ID,
            content="v2",
            metadata=None,
            importance=None,
        )

    @pytest.mark.asyncio
    async def test_memory_supersede_coerces_metadata(self, mock_celery_call):
        from memory_server.tools.memory_tools import memory_supersede

        await memory_supersede(granule_id=OLD_ID, content="v2", metadata='{"x": 1}')

        assert mock_celery_call.await_args.kwargs["metadata"] == {"x": 1}

    @pytest.mark.asyncio
    async def test_memory_get_history(self, mock_celery_call):
        from memory_server.tools.memory_tools import memory_get_history

        mock_celery_call.return_value = {"items": [], "current_id": NEW_ID}
        result = await memory_get_history(granule_id=OLD_ID)

        assert result["current_id"] == NEW_ID
        mock_celery_call.assert_awaited_once_with(
            "memory_server.tasks.memory_tasks.get_memory_history",
            granule_id=OLD_ID,
        )

    @pytest.mark.asyncio
    async def test_memory_freeze(self, mock_celery_call):
        from memory_server.tools.memory_tools import memory_freeze

        await memory_freeze(granule_id=OLD_ID, frozen=True)

        mock_celery_call.assert_awaited_once_with(
            "memory_server.tasks.memory_tasks.freeze_memory",
            granule_id=OLD_ID,
            frozen=True,
        )

    @pytest.mark.asyncio
    async def test_memory_stale_list(self, mock_celery_call):
        from memory_server.tools.memory_tools import memory_stale_list

        mock_celery_call.return_value = []
        await memory_stale_list(namespace="code_knowledge", limit=10)

        mock_celery_call.assert_awaited_once_with(
            "memory_server.tasks.memory_tasks.stale_list",
            user_id=None,
            namespace="code_knowledge",
            project_id=None,
            limit=10,
        )

    @pytest.mark.asyncio
    async def test_memory_cluster_list(self, mock_celery_call):
        from memory_server.tools.memory_tools import memory_cluster_list

        mock_celery_call.return_value = {"ok": False, "reason": "migration 022 pending"}
        result = await memory_cluster_list(namespace="code_knowledge")

        assert result["ok"] is False
        mock_celery_call.assert_awaited_once_with(
            "memory_server.tasks.memory_tasks.cluster_list",
            namespace="code_knowledge",
            project_id=None,
        )
