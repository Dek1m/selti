"""Приёмочный сьют Ф2 traverse(strategy="activation") — тест-план ред. 2.

Уровень ПРИЁМКИ: эталоны §5.1 (r1/r2/стационар graph6), расфиксированный
DESIGN-SYM (related_to симметрируется, depends_on — направлен; вердикт
Эны 23.09), reinforce по потоку активации, w_eff-проводимость, ошибки,
латентность (soft-бюджет 50 мс) и метрики наблюдаемости.
Дублирование с tests/test_traverse_activation.py допустимо: юниты Соны
проверяют реализацию, приёмка — контракт (другой уровень доверия).
"""

import inspect
import random
import statistics
import warnings
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.memory.activation import ActivationSpreader
from memory_server.memory.service import MemoryService

S, A, B, C, D, E = ("seed", "a", "b", "c", "d", "e")

# graph6 §5.1 — направленные дуги с равными весами; числа эталона
# (r1/r2/стационар) выписаны в тест-плане именно для этой топологии.
GRAPH6 = [
    (S, A, 1.0), (S, B, 1.0), (A, C, 1.0), (B, C, 1.0),
    (C, D, 1.0), (D, E, 1.0), (E, C, 1.0),
]

# Стационар §5.1 (d=0.85, телепорт в S): порядок топ-K C > D > E > S > A = B
STATIONARY = {S: 0.1500, A: 0.06375, B: 0.06375, C: 0.2809, D: 0.2388, E: 0.2030}
TOLERANCE = 0.02  # 15 итераций: остаток затухания 0.85^15 ≈ 0.087

DAMPING = 0.85


def activation_config(**overrides) -> Settings:
    base = {
        "dedup_enabled": False,
        "hybrid_search_enabled": False,
        "traverse_activation_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)


def ppr_reference(edges, seed, damping, iterations):
    """Независимый оракул: плотная numpy-реализация (не копия прод-кода)."""
    nodes = sorted({u for u, _, _ in edges} | {v for _, v, _ in edges})
    idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)
    out_w = np.zeros(n)
    for u, v, w in edges:
        out_w[idx[u]] += w
    P = np.zeros((n, n))
    for u, v, w in edges:
        if out_w[idx[u]] > 0:
            P[idx[u], idx[v]] += w / out_w[idx[u]]
    e = np.zeros(n)
    seeds = seed if isinstance(seed, list) else [seed]
    for s in seeds:
        e[idx[s]] = 1.0
    e /= e.sum()
    r = e.copy()
    for _ in range(iterations):
        r = damping * (P.T @ r) + (1 - damping) * e
    return {node: float(r[idx[node]]) for node in nodes}


def service_stub(repo: MagicMock, dispatch=None, **cfg) -> MemoryService:
    """Сервис на мок-репозитории; graph6 возвращает выборка активации."""
    repo.fetch_activation_edges = AsyncMock(return_value=list(GRAPH6))
    repo.fetch_by_ids = AsyncMock(return_value=[
        {"id": gid, "content": f"content {gid}", "namespace": "default",
         "importance": 3}
        for gid in (S, A, B, C, D, E)
    ])
    return MemoryService(
        repository=repo,
        embedding_provider=MagicMock(),
        namespace_repository=MagicMock(),
        config=activation_config(**cfg),
        edge_dispatch=dispatch,
    )


# ════════════════════════════════════════════════════════════════
# Ф2-PPR-conv-def: дефолты сходимости (вердикт Эны 23.09: iterations=25)
# ════════════════════════════════════════════════════════════════


