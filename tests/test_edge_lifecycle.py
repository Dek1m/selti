"""Жизнь рёбер (V3.5 «Жизнь графа знаний», Ф1) — формулы Эны 22.09.

Живой БД нет (паттерн test_linker_v3): SQL-контракты — инвариантами на
текстах констант queries.py, математика — эталонной Python-реализацией
(единственная копия формулы живёт в SQL; эталон здесь фиксирует вердикты
для стенд-сверки Катерины), поведение — юнитами на mock_pool.
"""

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.db import queries as q
from memory_server.memory.service import MemoryService, canonical_edge_pairs

A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"
C = "00000000-0000-0000-0000-00000000000c"

# Вердикты Эны 22.09: λ=0.02, λ_min=0.002, floor=0.05, возраст ≥ 30 дней
LAMBDA = 0.02
LAMBDA_MIN = 0.002



def _patch_reinforce_runtime(monkeypatch, values: dict) -> None:
    """enqueue_reinforce читает флаги Ф1 из runtime-снапшота state."""
    from types import SimpleNamespace

    from memory_server.runtime_config import RuntimeConfig

    runtime = RuntimeConfig(db_values=values)
    monkeypatch.setattr(
        "memory_server.state.get_state",
        lambda: SimpleNamespace(get_runtime_config_sync=lambda: runtime),
    )


def edge_config(**overrides) -> RuntimeConfig:
    """Конфиг жизни рёбер: Ф1 включена (боевой контур виден)."""
    base = {
        "dedup_enabled": False,
        "hybrid_search_enabled": False,
        "edge_lifecycle_enabled": True,
    }
    base.update(overrides)
    return RuntimeConfig(db_values=base)


def make_service(mock_repository, mock_embedding_provider, mock_namespace_repository,
                 mock_project_repository, **cfg) -> MemoryService:
    return MemoryService(
        repository=mock_repository,
        embedding_provider=mock_embedding_provider,
        namespace_repository=mock_namespace_repository,
        runtime=edge_config(**cfg),
        project_repository=mock_project_repository,
    )


# ── Эталонная математика (вердикты Эны; живёт в SQL, здесь — оракул) ──


def lambda_eff(used_count: int) -> float:
    """λ_eff = GREATEST(λ_min, λ / (1 + used_count)) — сатурация частоты."""
    return max(LAMBDA_MIN, LAMBDA / (1 + used_count))


def w_eff(weight: float, used_count: int, anchor: datetime, now: datetime) -> float:
    """Ленивая проекция веса: immune-рёбра не затухают (immune — атрибут
    строки, в эталоне моделируется только decay-ветка)."""
    days = max((now - anchor).total_seconds() / 86400.0, 0.0)
    return weight * math.exp(-lambda_eff(used_count) * days)


NOW = datetime(2026, 9, 22, 3, 30, tzinfo=timezone.utc)


class TestLambdaEffSaturation:
    """λ_eff-сатурация: частое использование замедляет затухание, ниже
    λ_min не опускается (консервация боевых рёбер)."""

    def test_zero_usage_full_lambda(self):
        assert lambda_eff(0) == pytest.approx(0.02)

    def test_saturation_curve(self):
        assert lambda_eff(4) == pytest.approx(0.004)   # 0.02/5
        assert lambda_eff(9) == pytest.approx(0.002)   # 0.02/10 — ровно λ_min

    def test_clamp_at_min(self):
        # used=99: 0.02/100 = 0.0002 < λ_min — клампится, дальше не падает
        assert lambda_eff(99) == pytest.approx(LAMBDA_MIN)
        assert lambda_eff(10_000) == pytest.approx(LAMBDA_MIN)

    def test_monotone_non_increasing(self):
        values = [lambda_eff(u) for u in range(0, 30)]
        assert all(x >= y for x, y in zip(values, values[1:]))

    def test_sql_formula_matches(self):
        """Та же формула — в кандидатском и активационном SQL."""
        formula = "GREATEST($2::float8, $1::float8 / (1 + r.used_count))"
        assert formula in q.PRUNE_EDGES_CANDIDATES
        assert formula in q.SELECT_ACTIVATION_EDGES


