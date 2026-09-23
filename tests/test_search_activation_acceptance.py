"""Приёмочный сьют Фазы 3 (волна 3): search(strategy="activation") —
ассоциативное расширение выдачи в старом туле memory_search.

Уровень ПРИЁМКИ: независимый PPR-оракул (плотная numpy-реализация, не
копия прод-кода ActivationSpreader), эталонный JSON-снапшот гибридной
выдачи (бит-в-бит прежний формат — без ключа activated), контракт
лимитов/ошибок и reinforce-хук финальной выдачи (C(k,2), одно касание).
Дублирование с tests/test_search_activation.py (юниты Соны) допустимо:
приёмка проверяет контракт, юниты — реализацию.
"""

import json
from datetime import datetime, timezone
from itertools import combinations
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from memory_server.config import Settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.memory.service import MemoryService

SEED_A = "aaaaaaaa-0000-0000-0000-000000000001"
SEED_B = "aaaaaaaa-0000-0000-0000-000000000002"
NEIGHBOR_C = "aaaaaaaa-0000-0000-0000-000000000003"  # связан с A и B — ранг выше
NEIGHBOR_D = "aaaaaaaa-0000-0000-0000-000000000004"  # связан только с A

# Живой граф для PPR: C — двойная связность (из A и из B), D — одна дуга.
GRAPH = [
    (SEED_A, NEIGHBOR_C, 1.0),
    (NEIGHBOR_C, SEED_A, 1.0),
    (SEED_B, NEIGHBOR_C, 1.0),
    (SEED_A, NEIGHBOR_D, 0.5),
]

DAMPING = 0.85
ITERATIONS = 25


def _iso(dt: datetime) -> str:
    """ISO-строка в формате pydantic mode="json" (UTC → Z, не +00:00)."""
    return dt.isoformat().replace("+00:00", "Z")


def ppr_reference(edges, seed_ids, damping, iterations) -> dict[str, float]:
    """Независимый оракул: плотная numpy-реализация PPR (не копия прод-кода)."""
    nodes = sorted({u for u, _, _ in edges} | {v for _, v, _ in edges})
    idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)
    out_w = np.zeros(n)
    for u, v, w in edges:
        out_w[idx[u]] += w
    P = np.zeros((n, n))
    for u, v, w in edges:
        if out_w[idx[u]] > 0:
            P[idx[v], idx[u]] += w / out_w[idx[u]]
    e = np.zeros(n)
    for s in seed_ids:
        e[idx[s]] = 1.0
    e /= e.sum()
    r = e.copy()
    for _ in range(iterations):
        r = damping * (P @ r) + (1.0 - damping) * e
    return {node: float(r[idx[node]]) for node in nodes}


def _candidate(cid: str, *, rank_dense: int, moment: datetime) -> object:
    """HybridCandidate фазы 1 (векторов нет — MMR не искажает порядок)."""
    from memory_server.memory.search_fusion import HybridCandidate

    return HybridCandidate(
        id=cid, content=f"content {cid[-1]}", metadata={}, namespace="hybridns",
        importance=3, project_id=None, status="asserted",
        created_at=moment, last_accessed_at=moment, frozen=False,
        rank_dense=rank_dense, rank_fts=None, vector=None,
    )


def _card(cid: str, moment: datetime) -> dict:
    """Карточка fetch_by_ids — проекция _MEMORY_COLUMNS."""
    return {
        "id": cid, "user_id": "u1", "content": f"content {cid[-1]}", "metadata": {},
        "importance": 3, "created_at": moment, "updated_at": moment,
        "content_hash": None, "project_id": None, "status": "asserted",
        "confidence": 1.0, "valid_from": moment, "valid_to": None,
        "ingested_at": moment, "namespace": "hybridns", "supersedes": None,
        "superseded_by": None, "frozen": False, "last_accessed_at": moment,
        "access_count": 0,
    }


