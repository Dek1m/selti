"""Memory V3 (ADR-019), фазы V3.0/V3.1 — контракты и SQL-инварианты.

Честный контракт записи (V3.0): резолв имён только по актуальным,
immutable-content, confirm-семантика exact-dedup. Наследование графа
(V3.1): REWIRE рёбер, cluster_id, time-travel traverse, GC-стоп-кран,
миграция 023. Живой БД нет — SQL-контракты проверяются инвариантами
на текстах констант/миграции (паттерн TestGetHistorySqlSemantics).
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.models import MemoryRecord, Relation

MIGRATION_023 = Path(__file__).parent.parent / "migrations" / "023_memory_v3_core.sql"


# ══════════════════════════════════════════════════════════════════
# V3.0 — резолв имён (дыра 5)
# ══════════════════════════════════════════════════════════════════


class TestEntityNameResolve:
    def test_sql_filters_to_asserted(self):
        """Без фильтра LIMIT 1 цеплял трупа — ребро прилипало к superseded."""
        assert "m.status = 'asserted'" in q.SELECT_MEMORY_BY_ENTITY_NAME
        assert "m.valid_to IS NULL" in q.SELECT_MEMORY_BY_ENTITY_NAME

    def test_sql_order_is_deterministic(self):
        """LIMIT 1 без ORDER BY — недетерминизм; теперь взвешенная
        детерминированная сортировка: вечный → важнее → свежее по
        доступу → свежее по созданию."""
        sql = q.SELECT_MEMORY_BY_ENTITY_NAME
        assert re.search(
            r"ORDER BY\s+m\.frozen\s+DESC,\s*"
            r"m\.importance\s+DESC,\s*"
            r"m\.last_accessed_at\s+DESC\s+NULLS\s+LAST,\s*"
            r"m\.created_at\s+DESC",
            sql,
        ), "порядок обязан быть frozen→importance→last_accessed_at→created_at"
        assert "LIMIT 1" in sql

    @pytest.mark.asyncio
    async def test_repository_uses_fixed_sql(self, mock_pool):
        pg = PostgreSQLRepository(pool=mock_pool)
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        from tests.conftest import memory_row

        conn.fetchrow = AsyncMock(return_value=memory_row(id="mem-live"))

        record = await pg.find_by_entity_name("selti")

        assert isinstance(record, MemoryRecord)
        assert conn.fetchrow.await_args.args == (
            q.SELECT_MEMORY_BY_ENTITY_NAME, "selti",
        )


# ══════════════════════════════════════════════════════════════════
# V3.1 — REWIRE: SQL-инварианты переноса рёбер (дыра 1)
# ══════════════════════════════════════════════════════════════════


class TestRewireSqlInvariants:
    def test_supersedes_link_never_moved(self):
        """'supersedes' — структурная связь версий, не наследуемое знание."""
        assert "r.link_type <> 'supersedes'" in q.REWIRE_RELATIONS_SOURCE
        assert "r.link_type <> 'supersedes'" in q.REWIRE_RELATIONS_TARGET

    def test_source_rewire_requires_live_target(self):
        """Рёбра с мёртвой второй стороной остаются на старой (история).

        Висячий конец (target_id IS NULL, soft-resolve) переносится — его
        сторона не мёртвая, а неизвестная.
        """
        sql = q.REWIRE_RELATIONS_SOURCE
        assert "(r.target_id IS NULL OR EXISTS (" in sql
        assert re.search(
            r"m\.id = r\.target_id\s+AND\s+m\.status = 'asserted' AND m\.valid_to IS NULL",
            sql,
        )

    def test_target_rewire_requires_live_source(self):
        sql = q.REWIRE_RELATIONS_TARGET
        assert re.search(
            r"m\.id = r\.source_id\s+AND\s+m\.status = 'asserted' AND m\.valid_to IS NULL",
            sql,
        )
        # у source FK NOT NULL — NULL-ветки быть не должно
        assert "r.source_id IS NULL OR" not in sql

    def test_duplicate_edge_not_created(self):
        """Идентичное ребро (пара + тип) уже на новой — UPDATE не трогает
        (иначе unique-тройка (source, target, type) абортирует транзакцию)."""
        assert "NOT EXISTS (" in q.REWIRE_RELATIONS_SOURCE
        assert "NOT EXISTS (" in q.REWIRE_RELATIONS_TARGET
        assert "IS NOT DISTINCT FROM r.target_id" in q.REWIRE_RELATIONS_SOURCE

    def test_inherited_from_stamped_on_move(self):
        """Метка происхождения = old.id — только при переносе."""
        assert "inherited_from = $1::uuid" in q.REWIRE_RELATIONS_SOURCE
        assert "inherited_from = $1::uuid" in q.REWIRE_RELATIONS_TARGET

    def test_insert_relation_leaves_inherited_null(self):
        """Новые рёбра создаются без происхождения (NULL = не переносилось)."""
        assert "inherited_from" not in q.INSERT_RELATION


class TestRelationModel:
    def test_inherited_from_field_defaults_none(self):
        rel = Relation(id="r1", source_id="a", target_id="b", link_type="related_to")
        assert rel.inherited_from is None

    def test_to_relation_maps_inherited_from(self):
        """Строки выборок с колонкой происхождения → поле модели; старые
        моки без колонки — NULL (guard)."""
        row = {
            "id": "r1", "source_id": "a", "target_id": "b", "target_name": None,
            "link_type": "related_to", "description": None, "weight": 1.0,
            "metadata": {}, "inherited_from": "old-version", "created_at": None,
        }
        rel = PostgreSQLRepository._to_relation(row)
        assert rel.inherited_from == "old-version"

        legacy_row = dict(row)
        del legacy_row["inherited_from"]
        assert PostgreSQLRepository._to_relation(legacy_row).inherited_from is None


# ══════════════════════════════════════════════════════════════════
# V3.1 — миграция 023 (traverse v3 / inherited_from / backfill A)
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def migration_023_sql() -> str:
    assert MIGRATION_023.exists(), "023_memory_v3_core.sql отсутствует"
    return MIGRATION_023.read_text(encoding="utf-8")


class TestMigration023:
    # все тесты берут module-scoped migration_023_sql напрямую
    @pytest.fixture
    def sql(self, migration_023_sql) -> str:
        return migration_023_sql

    def test_drops_old_traverse_signature(self, sql):
        assert "DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[]);" in sql

    def test_traverse_v3_defaults_to_now(self, sql):
        """Дефолт now() = фильтр актуальности (asserted only) — фикс дыры 4;
        вызовы с тремя аргументами (queries.TRAVERSE_FULL) совместимы."""
        assert re.search(
            r"p_as_of\s+TIMESTAMPTZ\s+DEFAULT\s+now\(\)", sql
        )

    def test_traverse_v3_windows_nodes_by_as_of(self, sql):
        """Нода входит, если окно валидности покрывает as_of — в якоре и в
        шаге обхода; сборка нод отдельного фильтра не требует: uniq_nodes
        наполняется только нодами, уже прошедшими окно при входе."""
        window = re.compile(
            r"m\.valid_from <= p_as_of\s+AND\s+\(m\.valid_to IS NULL OR m\.valid_to > p_as_of\)"
        )
        assert len(window.findall(sql)) == 2, "окно нужно ровно в якоре и шаге"

    def test_traverse_v3_projects_inherited_edges(self, sql):
        """Ребро, перенесённое после as_of, остаётся на inherited_from-версии:
        время привязки = valid_from физического конца (UPDATE-перенос
        created_at ребра не меняет), поэтому формула — через valid_from."""
        assert "r.inherited_from IS NOT NULL AND src.valid_from > p_as_of" in sql
        assert "r.inherited_from IS NOT NULL AND tgt.valid_from > p_as_of" in sql

    def test_traverse_v3_walks_physical_and_inherited(self, sql):
        """Ребро ноды на as_of: физические (source_id = нода) ∪ унаследованные
        (inherited_from = нода, физически на наследнике) — LATERAL."""
        assert "r.source_id = gw.node_id OR r.inherited_from = gw.node_id" in sql

    def test_superseded_by_index_partial(self, sql):
        assert (
            "CREATE INDEX IF NOT EXISTS idx_memories_superseded_by" in sql
            and "WHERE superseded_by IS NOT NULL" in sql
        )

    def test_relations_inherited_from_column(self, sql):
        assert "ALTER TABLE relations ADD COLUMN IF NOT EXISTS inherited_from UUID" in sql
        assert "FOREIGN KEY (inherited_from) REFERENCES memories(id)" in sql
        # труп удалён будущим GC — перенесённое ребро живёт (источник теряется)
        assert "ON DELETE SET NULL" in sql
        assert "idx_relations_inherited_from" in sql

    def test_backfill_a_rewires_with_same_rules(self, sql):
        """Backfill = те же правила runtime-REWIRE: живая вторая сторона,
        supersedes не трогаем, дубликаты не создаём, отчёт в лог."""
        assert "SET source_id = s.superseded_by" in sql
        assert "SET target_id = s.superseded_by" in sql
        assert sql.count("s.status = 'superseded' AND s.superseded_by IS NOT NULL") == 2
        assert sql.count("r.link_type <> 'supersedes'") >= 2
        assert "RAISE NOTICE" in sql
        # без курсоров — set-based
        assert "FOR row IN" not in sql and "LOOP\n        -- обход пар" not in sql

    def test_backfill_a_inherits_cluster_id(self, sql):
        assert re.search(
            r"UPDATE memories nw\s+SET cluster_id = old\.cluster_id", sql
        )

    def test_comments_registered(self, sql):
        assert "COMMENT ON COLUMN relations.inherited_from" in sql
        assert "COMMENT ON FUNCTION graph_traverse_full" in sql


# ══════════════════════════════════════════════════════════════════
# V3.1 — GC-стоп-кран: конфиг
# ══════════════════════════════════════════════════════════════════


class TestGcConfig:
    def test_defaults_disable_purge(self):
        """Дефолт: прод не удаляет ничего — поведение не меняется, мина
        FK (дыра 7) обезврежена."""
        config = Settings()
        assert config.gc_purge_enabled is False
        assert config.gc_mode == "disabled"

    def test_old_dry_run_flag_removed(self):
        assert not hasattr(Settings(), "gc_dry_run")


# ══════════════════════════════════════════════════════════════════
# V3.0 — задача update_memory: честный ответ о версии
# ══════════════════════════════════════════════════════════════════


class TestUpdateMemoryTaskContract:
    def test_content_marks_versioned(self):
        from memory_server.tasks.memory_tasks import update_memory

        record = MagicMock()
        record.model_dump.return_value = {
            "id": "mem-new", "supersedes": "mem-1", "content": "v2",
        }
        with patch(
            "memory_server.tasks.memory_tasks._get_service",
            return_value=MagicMock(update=AsyncMock(return_value=record)),
        ):
            result = update_memory(memory_id="mem-1", content="v2")

        assert result["versioned"] is True
        assert result["previous_id"] == "mem-1"
        assert result["id"] == "mem-new"
        assert result["supersedes"] == "mem-1"  # новый id + старый id — в записи

    def test_wrapper_patch_keeps_old_response_shape(self):
        """Без content — ответ без новых полей: старые клиенты не меняются."""
        from memory_server.tasks.memory_tasks import update_memory

        record = MagicMock()
        record.model_dump.return_value = {"id": "mem-1", "importance": 5}
        with patch(
            "memory_server.tasks.memory_tasks._get_service",
            return_value=MagicMock(update=AsyncMock(return_value=record)),
        ):
            result = update_memory(memory_id="mem-1", importance=5)

        assert "versioned" not in result
        assert result == {"id": "mem-1", "importance": 5}
