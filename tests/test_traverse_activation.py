"""Activation traverse (V3.5 Ф2): PPR на CSR — эталон graph6 из §5.1
тест-плана Катерины.

Эталон: направленный граф S→A, S→B, A→C, B→C, C→D, D→E, E→C, веса 1.0,
damping 0.85, равновероятные/взвешенные переходы по исходящим.
  r1: S=0.15, A=B=0.425; r2: S=0.15, A=B=0.06375, C=0.7225;
  стационар: S=0.15, A=B=0.06375, C=0.2809, D=0.2388, E=0.2030.
"""

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from memory_server.config import Settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.memory.activation import ActivationSpreader
from memory_server.memory.service import MemoryService

S, A, B, C, D, E = ("seed", "a", "b", "c", "d", "e")

GRAPH6 = [
    (S, A, 1.0), (S, B, 1.0), (A, C, 1.0), (B, C, 1.0),
    (C, D, 1.0), (D, E, 1.0), (E, C, 1.0),
]


def activation_config(**overrides) -> RuntimeConfig:
    base = {
        "dedup_enabled": False,
        "hybrid_search_enabled": False,
        "traverse_activation_enabled": True,
    }
    base.update(overrides)
    return RuntimeConfig(db_values=base)


def ppr_reference(edges, seed, damping, iterations):
    """Независимый оракул: плотная numpy-реализация PageRank (не копия кода)."""
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
    for s in seed if isinstance(seed, list) else [seed]:
        e[idx[s]] = 1.0
    e /= e.sum()
    r = e.copy()
    for _ in range(iterations):
        r = damping * (P.T @ r) + (1 - damping) * e
    return {node: float(r[idx[node]]) for node in nodes}


class TestGraph6Reference:
    def test_first_iteration_exact(self):
        """r1 точно: ловит ошибку нормировки/транспонирования (Ф2-PPR-02)."""
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        ranked = dict(spreader.spread([S], iterations=1))
        assert ranked[S] == pytest.approx(0.15, abs=1e-6)
        assert ranked[A] == pytest.approx(0.425, abs=1e-6)
        assert ranked[B] == pytest.approx(0.425, abs=1e-6)
        assert ranked[C] == pytest.approx(0.0, abs=1e-6)
        assert sum(ranked.values()) == pytest.approx(1.0, abs=1e-6)

    def test_second_iteration_exact(self):
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        ranked = dict(spreader.spread([S], iterations=2))
        assert ranked[S] == pytest.approx(0.15, abs=1e-6)
        assert ranked[A] == pytest.approx(0.06375, abs=1e-6)
        assert ranked[B] == pytest.approx(0.06375, abs=1e-6)
        assert ranked[C] == pytest.approx(0.7225, abs=1e-6)

    def test_stationary_converged(self):
        """Стационар цикла C→D→E→C ±0.02 (Ф2-PPR-01).

        ВАЖНО (находка для Эны/Катерины): чистый трёхцикл осциллирует с
        затуханием 0.85^n — за 15 итераций остаточная амплитуда ~0.045
        (порядок C/D/E перевёрнут); эталон ±0.02 достигается к ~25.
        Формула верна — это скорость сходимости, не ошибка реализации.
        """
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        ranked = dict(spreader.spread([S], iterations=25))
        assert ranked[C] == pytest.approx(0.2809, abs=0.02)
        assert ranked[D] == pytest.approx(0.2388, abs=0.02)
        assert ranked[E] == pytest.approx(0.2030, abs=0.02)
        assert ranked[S] == pytest.approx(0.15, abs=0.01)
        assert sum(ranked.values()) == pytest.approx(1.0, abs=0.01)

    def test_fifteen_iterations_residual_oscillation(self):
        """Вердикт «15 итераций» на циклическом графе: фактическая точность
        ~±0.055 (замерено на эталоне), порядок топ-3 может отличаться."""
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        ranked = dict(spreader.spread([S], iterations=15))
        assert ranked[C] == pytest.approx(0.2809, abs=0.06)
        assert ranked[D] == pytest.approx(0.2388, abs=0.06)
        assert ranked[E] == pytest.approx(0.2030, abs=0.06)
        assert ranked[S] == pytest.approx(0.15, abs=0.01)
        assert sum(ranked.values()) == pytest.approx(1.0, abs=1e-5)

    def test_top_k_order_activation_spreads_from_seed(self):
        """Порядок топ-K: C > D > E > S > A=B — активация расходится от
        старта, это не BFS-порядок (Ф2-PPR-01)."""
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        ranked = spreader.spread([S], iterations=25)
        scores = dict(ranked)
        assert scores[C] > scores[D] > scores[E] > scores[S]
        assert scores[A] == pytest.approx(scores[B], abs=1e-9)