def activation_service(
    seeds: list | None = None,
    cards: list[dict] | None = None,
    dispatch: MagicMock | None = None,
    **cfg,
) -> tuple[MemoryService, MagicMock]:
    """Сервис с включённым флагом и мок-репозиторием графа/карточек.

    Время карточек — now(): days_since=0 → decay=1.0, importance=3 →
    weight=1.0, score = чистый RRF — детерминизм эталонного снапшота без
    хрупких «плывущих» чисел."""
    overrides = {
        "dedup_enabled": False,
        "hybrid_search_enabled": True,
        "search_activation_enabled": True,
    }
    overrides.update(cfg)
    config = RuntimeConfig(db_values=overrides)
    repo = MagicMock()
    moment = datetime.now(timezone.utc)
    if seeds is None:
        seeds = [
            _candidate(SEED_A, rank_dense=0, moment=moment),
            _candidate(SEED_B, rank_dense=1, moment=moment),
        ]
    if cards is None:
        cards = [_card(NEIGHBOR_C, moment), _card(NEIGHBOR_D, moment)]
    repo.search_hybrid = AsyncMock(return_value=seeds)
    repo.fetch_activation_edges = AsyncMock(return_value=list(GRAPH))
    repo.fetch_by_ids = AsyncMock(return_value=cards)
    repo.bump_access = AsyncMock()
    service = MemoryService(
        repository=repo,
        embedding_provider=MagicMock(),
        namespace_repository=MagicMock(),
        runtime=config,
        project_repository=MagicMock(),
        edge_dispatch=dispatch,
    )
    service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    return service, repo


# ══════════════════════════════════════════════════════════════════
# Сценарий 4: seed → расширение, лимиты, ошибки
# ══════════════════════════════════════════════════════════════════


class TestSearchActivationAcceptance:
    @pytest.mark.asyncio
    async def test_seed_expansion_matches_independent_ppr_oracle(self):
        """Seed'ы гибрида + активированные соседи (activated=true) с рангами,
        совпадающими с НЕЗАВИСИМЫМ PPR-оракулом: C (двойная связность)
        выше D; seed'ы не помечены."""
        service, _ = activation_service()

        results = await service.search(query="q", limit=4, strategy="activation")

        assert [r.id for r in results] == [SEED_A, SEED_B, NEIGHBOR_C, NEIGHBOR_D]
        assert [r.activated for r in results] == [False, False, True, True]
        oracle = ppr_reference(GRAPH, [SEED_A, SEED_B], DAMPING, ITERATIONS)
        assert results[2].score == pytest.approx(oracle[NEIGHBOR_C], abs=1e-5)
        assert results[3].score == pytest.approx(oracle[NEIGHBOR_D], abs=1e-5)
        assert results[2].score > results[3].score > 0.0

    @pytest.mark.asyncio
    async def test_activated_neighbors_not_lost_full_fetch(self):
        """Активированные не-сееды не теряются: карточки запрашиваются ровно
        для всех активированных (в порядке PPR-ранга), каждый попадает в
        выдачу с собственным контентом."""
        service, repo = activation_service()

        results = await service.search(query="q", limit=4, strategy="activation")

        repo.fetch_by_ids.assert_awaited_once_with([NEIGHBOR_C, NEIGHBOR_D])
        activated = [r for r in results if r.activated]
        assert [r.id for r in activated] == [NEIGHBOR_C, NEIGHBOR_D]
        assert activated[0].content == f"content {NEIGHBOR_C[-1]}"
        assert activated[1].content == f"content {NEIGHBOR_D[-1]}"

    @pytest.mark.asyncio
    async def test_total_topk_limit_respected(self):
        """Общий топ-K = limit: при 2 seed'ах и limit=3 в выдачу идёт ровно
        один активированный (лучший по рангу), второй не протекает."""
        service, repo = activation_service()

        results = await service.search(query="q", limit=3, strategy="activation")

        assert len(results) == 3
        assert results[2].id == NEIGHBOR_C and results[2].activated
        repo.fetch_by_ids.assert_awaited_once_with([NEIGHBOR_C])

    @pytest.mark.asyncio
    async def test_offset_rejected_for_activation(self):
        """offset>0 + activation → ValueError: пагинация несовместима с
        расширением (расширение зависит от полного контекста выдачи)."""
        service, repo = activation_service()

        with pytest.raises(ValueError, match="offset"):
            await service.search(query="q", offset=1, strategy="activation")
        repo.search_hybrid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_flag_explicit_error_not_silent_fallback(self):
        """search_activation_enabled=False → ValueError; гибридный поиск НЕ
        вызывается (это явный отказ, а не тихий fallback на hybrid)."""
        service, repo = activation_service(search_activation_enabled=False)

        with pytest.raises(ValueError, match="search_activation_enabled"):
            await service.search(query="q", strategy="activation")
        repo.search_hybrid.assert_not_awaited()
        repo.fetch_activation_edges.assert_not_awaited()