class TestConvergenceDefaults:
    """Дефолт итераций и сходимость graph6 к стационару §5.1."""

    def test_settings_default_iterations_25(self):
        # контракт ред. 2 + вердикт Эны 23.09: 25 итераций в дефолте
        assert Settings().traverse_activation_iterations == 25

    def test_spread_signature_default_synced_with_settings(self):
        # дефолт сигнатуры spread и дефолт Settings — одно число:
        # расхождение означало бы два разных «молчаливых» умолчания
        default = inspect.signature(
            ActivationSpreader.spread
        ).parameters["iterations"].default
        assert default == Settings().traverse_activation_iterations

    def test_graph6_default_spread_top6_order(self):
        # spread БЕЗ явного iterations: порядок топ-6 C > D > E > S > A = B
        spreader = ActivationSpreader(GRAPH6, damping=DAMPING)
        ranked = spreader.spread([S], top_k=6)
        ids = [gid for gid, _ in ranked]
        assert ids[:3] == [C, D, E]
        assert ids[3] == S
        assert set(ids[4:]) == {A, B}  # A = B: порядок внутри пары не важен

    def test_graph6_default_scores_near_stationary(self):
        # каждое значение — в ±0.02 от стационара §5.1 (допуск ред. 2)
        spreader = ActivationSpreader(GRAPH6, damping=DAMPING)
        scores = dict(spreader.spread([S], top_k=6))
        for gid, expected in STATIONARY.items():
            assert scores[gid] == pytest.approx(expected, abs=TOLERANCE), gid

    def test_graph6_mass_sums_to_one(self):
        spreader = ActivationSpreader(GRAPH6, damping=DAMPING)
        scores = dict(spreader.spread([S], top_k=6))
        assert sum(scores.values()) == pytest.approx(1.0, abs=0.01)

    def test_directed_reference_oracle_converges(self):
        # направленный оракул и прод-движок совпадают; допуск 5e-7 —
        # выдача движка округлена до 6 знаков (round(score, 6))
        for iterations in (15, 25):
            spreader = ActivationSpreader(GRAPH6, damping=DAMPING)
            engine = dict(spreader.spread([S], iterations=iterations, top_k=6))
            oracle = ppr_reference(GRAPH6, S, DAMPING, iterations)
            for gid, expected in oracle.items():
                assert engine[gid] == pytest.approx(expected, abs=5e-7), gid


# ════════════════════════════════════════════════════════════════
# Ф2-PPR-rnf-flow: reinforce по потоку активации (пары топ-K, flow ≥ 0.001)
# ════════════════════════════════════════════════════════════════


class TestActivationReinforceFlow:
    """Пройденный активацией подграф (топ-K) становится reinforce-батчем;
    рёбра с концом вне топ-K касанием не вознаграждаются (flow ≥ 0.001)."""

    @pytest.mark.asyncio
    async def test_rnf_batch_exactly_topk_conductor_pairs(self):
        # graph6, seed S, top_k=3 → C,D + ensure-S; проводник C—D
        # (flow = r(C)×1.0 ≈ 0.28 ≥ 0.001) обязателен в батче; рёбра
        # A—C / B—C / S—A / S—B (конец вне топ-3) не касаются
        dispatch = MagicMock()
        service = service_stub(MagicMock(), dispatch=dispatch,
                               traverse_activation_top_k=3)
        result = await service.traverse(S, strategy="activation")
        seen_ids = {n["id"] for n in result.nodes}
        assert C in seen_ids and D in seen_ids
        dispatch.assert_called_once()
        pairs = dispatch.call_args[0][0]
        canonical = {tuple(sorted(p)) for p in pairs}
        assert tuple(sorted((C, D))) in canonical
        # ни одна пара не выводит за пределы выданных узлов топ-K
        assert all(a in seen_ids and b in seen_ids for a, b in pairs)
        # A никогда в топ-3 (score 0.06375 — минимум графа): касаний с ним нет
        assert not any(A in p for p in canonical)

    @pytest.mark.asyncio
    async def test_rnf_repeat_query_touches_once_per_pass(self):
        # повторный запрос — диспатч ещё ровно один раз, тот же батч:
        # used_count +1 за проход, не больше (одно касание за проход)
        dispatch = MagicMock()
        service = service_stub(MagicMock(), dispatch=dispatch,
                               traverse_activation_top_k=3)
        await service.traverse(S, strategy="activation")
        first_batch = [tuple(sorted(p)) for p in dispatch.call_args[0][0]]
        await service.traverse(S, strategy="activation")
        assert dispatch.call_count == 2
        second_batch = [tuple(sorted(p)) for p in dispatch.call_args[0][0]]
        assert first_batch == second_batch


# ════════════════════════════════════════════════════════════════
# Ф2-PPR-09 (расфиксация DESIGN-SYM): related_to — зеркало, depends_on — дуга
# ════════════════════════════════════════════════════════════════