class TestWEffExamples:
    """w_eff-примеры с датами: якорь COALESCE(last_used_at, created_at)."""

    def test_21_days_unused(self):
        anchor = NOW - timedelta(days=21)
        # 1.0 × exp(−0.02×21) = exp(−0.42) ≈ 0.6570
        assert w_eff(1.0, 0, anchor, NOW) == pytest.approx(0.657047, abs=1e-4)

    def test_coalesce_last_used_wins(self):
        created = NOW - timedelta(days=52)
        touched = NOW - timedelta(days=7)
        # якорь last_used_at: 7 дней, не 52 → 0.6 × exp(−0.02×7) ≈ 0.5216
        assert w_eff(0.6, 0, touched, NOW) == pytest.approx(0.521615, abs=1e-4)
        assert w_eff(0.6, 0, created, NOW) < w_eff(0.6, 0, touched, NOW)

    def test_usage_preserves_weight(self):
        anchor = NOW - timedelta(days=21)
        # used=9 → λ_eff=0.002: 1.0 × exp(−0.042) ≈ 0.9589
        assert w_eff(1.0, 9, anchor, NOW) == pytest.approx(0.958876, abs=1e-4)

    def test_zero_days_no_decay(self):
        assert w_eff(0.7, 0, NOW, NOW) == pytest.approx(0.7)

    def test_raw_below_floor_is_prunable(self):
        """90 дней без касаний: raw 0.2×exp(−1.8) ≈ 0.0331 ≤ floor 0.05 —
        кандидат (Д1: порог по raw значению, не по clamp)."""
        anchor = NOW - timedelta(days=90)
        assert w_eff(0.2, 0, anchor, NOW) == pytest.approx(0.033060, abs=1e-5)

    def test_sql_anchor_is_coalesce(self):
        assert "COALESCE(r.last_used_at, r.created_at)" in q.PRUNE_EDGES_CANDIDATES
        assert "COALESCE(r.last_used_at, r.created_at)" in q.SELECT_ACTIVATION_EDGES


class TestLazyIdempotency:
    """Идемпотентность ленивого подхода (закрытие Д2): нет батча — нет
    двойного затухания; повторный расчёт в тот же день даёт то же число."""

    def test_recompute_same_day_noop(self):
        anchor = NOW - timedelta(days=30)
        first = w_eff(1.0, 0, anchor, NOW)
        second = w_eff(1.0, 0, anchor, NOW)
        assert first == second

    def test_recompute_next_day_single_decay_only(self):
        """+1 день = ровно один шаг затухания от НЕмутируемого якоря — не
        экспонента от уже уменьшенного веса (старый баг материализации)."""
        anchor = NOW - timedelta(days=30)
        tomorrow = NOW + timedelta(days=1)
        lazy = w_eff(1.0, 0, anchor, tomorrow)
        materialized_twice = w_eff(w_eff(1.0, 0, anchor, NOW), 0, anchor, tomorrow)
        assert lazy > materialized_twice  # материализация затухала бы дважды

    def test_no_daily_weight_batch_in_queries(self):
        """Ежедневный decay-UPDATE веса НЕ существует: единственные пишущие
        вес запросы — reinforce/restore + mutual-кассета L1c (Фаза 3:
        reinforce существующей пары внутри INSERT co-occurrence — событие
        линкера, не ежедневный батч)."""
        sqls = {name: getattr(q, name) for name in dir(q)
                if name.isupper() and isinstance(getattr(q, name), str)}
        weight_writers = [
            name for name, sql in sqls.items()
            if "UPDATE relations" in sql and "weight" in sql.split("WHERE")[0].split("SET")[-1]
        ]
        assert set(weight_writers) == {
            "REINFORCE_RELATIONS", "RESTORE_EDGE", "INSERT_COOCCURRENCE_LINKS",
        }

    def test_prune_writes_only_pruned_at(self):
        """Материализуется ТОЛЬКО pruned_at (никаких весов/статусов)."""
        set_clause = q.PRUNE_EDGES_APPLY.split("WHERE")[0]
        assert "SET pruned_at = now()" in set_clause
        assert "weight" not in set_clause
        assert "status" not in set_clause
        # и это UPDATE, не DELETE
        assert "DELETE" not in q.PRUNE_EDGES_APPLY