class TestPPRSemantics:
    def test_damping_from_config_matches_reference(self):
        """damping≠0.85: сверка с независимым numpy-оракулом (Ф2-PPR-03)."""
        for damping in (0.5, 0.7):
            spreader = ActivationSpreader(GRAPH6, damping=damping)
            got = dict(spreader.spread([S], iterations=25))
            ref = ppr_reference(GRAPH6, [S], damping, 25)
            for node, expected in ref.items():
                assert got[node] == pytest.approx(expected, abs=1e-6), node

    def test_multi_start_uniform_teleport(self):
        """Мульти-старт: e равномерно по обоим seed, Σr≈1 (Ф2-PPR-04)."""
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        ranked = dict(spreader.spread([A, B], iterations=15))
        ref = ppr_reference(GRAPH6, [A, B], 0.85, 15)
        for node, expected in ref.items():
            assert ranked[node] == pytest.approx(expected, abs=1e-5), node

    def test_top_k_exact_size_sorted_seed_ensured(self):
        """K=3: ровно 3 узла, по убыванию; старт включён по контракту —
        ensure-узел вне топ-K заменяет хвост выдачи (Ф2-PPR-05)."""
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        ranked = spreader.spread([S], iterations=25, top_k=3, ensure_ids=[S])
        assert len(ranked) == 3
        scores = [s for _, s in ranked]
        assert scores == sorted(scores, reverse=True)
        assert S in dict(ranked)
        # без ensure (чистый топ-K) старт на 4-м месте в выдачу не попадает
        plain = spreader.spread([S], iterations=25, top_k=3)
        assert S not in dict(plain)

    def test_ensure_noop_when_already_in_top(self):
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        with_ensure = spreader.spread([S], iterations=25, top_k=6, ensure_ids=[S])
        assert len(with_ensure) == 6
        assert dict(with_ensure)[S] == pytest.approx(0.15, abs=0.01)

    def test_weighted_edges(self):
        """Веса рёбер взвешивают переход (Ф2-PPR-07, оракул-сверка)."""
        weighted = [(S, A, 0.5), (S, B, 2.0), (A, C, 1.0), (B, C, 1.0)]
        spreader = ActivationSpreader(weighted, damping=0.85)
        got = dict(spreader.spread([S], iterations=15))
        ref = ppr_reference(weighted, [S], 0.85, 15)
        for node, expected in ref.items():
            assert got[node] == pytest.approx(expected, abs=1e-6), node
        # тяжёлое ребро перекачивает активацию в B-ветку
        assert got[B] > got[A]

    def test_isolated_node_zero_score(self):
        """Недостижимый узел: score=0, не NaN (Ф2-PPR-06)."""
        spreader = ActivationSpreader(GRAPH6 + [("orphan", A, 1.0)], damping=0.85)
        ranked = dict(spreader.spread([S], iterations=15))
        assert ranked["orphan"] == pytest.approx(0.0)
        assert all(not np.isnan(v) for v in ranked.values())

    def test_dead_end_loses_mass_no_nan(self):
        """Dangling-узел: масса теряется (Σr < 1), итерации стабильны."""
        spreader = ActivationSpreader([(S, A, 1.0)], damping=0.85)
        ranked = dict(spreader.spread([S], iterations=15))
        # стационар: S=0.15 (телепорт), A=0.85×r(S)=0.1275 — приток есть,
        # оттока нет; остальная масса утекла на dangling-переходах
        assert ranked[A] == pytest.approx(0.1275, abs=1e-6)
        assert ranked[S] == pytest.approx(0.15, abs=1e-6)
        assert sum(ranked.values()) == pytest.approx(0.2775, abs=1e-6)

    def test_directed_transitions(self):
        """Контракт направленности (Д7): рёбра A→B, запрос из B — A не
        достижим, активация не течёт против стрелок (Ф2-PPR-09)."""
        spreader = ActivationSpreader([(A, B, 1.0)], damping=0.85)
        ranked = dict(spreader.spread([B], iterations=15))
        assert ranked[A] == pytest.approx(0.0)
        assert ranked[B] == pytest.approx(0.15)  # только телепорт

    def test_unknown_seed_empty_result(self):
        """Старт вне графа: пустой результат, не исключение (Ф2-ERR-03)."""
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        assert spreader.spread(["ghost"]) == []

    def test_zero_and_negative_weights_skipped(self):
        spreader = ActivationSpreader([(S, A, 0.0), (S, B, -1.0), (S, C, 1.0)])
        ranked = dict(spreader.spread([S], iterations=3))
        assert ranked[A] == pytest.approx(0.0)
        assert ranked[B] == pytest.approx(0.0)
        assert ranked[S] == pytest.approx(0.15)  # только телепорт
        # единственный перенос S→C: на каждом шаге C получает 0.85·r_prev(S)
        assert ranked[C] == pytest.approx(0.85 * 0.15)

    def test_invalid_damping_rejected(self):
        with pytest.raises(ValueError):
            ActivationSpreader(GRAPH6, damping=0.0)
        with pytest.raises(ValueError):
            ActivationSpreader(GRAPH6, damping=1.0)