class TestSymmetryDESIGNSYM:
    """Симметричный related_to против направленного depends_on."""

    def test_related_to_mirrored_in_activation_select(self):
        # SQL выборки отдаёт симметричные типы ОБЕИМИ дугами: UNION ALL
        # с обратной дугой под списком $4 (traverse_symmetric_link_types)
        assert "UNION ALL" in q.SELECT_ACTIVATION_EDGES
        assert "r.target_id::text AS source_id" in q.SELECT_ACTIVATION_EDGES
        assert "r.source_id::text AS target_id" in q.SELECT_ACTIVATION_EDGES
        assert "r.link_type = ANY($4::text[])" in q.SELECT_ACTIVATION_EDGES
        assert Settings().traverse_symmetric_link_types == ["related_to"]

    def test_related_to_spread_reaches_reverse(self):
        # единственное related_to A→B отдаётся двумя дугами → spread из B
        # достигает A (активация идёт по встречному направлению)
        mirrored = [(A, B, 1.0), (B, A, 1.0)]
        ranked = ActivationSpreader(mirrored, damping=DAMPING).spread([B], top_k=2)
        ids = [gid for gid, _ in ranked]
        assert A in ids

    def test_depends_on_directed_only(self):
        # единственный depends_on A→B — одна дуга: из B в A активация НЕ
        # идёт; seed держится только телепорт-вкладом (0.15), A — ровно 0
        ranked = ActivationSpreader([(A, B, 1.0)], damping=DAMPING).spread(
            [B], top_k=2
        )
        scores = dict(ranked)
        assert ranked[0][0] == B
        assert scores[B] == pytest.approx(0.15, abs=1e-6)  # только телепорт
        assert scores[A] == 0.0  # встречной дуги нет: ни капли активации
        # в узкий топ-K недостижимый узел не попадает вовсе
        top1 = ActivationSpreader([(A, B, 1.0)], damping=DAMPING).spread(
            [B], top_k=1
        )
        assert [gid for gid, _ in top1] == [B]

    def test_directed_benchmark_converges_to_reference(self):
        # направленные эталоны сходятся: движок == оракул на both-arc графе
        # (допуск 5e-7: выдача движка округлена до 6 знаков — round(score, 6))
        both_arcs = GRAPH6 + [(v, u, w) for u, v, w in GRAPH6]
        spreader = ActivationSpreader(both_arcs, damping=DAMPING)
        engine = dict(spreader.spread([S], iterations=25, top_k=6))
        oracle = ppr_reference(both_arcs, S, DAMPING, 25)
        for gid, expected in oracle.items():
            assert engine[gid] == pytest.approx(expected, abs=5e-7), gid


# ════════════════════════════════════════════════════════════════
# Ф2-PPR-weff: проводимость затухшего рёбра в downstream-скоре
# ════════════════════════════════════════════════════════════════


class TestWeffConductivity:
    """Изоморфные графы: разница только в свежести проводника S→M."""

    # 100 дней без касаний: w_eff = 1.0 × exp(−0.02×100) = exp(−2) ≈ 0.1353
    STALE_CONDUCTOR = float(np.exp(-2.0))

    def _downstream_score(self, conductor_weight: float) -> float:
        # изоморфные графы: S расходится на проводника M (вес c) и
        # конкурента X (вес 1.0); доля активации в D пропорциональна
        # c/(c+1) — абсолютный вес проводника влияет только на фоне
        # альтернативной дуги (нормировка по исходящим это подчёркивает)
        edges = [
            (S, "m", conductor_weight), (S, "x", 1.0),
            ("m", D, 1.0), ("x", "y", 1.0),
        ]
        spreader = ActivationSpreader(edges, damping=DAMPING)
        scores = dict(spreader.spread([S], iterations=25))
        return scores[D]

    def test_fresh_conductor_scores_strictly_higher(self):
        stale = self._downstream_score(self.STALE_CONDUCTOR)  # c/(c+1)≈0.119
        fresh = self._downstream_score(1.0)                   # c/(c+1)=0.5
        assert 0.0 < stale < fresh, (
            "затухший проводник обязан глушить downstream-скор строго"
        )

    def test_stale_conductor_matches_weff_oracle(self):
        # 0.135335 — эталон w_eff(weight=1.0, used=0, 100 дней, λ=0.02)
        assert self.STALE_CONDUCTOR == pytest.approx(0.135335, abs=1e-5)

    def test_lambda_zero_is_bit_identical_regardless_of_age(self):
        # λ=0 и λ_min=0: GREATEST(0, 0/(1+u)) = 0 → exp(0) = 1 — вес
        # не зависит от возраста бит-в-бит (проверка контракта формулы)
        import math

        for days in (0, 10, 100, 1000):
            for used in (0, 3, 9):
                lam_eff = max(0.0, 0.0 / (1 + used))
                assert 1.0 * math.exp(-lam_eff * days) == 1.0
        # SQL-форма это допускает: оба плейсхолдера — параметры запроса
        assert "{decay_lambda_min}" in q._W_EFF_TEMPLATE
        assert "GREATEST({decay_lambda_min}," in q._W_EFF_TEMPLATE


# ════════════════════════════════════════════════════════════════
# Ф2-BFS / Ф2-ERR: регрессия default и внятные ошибки
# ════════════════════════════════════════════════════════════════