class TestPruneCandidateImmunities:
    """Каждый иммунитет — отдельный инвариант SQL (Ф1-IMM)."""

    SQL = q.PRUNE_EDGES_CANDIDATES

    def test_manual_edges_immune(self):
        """source ≠ linker_v3 (NULL source = ручное/синк — IS DISTINCT FROM)."""
        assert "r.metadata->>'source' IS DISTINCT FROM 'linker_v3'" in self.SQL

    def test_l2_layer_immune(self):
        assert "r.metadata->>'layer' = 'l2'" in self.SQL

    def test_inherited_rewire_immune(self):
        assert "r.inherited_from IS NOT NULL" in self.SQL

    def test_frozen_granules_immune(self):
        """frozen — колонка memories (018): вечные факты не затухают (D4)."""
        assert "src.frozen" in self.SQL
        assert "tgt.frozen" in self.SQL

    def test_bridge_immune_only_from_pruning(self):
        """Мост: оба кластера определены и различны (NULL — не мост, Д4)."""
        assert "src.cluster_id IS NOT NULL" in self.SQL
        assert "tgt.cluster_id IS NOT NULL" in self.SQL
        assert "src.cluster_id IS DISTINCT FROM tgt.cluster_id" in self.SQL

    def test_bridge_still_decays(self):
        """Мост — иммунитет ТОЛЬКО от pruning: в w_eff SELECT (decay-проекция)
        мостового исключения нет — затухает как обычное ребро."""
        assert "cluster_id" not in q.SELECT_ACTIVATION_EDGES

    def test_min_age_guard(self):
        assert "r.created_at <= now() - make_interval(days => $3::int)" in self.SQL

    def test_threshold_on_raw_value(self):
        """Порог — на raw w_eff БЕЗ clamp (закрытие Д1): сравнение с
        произведением weight × exp(−λ_eff × days), не с GREATEST-floor."""
        assert "r.weight * exp(" in self.SQL
        assert "<= $4::float8" in self.SQL
        # floor-клампа в сравнении нет
        comparison = self.SQL.split("<= $4::float8")[0]
        assert "GREATEST($4" not in comparison

    def test_only_alive_resolved_edges(self):
        assert "r.pruned_at IS NULL" in self.SQL
        assert "r.target_id IS NOT NULL" in self.SQL


