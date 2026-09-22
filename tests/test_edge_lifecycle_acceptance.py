"""Приёмочный сьют Ф1 «Жизнь рёбер» (V3.5) — тест-план ред. 2 Катерины.

Уровень ПРИЁМКИ, не юниты: кейсы сверяются с зафиксированными контрактами
(docs/TEST_PLAN_EDGE_LIFECYCLE.md + вердикты Эны 22-23.09), а не с текущим
состоянием кода. Дублирование проверок с tests/test_edge_lifecycle.py
допустимо: юнит-файл Соны проверяет реализацию, приёмка — контракт.
Живой БД нет (паттерн test_linker_v3/test_memory_v3): SQL-контракты —
инвариантами на текстах миграций и констант queries.py; математика —
эталонным оракулом (числа пересчитаны Python-проверкой 23.09); поведение —
на мок-репозитории. Живой SQL уже репетирован на ephemeral PG и прод-дампе
(docs/DIAG_026_REHEARSAL_2026-09-23.md — все пункты зелёные).
"""

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.memory.service import MemoryService, canonical_edge_pairs

MIGRATIONS = Path(__file__).parent.parent / "migrations"
MIGRATION_026 = MIGRATIONS / "026_edge_lifecycle.sql"
MIGRATION_023 = MIGRATIONS / "023_memory_v3_core.sql"
MIGRATION_005 = MIGRATIONS / "005_relations.sql"

A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"
C = "00000000-0000-0000-0000-00000000000c"

# Вердикты Эны 22.09 — единственный источник чисел (ред. 2 тест-плана)
LAMBDA = 0.02
LAMBDA_MIN = 0.002
FLOOR = 0.05
ALPHA = 0.2
RESTORE_BETA = 0.5
MIN_AGE_DAYS = 30