class TestHybridBitwiseSnapshot:
    """Гибридная выдача бит-в-бит прежняя: эталонный JSON без ключа
    activated (регрессия формата недопустима — консьюмеры таски).

    Синхронные тесты: таска поднимает собственный event-loop (run_async),
    внутри pytest-asyncio это конфликт — вызов строго без asyncio-маркера."""

    def test_hybrid_payload_matches_reference_json_snapshot(self, monkeypatch):
        """Дефолтная стратегия: payload таски search_memories равен эталонному
        JSON (все поля, score = чистый RRF при decay=weight=1.0), ключа
        activated нет ни в одной записи."""
        import memory_server.tasks.memory_tasks as mt
        from memory_server.tasks.memory_tasks import search_memories

        # seeds собраны на известный момент: эталон даты строится из него же
        moment = datetime.now(timezone.utc)
        seeds = [
            _candidate(SEED_A, rank_dense=0, moment=moment),
            _candidate(SEED_B, rank_dense=1, moment=moment),
        ]
        service, _ = activation_service(seeds=seeds)
        monkeypatch.setattr(mt, "_get_service", lambda: service)

        payloads = search_memories(query="q", limit=10)

        runtime = service.runtime
        rrf_k = runtime.get("rrf_k")
        rrf_a = 1.0 / (rrf_k + 0)  # rank_dense=0
        rrf_b = 1.0 / (rrf_k + 1)  # rank_dense=1
        iso = _iso(moment)
        expected = [
            {
                "id": SEED_A, "content": f"content {SEED_A[-1]}", "metadata": {},
                "importance": 3, "score": round(rrf_a, 6), "project_id": None,
                "status": "asserted", "namespace": "hybridns",
                "created_at": iso, "last_accessed_at": iso, "frozen": False,
                "score_rrf": round(rrf_a, 6), "score_decay": 1.0,
                "score_importance": 1.0,
            },
            {
                "id": SEED_B, "content": f"content {SEED_B[-1]}", "metadata": {},
                "importance": 3, "score": round(rrf_b, 6), "project_id": None,
                "status": "asserted", "namespace": "hybridns",
                "created_at": iso, "last_accessed_at": iso, "frozen": False,
                "score_rrf": round(rrf_b, 6), "score_decay": 1.0,
                "score_importance": 1.0,
            },
        ]
        # Бит-в-бит: нормализованные JSON-строки совпадают с эталоном
        assert json.dumps(payloads, sort_keys=True) == json.dumps(expected, sort_keys=True)
        # и ключа activated нет вовсе — формат не расширился молча
        assert "activated" not in json.dumps(payloads)

    def test_activation_payload_marks_only_expansions(self, monkeypatch):
        """strategy=activation: активированные несут activated=true, seed'ы —
        БЕЗ ключа (тот же exclude-контракт сериализации)."""
        import memory_server.tasks.memory_tasks as mt
        from memory_server.tasks.memory_tasks import search_memories

        service, _ = activation_service()
        monkeypatch.setattr(mt, "_get_service", lambda: service)

        payloads = search_memories(query="q", limit=4, strategy="activation")

        assert len(payloads) == 4
        assert "activated" not in payloads[0]
        assert "activated" not in payloads[1]
        assert payloads[2]["activated"] is True
        assert payloads[3]["activated"] is True


class TestReinforceHook:
    """Reinforce-хук финальной выдачи: все C(k,2) пар seed+активированных,
    ровно ОДНО касание на вызов поиска (без двойного dispatch)."""

    @pytest.mark.asyncio
    async def test_activation_dispatches_single_c_k2_reinforce(self):
        """k=4 (2 seed + 2 активированных) → один dispatch с 6 парами —
        все сочетания финальной выдачи, включая seed×активированный."""
        dispatch = MagicMock()
        service, _ = activation_service(dispatch=dispatch)

        results = await service.search(query="q", limit=4, strategy="activation")

        dispatch.assert_called_once()
        pairs = dispatch.call_args.args[0]
        expected = [
            (a.id, b.id) for a, b in combinations(results, 2)
        ]
        assert pairs == expected
        assert len(pairs) == 6  # C(4,2)

    @pytest.mark.asyncio
    async def test_hybrid_path_reinforce_unchanged_single_dispatch(self):
        """Гибридный путь: контракт прежний — один dispatch на C(k,2) пар
        выдачи (activation не сломал существующий хук)."""
        dispatch = MagicMock()
        service, _ = activation_service(dispatch=dispatch)

        results = await service.search(query="q", limit=10)

        dispatch.assert_called_once()
        pairs = dispatch.call_args.args[0]
        assert pairs == [(a.id, b.id) for a, b in combinations(results, 2)]