class TestReinforce:
    """Касание: канонизация пар в Python, TOUCH-семантика в SQL."""

    def test_canonical_pairs_dedup_and_direction(self):
        pairs = [(A, B), (B, A), (A, B), (A, A)]
        assert canonical_edge_pairs(pairs) == [(A, B)]

    def test_canonical_pairs_sorted_deterministic(self):
        assert canonical_edge_pairs([(C, A), (B, C)]) == [(A, C), (B, C)]

    def test_sql_touch_semantics(self):
        sql = " ".join(q.REINFORCE_RELATIONS.split())  # нормализация отбивки
        assert "used_count = r.used_count + 1" in sql
        assert "last_used_at = now()" in sql
        assert "LEAST(1.0, r.weight + (1.0 - r.weight) * $3::float8)" in sql
        # только живые; reinforce не воскрешает pruned
        assert "r.pruned_at IS NULL" in sql

    def test_sql_matches_pair_either_direction(self):
        sql = q.REINFORCE_RELATIONS
        assert "(r.source_id = p.a_id AND r.target_id = p.b_id)" in sql
        assert "(r.source_id = p.b_id AND r.target_id = p.a_id)" in sql

    @pytest.mark.asyncio
    async def test_service_reinforce_single_roundtrip(self, mock_pool):
        """Батч пар — один fetch (не N+1); канонизация до SQL (Ф1-RNF-02/04)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": "r1"}, {"id": "r2"}])
        service = make_service_from_pool(mock_pool)
        touched = await service.reinforce_edges([(B, A), (A, B), (A, B)])
        assert touched == 2
        assert conn.fetch.await_count == 1
        args = conn.fetch.await_args[0]
        assert args[0] == q.REINFORCE_RELATIONS
        assert [str(u) for u in args[1]] == [A]  # канонизовано least/greatest
        assert [str(u) for u in args[2]] == [B]
        assert args[3] == pytest.approx(0.2)  # edge_reinforce_alpha

    @pytest.mark.asyncio
    async def test_service_reinforce_empty_noop(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        service = make_service_from_pool(mock_pool)
        assert await service.reinforce_edges([(A, A)]) == 0
        conn.fetch.assert_not_awaited()


class TestRestoreEdge:
    def test_sql_restore_semantics(self):
        sql = q.RESTORE_EDGE
        assert "pruned_at    = NULL" in sql or "pruned_at   = NULL" in sql
        assert "used_count   = used_count + 1" in sql
        assert "pruned_at IS NOT NULL" in sql  # гвард: только pruned (Д3)

    @pytest.mark.asyncio
    async def test_service_restore(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(return_value={"id": A})
        service = make_service_from_pool(mock_pool)
        assert await service.restore_edge(A) is True
        args = conn.fetchrow.await_args[0]
        assert args[0] == q.RESTORE_EDGE
        assert str(args[1]) == A
        assert args[2] == pytest.approx(0.5)  # β восстановления


class TestEdgePruneCampaign:
    @pytest.mark.asyncio
    async def test_disabled_master_switch_skips(self, mock_pool):
        """Мастер-выключатель: SQL не выполняется вовсе."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        service = make_service_from_pool(mock_pool, edge_lifecycle_enabled=False)
        result = await service.edge_prune()
        assert result == {"skipped": "edge_lifecycle_enabled"}
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dry_run_report_only(self, mock_pool):
        """dry_run (дефолт): только SELECT кандидатов, ни одного UPDATE."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": "r1"}, {"id": "r2"}])
        service = make_service_from_pool(mock_pool)
        result = await service.edge_prune()  # dry_run не задан → конфиг True
        assert result["dry_run"] is True
        assert result["candidates"] == 2
        conn.fetch.assert_awaited_once_with(
            q.PRUNE_EDGES_CANDIDATES, 0.02, 0.002, 30, 0.05
        )

    @pytest.mark.asyncio
    async def test_apply_batches_of_1000(self, mock_pool):
        """Бой: UPDATE pruned_at батчами по 1000 (не одним гигантом)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        candidates = [{"id": f"00000000-0000-0000-0000-{i:012d}"} for i in range(2500)]
        conn.fetch = AsyncMock(
            side_effect=[candidates] + [[{"id": f"x{i}"} for i in range(1000)]] * 3
        )
        service = make_service_from_pool(mock_pool, edge_prune_dry_run=False)
        result = await service.edge_prune()
        assert result["candidates"] == 2500
        assert result["pruned"] == 3000  # заглушка мока: 3 батча по 1000
        assert conn.fetch.await_count == 4  # SELECT + 3 UPDATE-батча

    @pytest.mark.asyncio
    async def test_apply_idempotent_on_empty(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[])
        service = make_service_from_pool(mock_pool, edge_prune_dry_run=False)
        result = await service.edge_prune()
        assert result["pruned"] == 0
        assert conn.fetch.await_count == 1  # только SELECT