class TestServiceTraverseStrategy:
    """traverse(strategy=...): bfs-контракт нетронут, activation за флагом."""

    def _service(self, **cfg) -> MemoryService:
        repo = MagicMock()
        repo.fetch_activation_edges = AsyncMock(return_value=list(GRAPH6))
        repo.fetch_by_ids = AsyncMock(return_value=[
            {"id": gid, "content": f"content {gid}", "namespace": "default",
             "importance": 3}
            for gid in (S, A, B, C, D, E)
        ])
        repo.get_by_id = AsyncMock(return_value=MagicMock())
        repo.traverse = AsyncMock(return_value={"nodes": [], "edges": []})
        return MemoryService(
            repository=repo,
            embedding_provider=MagicMock(),
            namespace_repository=MagicMock(),
            runtime=activation_config(**cfg),
        ), repo

    @pytest.mark.asyncio
    async def test_activation_traverse_result_shape(self):
        service, repo = self._service()
        result = await service.traverse(S, strategy="activation")
        nodes = result.nodes
        assert result.edges == []
        assert len(nodes) == 6  # top_k=50 > graph6
        assert all("score" in n for n in nodes)
        assert all(n["score"] >= 0 for n in nodes)
        scores = [n["score"] for n in nodes]
        assert scores == sorted(scores, reverse=True)
        # граф загружен с ленивыми параметрами decay из конфига; симметричные
        # типы — из traverse_symmetric_link_types (дефолт related_to)
        repo.fetch_activation_edges.assert_awaited_once_with(
            0.02, 0.002, None, ["related_to"]
        )

    @pytest.mark.asyncio
    async def test_activation_disabled_explicit_error(self):
        """Флаг выключен: внятная ошибка, НЕ тихий fallback на bfs (Ф2-ERR-01)."""
        service, repo = self._service(traverse_activation_enabled=False)
        with pytest.raises(ValueError, match="traverse_activation_enabled"):
            await service.traverse(S, strategy="activation")
        repo.fetch_activation_edges.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_strategy_rejected(self):
        service, _ = self._service()
        with pytest.raises(ValueError, match="strategy"):
            await service.traverse(S, strategy="dfs")

    @pytest.mark.asyncio
    async def test_bfs_default_path_unchanged(self):
        """bfs (дефолт): прежний путь через хранимку, activation-выборки нет."""
        service, repo = self._service(traverse_activation_enabled=False)
        await service.traverse(S, depth=2)
        repo.traverse.assert_awaited_once()
        repo.fetch_activation_edges.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_activation_link_types_passthrough(self):
        service, repo = self._service()
        await service.traverse(S, strategy="activation", link_types=["related_to"])
        repo.fetch_activation_edges.assert_awaited_once_with(
            0.02, 0.002, ["related_to"], ["related_to"]
        )