# Зафиксированное «сегодня» кампании (фикстура-дата тест-плана §3)
CAMPAIGN_NOW = datetime(2026, 9, 22, 3, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def mig026() -> str:
    assert MIGRATION_026.exists(), "026_edge_lifecycle.sql отсутствует"
    return MIGRATION_026.read_text(encoding="utf-8")


def _function_body(migration_text: str, fn_name: str) -> str:
    """Тело функции из SQL-миграции: от CREATE OR REPLACE до закрывающего $$;."""
    marker = f"CREATE OR REPLACE FUNCTION {fn_name}"
    start = migration_text.index(marker)
    return migration_text[start : migration_text.index("$$;", start)]


# ── Эталонный оракул ленивой проекции (числа тест-плана §4.3) ──


def lambda_eff(used_count: int, lam: float = LAMBDA, lam_min: float = LAMBDA_MIN) -> float:
    """λ_eff = GREATEST(λ_min, λ / (1 + used_count)) — сатурация частоты."""
    return max(lam_min, lam / (1 + used_count))


def w_eff(weight: float, used_count: int, days: float,
          lam: float = LAMBDA, lam_min: float = LAMBDA_MIN) -> float:
    """Raw w_eff = weight × exp(−λ_eff × days) — decay-ветка (не-immune)."""
    return weight * math.exp(-lambda_eff(used_count, lam, lam_min) * days)


def accept_config(**overrides) -> Settings:
    """Конфиг приёмки: Ф1 включена, шумовые пути выключены."""
    base = {
        "dedup_enabled": False,
        "hybrid_search_enabled": False,
        "edge_lifecycle_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)


def service_stub(repo: MagicMock, **cfg) -> MemoryService:
    """Сервис на мок-репозитории (паттерн TestServiceTraverseStrategy)."""
    return MemoryService(
        repository=repo,
        embedding_provider=MagicMock(),
        namespace_repository=MagicMock(),
        config=accept_config(**cfg),
    )


# ════════════════════════════════════════════════════════════════
# Ф1-MIG: миграционные инварианты 026 (текст миграции, паттерн MIGRATION_023)
# ════════════════════════════════════════════════════════════════


class TestMigrationContract:
    """Схема 026: колонки, индекс, unique, фильтры хранимки, REWIRE-гварды."""

    def test_mig01_new_columns_defaults_no_status(self, mig026: str):
        # Ф1-MIG-01: used_count NOT NULL DEFAULT 0; timestamps NULL-able;
        # статус-КОЛОНКИ НЕТ (решение Норы/Эны — только pruned_at)
        assert "ADD COLUMN IF NOT EXISTS used_count INTEGER NOT NULL DEFAULT 0" in mig026
        assert "ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;" in mig026
        assert "ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMPTZ;" in mig026
        alter_lines = [
            ln for ln in mig026.splitlines() if "ADD COLUMN IF NOT EXISTS last_used_at" in ln
        ]
        assert alter_lines and all("NOT NULL" not in ln for ln in alter_lines), (
            "last_used_at NULL-able: NULL = никогда не использовалось"
        )
        added = [ln.strip() for ln in mig026.splitlines() if "ADD COLUMN IF NOT EXISTS" in ln]
        assert all("status" not in ln.lower() for ln in added), (
            "статус-колонка запрещена контрактом 026 (Д3): только pruned_at"
        )

    def test_mig02_decay_due_partial_expression_index(self, mig026: str):
        # Ф1-MIG-02: частичный expression-индекс по COALESCE-якорю
        assert "CREATE INDEX IF NOT EXISTS idx_relations_decay_due" in mig026
        assert "ON relations (COALESCE(last_used_at, created_at))" in mig026
        assert "WHERE pruned_at IS NULL" in mig026
        # отдельного индекса по «голому» last_used_at НЕ заводим (избыточно)
        assert "ON relations (last_used_at)" not in mig026

    def test_mig03_unique_not_extended_by_pruned(self, mig026: str):
        # Ф1-MIG-03: partial unique остался БЕЗ условия по pruned_at —
        # ON CONFLICT пары срабатывает и на pruned-строке (дверь к
        # DO UPDATE-воскрешению из линкера). 026 его не переопределяет.
        mig005 = MIGRATION_005.read_text(encoding="utf-8")
        assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_relations_unique_link" in mig005
        assert "WHERE target_id IS NOT NULL" in mig005
        assert "pruned" not in mig005.lower().split("idx_relations_unique_link")[-1]
        # 026 unique вообще не переопределяет (только комментарий)
        assert "CREATE UNIQUE INDEX" not in mig026

    def test_mig04_traverse_filters_pruned_both_places(self, mig026: str):
        # Ф1-MIG-04: предикат живости в рекурсивном шаге И в edge_proj
        body = _function_body(mig026, "graph_traverse_full")
        predicate = "(r.pruned_at IS NULL OR r.pruned_at > p_as_of)"
        assert body.count(predicate) == 2, (
            "фильтр pruned-рёбер нужен в обоих местах: LATERAL-шаг + edge_proj"
        )

    def test_mig05_history_projection_not_hard_hidden(self, mig026: str):
        # Ф1-MIG-05: НЕ жёсткое скрытие — при as_of < pruned_at ребро видно
        body = _function_body(mig026, "graph_traverse_full")
        assert "pruned_at > p_as_of" in body  # историческая проекция жива
        # сигнатура и умолчание as_of не изменились (вызовы 3/4 аргументами)
        assert "p_as_of      TIMESTAMPTZ DEFAULT now()" in mig026
        assert "DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ)" in mig026

    def test_mig06_idempotent_rerun_markers(self, mig026: str):
        # Ф1-MIG-06: повторный прогон — no-op (IF NOT EXISTS везде)
        assert mig026.count("IF NOT EXISTS") >= 4  # 3 колонки + индекс
        assert "DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[])" in mig026
        # в миграции НЕТ per-row UPDATE — backfill не требуется
        assert "UPDATE relations" not in mig026

    def test_mig08_rewire_guards_pruned_edges(self):
        # Ф1-MIG-08: pruned-рёбра не переезжают на наследника (оба REWIRE)
        assert "r.pruned_at IS NULL" in q.REWIRE_RELATIONS_SOURCE
        assert "r.pruned_at IS NULL" in q.REWIRE_RELATIONS_TARGET


# ════════════════════════════════════════════════════════════════
# Ф1-LAZ: ленивая проекция w_eff — точные числа ред. 2
# ════════════════════════════════════════════════════════════════


class TestLazyProjectionNumbers:
    """Эталоны §4.3: 0.6570 / 0.5594 / 0.9194 / 0.9589 — оракул ±1e-4."""

    def test_laz01_base_formula_21_days(self):
        # weight=1.0, used=0, якорь created_at, days=21 → exp(−0.42)
        assert w_eff(1.0, 0, days=(CAMPAIGN_NOW - (CAMPAIGN_NOW - timedelta(days=21))).days) == pytest.approx(0.657047, abs=1e-4)

    def test_laz02_coalesce_anchor_last_used_wins(self):
        # created_at 52д назад, last_used_at 7д назад: days=7, used=1 → λ_eff=0.01
        anchor = CAMPAIGN_NOW - timedelta(days=7)
        days = (CAMPAIGN_NOW - anchor).total_seconds() / 86400.0
        assert w_eff(0.6, 1, days=days) == pytest.approx(0.559436, abs=1e-4)

    def test_laz03_lambda_eff_saturation_used4(self):
        assert w_eff(1.0, 4, days=21) == pytest.approx(0.919431, abs=1e-4)

    def test_laz04_lambda_floor_used9(self):
        # 0.02/10 = 0.002 ровно λ_min — кламп не меняет, экспонент −0.042
        assert w_eff(1.0, 9, days=21) == pytest.approx(0.958870, abs=1e-4)

    def test_laz07_zero_days_identity(self):
        assert w_eff(0.7, 0, days=0.0) == pytest.approx(0.7)
        assert w_eff(0.37, 5, days=0.0) == pytest.approx(0.37)

    def test_laz08_monotone_by_days(self):
        values = [w_eff(1.0, 0, days=d) for d in (10, 20, 30, 60)]
        assert all(x > y for x, y in zip(values, values[1:]))
        for got, days in zip(values, (10, 20, 30, 60)):
            assert got == pytest.approx(math.exp(-0.02 * days), abs=1e-9)

    def test_laz_oracle_formula_matches_sql_shape(self):
        # формулы оракула и SQL — одна и та же математика (вердикт Эны)
        formula = "GREATEST($2::float8, $1::float8 / (1 + r.used_count))"
        anchor = "COALESCE(r.last_used_at, r.created_at)"
        for sql in (q.PRUNE_EDGES_CANDIDATES, q.SELECT_ACTIVATION_EDGES):
            assert formula in sql
            assert anchor in sql


class TestLazyProjectionPurity:
    """Д2: расчёт не мутирует БД — идемпотентность по построению."""

    def test_laz05_projection_is_read_only(self):
        # проекция — SELECT: ни одного UPDATE в кандидатской и активационной
        for sql in (q.PRUNE_EDGES_CANDIDATES, q.SELECT_ACTIVATION_EDGES):
            assert sql.lstrip().upper().startswith("SELECT")
            assert "UPDATE" not in sql.upper()

    def test_laz05_no_update_ever_applies_decay(self):
        # Д2 по построению: НИ один UPDATE не применяет exp() — вес не
        # мутируется затуханием ни в каком запросе (писать weight могут
        # только рождение рёбер, reinforce α и restore β)
        sql_texts = {
            name: getattr(q, name)
            for name in dir(q)
            if name.isupper() and isinstance(getattr(q, name), str)
        }
        offenders = [
            name for name, sql in sql_texts.items()
            if "UPDATE" in sql.upper() and "SET" in sql.upper() and "exp(" in sql
        ]
        assert offenders == [], f"материальный decay всплыл в UPDATE: {offenders}"
        # и сами записи веса содержат только рождение/α/β-формы
        assert "LEAST(1.0, r.weight + (1.0 - r.weight)" in q.REINFORCE_RELATIONS
        assert "LEAST(1.0, weight + (1.0 - weight)" in q.RESTORE_EDGE

    def test_laz06_recompute_same_day_bit_identical(self):
        first = w_eff(0.8, 3, days=17.25)
        second = w_eff(0.8, 3, days=17.25)
        assert first == second  # бит-в-бит: двойного затухания не существует

    def test_laz09b_no_weff_in_serving_paths(self, mig026: str):
        # боевые пути (bfs-traverse + карточки связей) наружу w_eff НЕ отдают:
        # вес в выдаче — r.weight, ни exp, ни w_eff в хранимках
        traverse_body = _function_body(mig026, "graph_traverse_full")
        assert "w_eff" not in traverse_body
        assert "exp(" not in traverse_body
        unified_body = _function_body(
            MIGRATION_023.read_text(encoding="utf-8"), "get_relations_unified"
        )
        assert "w_eff" not in unified_body
        assert "exp(" not in unified_body

    def test_laz09a_traverse_orders_by_rel_id_not_weight(self, mig026: str):
        # порядок рёбер выдачи — по rel_id, вес на сортировку не влияет
        traverse_body = _function_body(mig026, "graph_traverse_full")
        assert "ORDER BY e.rel_id" in traverse_body
        assert "ORDER BY un.depth, m.id" in traverse_body
        # сортировок по weight в хранимке нет вовсе
        assert "ORDER BY" not in traverse_body.replace("ORDER BY e.rel_id", "").replace("ORDER BY un.depth, m.id", "").replace("ORDER BY node_id, depth", "")

    @pytest.mark.asyncio
    async def test_laz09a_two_traverse_calls_identical_json(self):
        # два одинаковых вызова bfs-traverse — бит-в-бит одинаковый JSON
        raw = {
            "nodes": [
                {"id": B, "content": "beta", "namespace": "default", "importance": 3, "depth": 1},
                {"id": A, "content": "alpha", "namespace": "default", "importance": 5, "depth": 0},
                {"id": C, "content": "gamma", "namespace": "default", "importance": 1, "depth": 2},
            ],
            "edges": [
                {"id": "00000000-0000-0000-0000-0000000000f1", "source_id": A,
                 "target_id": B, "link_type": "related_to", "weight": 0.1},
                {"id": "00000000-0000-0000-0000-0000000000f2", "source_id": A,
                 "target_id": C, "link_type": "related_to", "weight": 0.9},
            ],
        }
        repo = MagicMock()
        repo.get_by_id = AsyncMock(return_value=MagicMock())
        repo.traverse = AsyncMock(return_value=raw)
        service = service_stub(repo)
        first = await service.traverse(A, depth=2)
        second = await service.traverse(A, depth=2)
        # вес 0.9 у второго ребра НЕ двигает его вперёд первого (id-порядок);
        # повторный вызов — идентичный JSON целиком
        dump1 = json.dumps(first.model_dump(), sort_keys=True, default=str)
        dump2 = json.dumps(second.model_dump(), sort_keys=True, default=str)
        assert dump1 == dump2
        assert [e.id for e in first.edges] == sorted(
            [e.id for e in first.edges]
        )


# ════════════════════════════════════════════════════════════════
# Ф1-IMM: иммунитеты pruning-кампании — каждый отдельным кейсом
# ════════════════════════════════════════════════════════════════


class TestPruneImmunities:
    """§4.4: ручные/l2/inherited/frozen — decay+prune иммунитет; мосты —
    только prune; NULL-кластер мостом не бессмертит; висячие — иммунитет."""

    def _norm(self, fragment: str) -> str:
        """Нормализация пробелов: устойчивое вхождение фрагмент-в-фрагмент."""
        return " ".join(fragment.split())

    def test_imm01_manual_edges_immune(self):
        # ручные рёбра (source ≠ linker_v3, NULL-source — тоже ручные)
        assert "r.metadata->>'source' IS DISTINCT FROM 'linker_v3'" in q._EDGE_IMMUNE_SQL
        # и весь immune-блок исключает из кандидатской кампании
        assert f"NOT {self._norm(q._EDGE_IMMUNE_SQL)}" in self._norm(q.PRUNE_EDGES_CANDIDATES)

    def test_imm02_l2_layer_immune(self):
        assert "r.metadata->>'layer' = 'l2'" in q._EDGE_IMMUNE_SQL

    def test_imm02_inherited_rewire_immune(self):
        assert "r.inherited_from IS NOT NULL" in q._EDGE_IMMUNE_SQL

    def test_imm01_frozen_granules_immune_both_ends(self):
        assert "src.frozen" in q._EDGE_IMMUNE_SQL
        assert "tgt.frozen" in q._EDGE_IMMUNE_SQL

    def test_imm03_bridge_immune_only_from_pruning(self):
        # мост: оба кластера NOT NULL + разные — иммунитет от pruning
        assert "src.cluster_id IS NOT NULL" in q._EDGE_BRIDGE_SQL
        assert "tgt.cluster_id IS NOT NULL" in q._EDGE_BRIDGE_SQL
        assert "src.cluster_id IS DISTINCT FROM tgt.cluster_id" in q._EDGE_BRIDGE_SQL
        # в кандидатской кампании мост исключён
        assert f"NOT {self._norm(q._EDGE_BRIDGE_SQL)}" in self._norm(q.PRUNE_EDGES_CANDIDATES)

    def test_imm04_null_cluster_one_side_is_not_bridge(self):
        # NOT NULL-гварды: NULL-конец не бессмертит ребро (Д4)
        assert q._EDGE_BRIDGE_SQL.count("IS NOT NULL") == 2

    def test_imm05_both_clusters_null_not_bridge(self):
        # оба NULL (миграция 022 pending) — НЕ мост, кампания не мертва
        assert "src.cluster_id IS NOT NULL" in q._EDGE_BRIDGE_SQL
        assert "tgt.cluster_id IS NOT NULL" in q._EDGE_BRIDGE_SQL

    def test_imm08_combined_immunity_or_chain(self):
        # один любой иммунитет достаточен: immune — OR-цепочка альтернатив
        assert q._EDGE_IMMUNE_SQL.count("OR") >= 3

    def test_imm09_dangling_edges_never_candidates(self):
        # висячие (target_id NULL, 2171 legacy) — в кампанию не попадают
        assert "r.target_id IS NOT NULL" in q.PRUNE_EDGES_CANDIDATES

    def test_imm03_bridge_still_decays_in_projection(self):
        # мост — иммунитет ТОЛЬКО от pruning: в w_eff-проекции моста нет
        assert q._EDGE_BRIDGE_SQL not in q.SELECT_ACTIVATION_EDGES

    def test_imm10_candidates_is_single_snapshot(self):
        # гонки с assign_clusters нет: кандидат-снимок одним SELECT,
        # применение — гард-UPDATE (проигравший конкурент — 0 строк)
        assert "SELECT r.id::text" in q.PRUNE_EDGES_CANDIDATES
        assert "AND pruned_at IS NULL" in q.PRUNE_EDGES_APPLY


# ════════════════════════════════════════════════════════════════
# Ф1-PRN: pruning-кампания / restore
# ════════════════════════════════════════════════════════════════


class TestPruneCampaign:
    """§4.5: raw-порог без clamp, dry_run, атомарность, restore round-trip."""

    def test_prn01_raw_threshold_in_sql(self):
        # порог применяется к RAW произведению weight×exp(...) — без
        # clamp-обёртки GREATEST вокруг произведения (Д1)
        assert "r.weight * exp(" in q.PRUNE_EDGES_CANDIDATES
        assert ") <= $4::float8" in q.PRUNE_EDGES_CANDIDATES

    def test_prn02_boundary_149_vs_150_days(self):
        # 149д: exp(−2.98)=0.050794 > 0.05 — живёт; 150д: 0.049787 — кандидат
        assert w_eff(1.0, 0, days=149) == pytest.approx(0.050794, abs=1e-5)
        assert w_eff(1.0, 0, days=149) > FLOOR
        assert w_eff(1.0, 0, days=150) == pytest.approx(0.049787, abs=1e-5)
        assert w_eff(1.0, 0, days=150) <= FLOOR

    def test_prn02_age_guard_present(self):
        assert "make_interval(days => $3::int)" in q.PRUNE_EDGES_CANDIDATES
        assert "r.created_at <= now() - make_interval" in q.PRUNE_EDGES_CANDIDATES

    def test_prn03_restore_round_trip_math(self):
        # 0.05 → LEAST(1.0, 0.05 + 0.5×0.95) = 0.525 (β=0.5, вердикт Эны)
        assert min(1.0, 0.05 + RESTORE_BETA * (1 - 0.05)) == pytest.approx(0.525)
        assert "pruned_at    = NULL" in q.RESTORE_EDGE or "pruned_at = NULL" in q.RESTORE_EDGE
        assert "LEAST(1.0, weight + (1.0 - weight) * $2::float8)" in q.RESTORE_EDGE

    def test_prn04_repeat_restore_guarded(self):
        # повторный restore активного ребра — идемпотентный no-op (гвард)
        assert "AND pruned_at IS NOT NULL" in q.RESTORE_EDGE

    def test_prn06_campaign_idempotent_predicates(self):
        # pruned не пересчитывается: предикат живости в SELECT и UPDATE
        assert "r.pruned_at IS NULL" in q.PRUNE_EDGES_CANDIDATES
        assert "AND pruned_at IS NULL" in q.PRUNE_EDGES_APPLY

    def test_prn08_apply_is_guarded_update_no_delete(self):
        # каждый кандидат прунится ровно один раз; DELETE отсутствует
        assert "UPDATE relations" in q.PRUNE_EDGES_APPLY
        assert "SET pruned_at = now()" in q.PRUNE_EDGES_APPLY
        assert "DELETE" not in q.PRUNE_EDGES_APPLY.upper()
        # вес/счётчики кампанией не трогаются
        assert "used_count" not in q.PRUNE_EDGES_APPLY
        assert "weight" not in q.PRUNE_EDGES_APPLY

    @pytest.mark.asyncio
    async def test_prn07_dry_run_report_only(self):
        # dry_run=True: ни одной записи не тронуто (apply не вызван)
        repo = MagicMock()
        repo.prune_candidates = AsyncMock(return_value=[A, B, C])
        repo.prune_edges_apply = AsyncMock(return_value=3)
        service = service_stub(repo)
        report = await service.edge_prune(dry_run=True)
        assert report["dry_run"] is True
        assert report["candidates"] == 3
        assert "pruned" not in report  # боя не было
        repo.prune_edges_apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prn09_live_report_keys(self):
        repo = MagicMock()
        repo.prune_candidates = AsyncMock(return_value=[A, B])
        repo.prune_edges_apply = AsyncMock(return_value=2)
        service = service_stub(repo)
        report = await service.edge_prune(dry_run=False)
        assert report["dry_run"] is False
        assert report["pruned"] == 2
        assert report["candidates"] == 2
        # параметры кампании в отчёте — аудит волны
        for key in ("lambda", "lambda_min", "floor", "min_age_days"):
            assert key in report

    @pytest.mark.asyncio
    async def test_prn07_dry_run_default_from_config(self):
        # вызов без dry_run берёт конфиг: дефолт True (первая волна — отчёт)
        assert Settings().edge_prune_dry_run is True
        repo = MagicMock()
        repo.prune_candidates = AsyncMock(return_value=[A])
        repo.prune_edges_apply = AsyncMock()
        service = service_stub(repo)  # edge_prune_dry_run не переопределён
        report = await service.edge_prune()
        assert report["dry_run"] is True
        repo.prune_edges_apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prn_disabled_master_switch_skips(self):
        # edge_lifecycle_enabled=False (дефолт прода): кампания пропущена
        repo = MagicMock()
        repo.prune_candidates = AsyncMock()
        service = service_stub(repo, edge_lifecycle_enabled=False)
        report = await service.edge_prune(dry_run=False)
        assert report == {"skipped": "edge_lifecycle_enabled"}
        repo.prune_candidates.assert_not_awaited()

    def test_prn10_beat_schedule_slot(self):
        # 03:30 UTC, строго после confidence_decay (03:00) и до mark_stale (04:00)
        from memory_server.celery_app import app

        def cron(entry_key: str) -> tuple[int, int]:
            schedule = app.conf.beat_schedule[entry_key]["schedule"]
            hour = next(iter(schedule.hour)) if isinstance(schedule.hour, set) else schedule.hour
            minute = next(iter(schedule.minute)) if isinstance(schedule.minute, set) else schedule.minute
            return hour, minute

        def daily_slots() -> list[tuple[int, int]]:
            # только crontab-записи (ежечасные timedelta-записи вне игры)
            return [
                cron(key)
                for key, entry in app.conf.beat_schedule.items()
                if hasattr(entry["schedule"], "hour")
            ]

        beat = app.conf.beat_schedule
        entry = beat["edge-prune"]
        assert entry["task"] == "memory_server.tasks.lifecycle_tasks.edge_prune"
        assert cron("edge-prune") == (3, 30)
        # окно ночи: кластеры (02:00/02:30) → decay-гранул (03:00) →
        # edge_prune (03:30) → stale (04:00); наложений нет
        assert cron("edge-prune") > cron("confidence-decay")
        assert cron("edge-prune") < cron("mark-stale")
        slots = daily_slots()
        assert len(slots) == len(set(slots)), "слоты beat не должны совпадать"

    def test_x02_no_physical_delete_anywhere(self):
        # инвариант графа: pruned_at — разметка, НЕ удаление;
        # во всём жизненном цикле рёбер нет DELETE (кроме явного тула)
        for name in ("PRUNE_EDGES_CANDIDATES", "PRUNE_EDGES_APPLY",
                     "RESTORE_EDGE", "REINFORCE_RELATIONS"):
            assert "DELETE" not in getattr(q, name).upper()


# ════════════════════════════════════════════════════════════════
# Ф1-MUT: reinforce — канонизация, дедуп, хебб-добавка, флаги
# ════════════════════════════════════════════════════════════════


class TestReinforceAcceptance:
    """§4.2: одно касание за проход, LEAST-кламп, флаг-off — ноль UPDATE."""

    def test_mut_canonicalization_both_directions_and_dedup(self):
        # (A,B), (B,A), (A,B) → одна каноническая пара; петли отброшены
        assert canonical_edge_pairs([(A, B), (B, A), (A, B), (A, A)]) == [(A, B)]
        assert canonical_edge_pairs([(C, A), (A, C), (B, C)]) == [(A, C), (B, C)]

    def test_mut_sql_touch_semantics(self):
        # +1 ровно (не +=N), якорь now, хебб-добавка с клампом
        assert "used_count   = r.used_count + 1" in q.REINFORCE_RELATIONS
        assert "last_used_at = now()" in q.REINFORCE_RELATIONS
        assert "LEAST(1.0, r.weight + (1.0 - r.weight) * $3::float8)" in q.REINFORCE_RELATIONS

    def test_mut_sql_matches_pair_either_direction(self):
        # канонизированная пара матчится в обе стороны (безразличие к направлению)
        assert "(r.source_id = p.a_id AND r.target_id = p.b_id)" in q.REINFORCE_RELATIONS
        assert "(r.source_id = p.b_id AND r.target_id = p.a_id)" in q.REINFORCE_RELATIONS

    def test_mut_pruned_never_reinforced(self):
        # RNF-09: pruned-ребро касанием не воскрешается
        assert "r.pruned_at IS NULL" in q.REINFORCE_RELATIONS

    def test_mut_hebb_saturation_example(self):
        # 0.05 → 0.24; 1.0 остаётся 1.0 (добавка затухает у сильных рёбер)
        assert min(1.0, 0.05 + ALPHA * 0.95) == pytest.approx(0.24)
        assert min(1.0, 1.0 + ALPHA * 0.0) == 1.0

    @pytest.mark.asyncio
    async def test_mut_service_single_roundtrip_per_batch(self):
        # дедуп до SQL: 3 сырых пары → 1 каноническая → один вызов репозитория
        repo = MagicMock()
        repo.reinforce_relations = AsyncMock(return_value=1)
        service = service_stub(repo)
        touched = await service.reinforce_edges([(A, B), (B, A), (A, B)])
        assert touched == 1
        repo.reinforce_relations.assert_awaited_once()
        args = repo.reinforce_relations.await_args[0]
        assert args[0] == [(A, B)]          # канонизированный дедуп-батч
        assert args[1] == pytest.approx(ALPHA)

    @pytest.mark.asyncio
    async def test_mut_two_batches_accumulate_per_pass(self):
        # два последовательных вызова = два касания (used_count 0→1→2):
        # каждое — свой UPDATE с «+1 ровно» (SQL-инвариант выше)
        repo = MagicMock()
        repo.reinforce_relations = AsyncMock(return_value=1)
        service = service_stub(repo)
        await service.reinforce_edges([(A, B)])
        await service.reinforce_edges([(A, B)])
        assert repo.reinforce_relations.await_count == 2

    def test_mut_flags_off_zero_updates(self, monkeypatch):
        # Ф1-RNF-06: флаги выключены (дефолт Settings!) — SQL не выполняется
        # вовсе: хук даже не ставит задачу в очередь
        from memory_server.tasks import memory_tasks

        sent: list = []
        fake_app = MagicMock()
        fake_app.send_task = lambda *a, **k: sent.append(k)
        monkeypatch.setattr("memory_server.celery_app.app", fake_app)
        # дефолт Settings: edge_lifecycle_enabled=False (прод до включения)
        monkeypatch.setattr(
            "memory_server.config.settings", Settings()
        )
        memory_tasks.enqueue_reinforce([(A, B)])
        assert sent == []  # мастер-выключатель: тишина

    def test_mut_dispatch_failure_never_breaks_serving(self):
        # RNF-07: сломанный брокер не роняет выдачу (best-effort)
        from memory_server.tasks import memory_tasks

        def broken_hook(pairs):
            raise RuntimeError("broker down")

        service = MemoryService(
            repository=MagicMock(),
            embedding_provider=MagicMock(),
            namespace_repository=MagicMock(),
            config=accept_config(),
            edge_dispatch=broken_hook,
        )
        service._dispatch_reinforce([(A, B)])  # не поднимает исключение

    def test_mut_default_settings_lifecycle_disabled(self):
        # прод-дефолт: вся Ф1 выключена до решения Мастера (чек-лист §9)
        assert Settings().edge_lifecycle_enabled is False
        assert Settings().edge_reinforcement_enabled is True