class TestReinforceDispatch:
    """Хук enqueue_reinforce: флаги в хуке, сбой не поднимается наверх."""

    def _hook(self, monkeypatch, sent):
        from memory_server.tasks import memory_tasks

        def fake_send(*args, **kwargs):
            sent.append(kwargs)
        fake_app = MagicMock()
        fake_app.send_task = fake_send
        monkeypatch.setattr("memory_server.celery_app.app", fake_app)
        return memory_tasks.enqueue_reinforce

    def test_flags_gate_in_hook(self, monkeypatch):
        sent: list = []
        hook = self._hook(monkeypatch, sent)
        _patch_reinforce_runtime(monkeypatch, {"edge_lifecycle_enabled": True})
        hook([(A, B)])
        assert len(sent) == 1
        # мастер-выключатель: молчит
        _patch_reinforce_runtime(monkeypatch, {"edge_lifecycle_enabled": False})
        hook([(A, B)])
        # точечный флаг: молчит
        _patch_reinforce_runtime(
            monkeypatch, {"edge_lifecycle_enabled": True, "edge_reinforcement_enabled": False}
        )
        hook([(A, B)])
        assert len(sent) == 1  # оба гейта отработали тихо

    def test_dispatch_failure_non_fatal(self, monkeypatch):
        from memory_server.tasks import memory_tasks

        _patch_reinforce_runtime(monkeypatch, {"edge_lifecycle_enabled": True})

        def broken_send(*args, **kwargs):
            raise RuntimeError("broker down")

        broken_app = MagicMock()
        broken_app.send_task = broken_send
        monkeypatch.setattr("memory_server.celery_app.app", broken_app)
        memory_tasks.enqueue_reinforce([(A, B)])  # не поднимает исключение

    @pytest.mark.asyncio
    async def test_search_hook_fires_all_pairs(self, mock_pool, mock_embedding_provider,
                                               mock_namespace_repository,
                                               mock_project_repository):
        """Выдача топ-K → C(k,2) пар гранул в диспетчер (вердикт Эны)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        rows = [
            {"id": f"00000000-0000-0000-0000-00000000000{i}", "content": f"c{i}",
             "metadata": {}, "namespace": "default", "importance": 3,
             "project_id": None, "status": "asserted", "created_at": NOW,
             "last_accessed_at": None, "frozen": False}
            for i in range(3)
        ]
        conn.fetch = AsyncMock(return_value=rows)
        dispatch = MagicMock()
        service = MemoryService(
            repository=_repo_stub(search_rows=rows),
            embedding_provider=mock_embedding_provider,
            namespace_repository=mock_namespace_repository,
            runtime=edge_config(hybrid_search_enabled=False),
            project_repository=mock_project_repository,
            edge_dispatch=dispatch,
        )
        results = await service.search("q", user_id="u")
        assert len(results) == 3
        pairs = dispatch.call_args[0][0]
        assert len(pairs) == 3  # C(3,2)
        assert all(a != b for a, b in pairs)


def _repo_stub(search_rows):
    """Repository-заглушка для хука поиска: только search + bump_access."""
    repo = MagicMock()
    repo.search = AsyncMock(return_value=[
        _sr(row["id"], row["content"]) for row in search_rows
    ])
    repo.bump_access = AsyncMock(return_value=len(search_rows))
    return repo


def _sr(gid: str, content: str):
    from memory_server.models import SearchResult
    return SearchResult(
        id=gid, content=content, metadata={}, importance=3, score=0.9,
        namespace="default", created_at=NOW,
    )


def make_service_from_pool(mock_pool, **cfg) -> MemoryService:
    """Сервис на мок-пуле (pg-репозиторий реальный, SQL — в мок-коннект)."""
    from memory_server.memory.pg_repository import PostgreSQLRepository
    from memory_server.memory.repository import MemoryRepository

    repo = MemoryRepository(pg=PostgreSQLRepository(pool=mock_pool))
    ns = MagicMock()
    ns.get_by_uid = AsyncMock(return_value=None)
    return MemoryService(
        repository=repo,
        embedding_provider=MagicMock(),
        namespace_repository=ns,
        runtime=edge_config(**cfg),
    )


class TestBeatSchedule:
    def test_edge_prune_330_utc_after_confidence_decay(self):
        from memory_server.celery_app import app

        beat = app.conf.beat_schedule
        assert "edge-prune" in beat
        entry = beat["edge-prune"]
        assert entry["task"] == "memory_server.tasks.lifecycle_tasks.edge_prune"
        schedule = entry["schedule"]
        assert schedule.hour == {3} or schedule.hour == 3
        assert schedule.minute == {30} or schedule.minute == 30
        # порядок ночи: кластеры 02:00 → decay 03:00 → edge_prune 03:30 → stale 04:00
        assert "confidence-decay" in beat and "mark-stale" in beat