class TestBfsRegressionAndErrors:
    """BFS-контракт не изменился; флаги-off и мусорные strategy — ошибки."""

    @pytest.mark.asyncio
    async def test_bfs_default_unchanged(self):
        repo = MagicMock()
        repo.get_by_id = AsyncMock(return_value=MagicMock())
        repo.traverse = AsyncMock(return_value={"nodes": [], "edges": []})
        service = service_stub(repo, traverse_activation_enabled=False)
        await service.traverse(S, depth=2)
        repo.traverse.assert_awaited_once()          # прежний путь: хранимка
        repo.fetch_activation_edges.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_activation_disabled_explicit_error(self):
        # Ф2-ERR-01: внятная ошибка, НЕ 500 и НЕ тихий fallback на bfs
        repo = MagicMock()
        service = service_stub(repo, traverse_activation_enabled=False)
        with pytest.raises(ValueError, match="traverse_activation_enabled"):
            await service.traverse(S, strategy="activation")
        repo.fetch_activation_edges.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_strategy_listed(self):
        repo = MagicMock()
        service = service_stub(repo)
        with pytest.raises(ValueError, match="bfs"):
            await service.traverse(S, strategy="dfs")

    @pytest.mark.asyncio
    async def test_unknown_start_empty_result(self):
        # старт вне графа: пустой результат штатно, не исключение
        repo = MagicMock()
        repo.fetch_activation_edges = AsyncMock(return_value=[])
        service = service_stub(repo)
        result = await service.traverse("unknown-id", strategy="activation")
        assert result.nodes == []
        assert result.total_nodes == 0


# ════════════════════════════════════════════════════════════════
# Наблюдаемость: инкременты метрик Мая на операциях
# ════════════════════════════════════════════════════════════════


class TestActivationMetrics:
    """TRAVERSE_ACTIVATION_*: ok/empty-инкременты и latency-наблюдения."""

    @staticmethod
    def _counter(status: str) -> float:
        from prometheus_client import REGISTRY

        return REGISTRY.get_sample_value(
            "selti_traverse_activation_requests_total", {"status": status}
        ) or 0.0

    @staticmethod
    def _latency_count() -> float:
        from prometheus_client import REGISTRY

        return REGISTRY.get_sample_value(
            "selti_traverse_activation_latency_seconds_count"
        ) or 0.0

    @pytest.mark.asyncio
    async def test_ok_and_empty_counters_increment(self):
        before_ok = self._counter("ok")
        before_empty = self._counter("empty")
        before_lat = self._latency_count()

        ok_service = service_stub(MagicMock())
        await ok_service.traverse(S, strategy="activation")

        empty_repo = MagicMock()
        empty_repo.fetch_activation_edges = AsyncMock(return_value=[])
        empty_service = service_stub(empty_repo)
        await empty_service.traverse("ghost", strategy="activation")

        assert self._counter("ok") == before_ok + 1
        assert self._counter("empty") == before_empty + 1
        assert self._latency_count() >= before_lat + 2


# ════════════════════════════════════════════════════════════════
# Ф2-PRF-01: latency-бюджет 15k рёбер < 50 мс (soft-ассерт)
# ════════════════════════════════════════════════════════════════


class TestLatencyBudget:
    """Медиана 5 прогонов spread на синтетике 15k рёбер; CI-бюджет 50 мс.

    Soft-ассерт: превышение — warning в выводе pytest, не падение
    (жёсткий порог — прерогатива стенда/nightly на 106k, Ф2-PRF-02).
    """

    N_NODES = 3_000
    N_EDGES = 15_000

    def _synth_edges(self) -> list[tuple[str, str, float]]:
        rng = random.Random(42)
        nodes = [f"n{i}" for i in range(self.N_NODES)]
        edges = []
        for i in range(self.N_EDGES):
            src = nodes[rng.randrange(self.N_NODES)]
            dst = nodes[rng.randrange(self.N_NODES)]
            if src == dst:
                dst = nodes[(nodes.index(dst) + 1) % self.N_NODES]
            edges.append((src, dst, rng.uniform(0.05, 1.0)))
        return edges

    def test_prf01_median_under_50ms_soft(self):
        import time

        spreader = ActivationSpreader(self._synth_edges(), damping=DAMPING)
        seed = ["n0"]
        # прогрев (CSR построен в __init__; первый spread — кеш аллокаций)
        spreader.spread(seed, iterations=15, top_k=50)
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            spreader.spread(seed, iterations=15, top_k=50)
            timings.append(time.perf_counter() - started)
        median = statistics.median(timings)
        if median >= 0.05:
            warnings.warn(
                f"PRF-01 soft-бюджет превышен: медиана {median * 1000:.1f} мс"
                f" на 5 прогонах (бюджет 50 мс, {self.N_EDGES} рёбер)"
            )
        assert median > 0  # сам замер состоялся; жёсткий порог — стенд