class TestEdgeFlows:
    """flow(src→tgt) = r[src] × M[tgt,src] на финальном ранге (вердикт
    Эны 23.09): одна операция поверх посчитанного ранга."""

    def test_flow_formula_exact(self):
        """graph6: M[A,S]=1/2 (у S два исходящих), r[S]=0.15 → flow(S→A)=0.075;
        единственный выход C: flow(C→D)=r[C]×1.0 (25 итераций)."""
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        ranked = dict(spreader.spread([S], iterations=25))
        flows = {(s, t): f for s, t, f in spreader.edge_flows(ranked)}
        assert flows[(S, A)] == pytest.approx(0.15 * 0.5, abs=1e-9)
        assert flows[(S, B)] == pytest.approx(0.15 * 0.5, abs=1e-9)
        # ranked округлён до 6 знаков — допуск на round, не на математику
        assert flows[(C, D)] == pytest.approx(ranked[C], abs=1e-6)
        assert flows[(E, C)] == pytest.approx(ranked[E], abs=1e-6)

    def test_only_edges_with_both_ends_in_set(self):
        """Ребро вне набора (хотя бы один конец) в потоки не попадает."""
        spreader = ActivationSpreader(GRAPH6, damping=0.85)
        spreader.spread([S], iterations=25)
        flows = spreader.edge_flows({S, A})
        assert [(s, t) for s, t, _ in flows] == [(S, A)]

    def test_requires_prior_spread(self):
        with pytest.raises(ValueError, match="spread"):
            ActivationSpreader(GRAPH6, damping=0.85).edge_flows([S])

    def test_opposite_direction_duplicate_edges_sum(self):
        """Встречные дуги зеркал (UNION ALL) и явные двойные связи: дубли
        одной пары в CSR суммируются — 0.5+0.5 дают M=1.0, flow=r[A]×1.0
        (без суммирования было бы r[A]×0.5)."""
        spreader = ActivationSpreader([(A, B, 0.5), (A, B, 0.5)], damping=0.85)
        spreader.spread([A], iterations=5)
        flows = spreader.edge_flows([A, B])
        assert len(flows) == 1
        src, tgt, flow = flows[0]
        assert (src, tgt) == (A, B)
        assert flow == pytest.approx(0.15, abs=1e-9)


class TestActivationReinforceFlowFilter:
    """Рёбра-проводники (вердикт Эны 23.09): касаем только рёбра с обоими
    концами в топ-K и flow ≥ edge_reinforce_flow_min — один канонический
    батч на запрос; не C(k,2), как в search-хуке."""

    def _service(self, **cfg):
        repo = MagicMock()
        repo.fetch_activation_edges = AsyncMock(return_value=list(GRAPH6))
        repo.fetch_by_ids = AsyncMock(return_value=[
            {"id": gid, "content": f"content {gid}", "namespace": "default",
             "importance": 3}
            for gid in (S, A, B, C, D, E)
        ])
        dispatched: list[tuple[str, str]] = []
        return MemoryService(
            repository=repo,
            embedding_provider=MagicMock(),
            namespace_repository=MagicMock(),
            runtime=activation_config(**cfg),
            edge_dispatch=dispatched.append,
        ), dispatched

    @pytest.mark.asyncio
    async def test_top_k_window_gates_pairs(self):
        """top_k=4: выдача {C,D,E,S} (S и так 4-й по рангу) — касание только
        рёбер внутри окна (C→D, D→E, E→C); S→A, S→B, A→C, B→C с концами
        вне топа не касаются."""
        service, dispatched = self._service(traverse_activation_top_k=4)
        await service.traverse(S, strategy="activation")
        assert len(dispatched) == 1
        assert set(dispatched[0]) == {(C, D), (C, E), (D, E)}  # канонизированы

    @pytest.mark.asyncio
    async def test_flow_threshold_gates_pairs(self):
        """Порог выше максимального потока (max ≈ r[C] ≈ 0.28): ни одна
        пара не проходит — касаний нет вовсе."""
        service, dispatched = self._service(edge_reinforce_flow_min=0.5)
        await service.traverse(S, strategy="activation")
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_full_top_one_canonical_batch(self):
        """Полный топ: все 7 рёбер-проводников одним каноническим батчем
        (least/greatest, отсортирован) — одно касание на пару за запрос."""
        service, dispatched = self._service()
        await service.traverse(S, strategy="activation")
        assert len(dispatched) == 1
        assert dispatched[0] == [
            (A, C), (A, S), (B, C), (B, S), (C, D), (C, E), (D, E),
        ]
