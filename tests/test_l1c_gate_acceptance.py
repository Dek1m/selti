"""Приёмочный сьют Фазы 3 (волна 3): L1c-гейт, mutual-reinforce, кампания
prune_cooccurrence_history — тест-план ред. 2, Ф3-блок.

Уровень ПРИЁМКИ: контракт кампании целиком (не реализация Соны из
test_l1c_gate.py — там юниты на мок-пуле; здесь эталонные оракулы,
SQL-инварианты на тексты констант и сквозные сценарии деплоя).

Ключевой регресс (вердикт Эны): l1c_done у гранулы со всеми отсеянными
соседями ДОЛЖЕН записываться — иначе гранула вечно крутится в beat-выборке
кампании co_occurrence. Fail-closed гейта — обратное: маркер НЕ пишется,
гранула возвращается на ретрай после поднятия Qdrant.
"""

import math
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.memory.linker import Linker
from memory_server.metrics import LINKER_L1C_GATE_FAILURES_TOTAL

GID = "11111111-1111-1111-1111-111111111111"
NEAR = "22222222-2222-2222-2222-222222222222"   # коллинеарен источнику, cos=0.8/0.8→1.0
ABOVE = "33333333-3333-3333-3333-333333333333"  # cos чуть выше порога 0.30
BELOW = "44444444-4444-4444-4444-444444444444"  # cos чуть ниже порога 0.30
FAR = "77777777-7777-7777-7777-777777777777"    # ортогонален источнику, cos=0.0
EDGE_STRONG = "55555555-5555-5555-5555-555555555555"
EDGE_WEAK = "66666666-6666-6666-6666-666666666666"
BOUND65 = "88888888-8888-8888-8888-888888888888"   # cos к источнику ровно 0.65
UNDER65 = "99999999-9999-9999-9999-999999999999"   # cos 0.6499 — ниже порога 0.65

_VEC_SRC = [1.0, 0.0]
_VEC_NEAR = [0.8, 0.0]
_VEC_FAR = [0.0, 1.0]
# Единичные векторы [g, sqrt(1-g^2)]: косинус к источнику = g с точностью
# float — граница порога проверяется с запасом ±1e-6 (не flaky на округлении)
_VEC_ABOVE = [0.30 + 1e-6, math.sqrt(1.0 - (0.30 + 1e-6) ** 2)]
_VEC_BELOW = [0.30 - 1e-6, math.sqrt(1.0 - (0.30 - 1e-6) ** 2)]
# Граница вердикта Эны 0.65 — ТОЧНАЯ, без запаса: float64-косинус этого
# вектора бит-в-бит равен литералу 0.65 (доказательство — в докстринге
# test_threshold_065_boundary_inclusive_exact_passes_just_below_rejected)
_VEC_BOUND65 = [0.65, math.sqrt(1.0 - 0.65 ** 2)]
_VEC_UNDER65 = [0.6499, math.sqrt(1.0 - 0.6499 ** 2)]


def gate_config(**overrides) -> Settings:
    base = {
        "dedup_enabled": False,
        "hybrid_search_enabled": False,
        "linker_l1c_enabled": True,
        "linker_l1c_gate_min": 0.30,
    }
    base.update(overrides)
    return Settings(**base)


def make_linker(mock_pool, qdrant=None, **cfg) -> Linker:
    return Linker(
        pool=mock_pool, qdrant=qdrant, redis_provider=None, config=gate_config(**cfg)
    )


def gate_qdrant(vectors: dict[str, list[float]] | None = None) -> MagicMock:
    """QdrantStore-мок: retrieve_vectors отдаёт заготовленные векторы."""
    if vectors is None:
        vectors = {
            GID: _VEC_SRC, NEAR: _VEC_NEAR, ABOVE: _VEC_ABOVE,
            BELOW: _VEC_BELOW, FAR: _VEC_FAR,
        }
    qdrant = MagicMock()
    qdrant.retrieve_vectors = MagicMock(return_value=vectors)
    return qdrant


def campaign_rows() -> list[dict]:
    """Батч исторических l1c-кандидатов: сильная пара (коллинеарная) и
    слабая (ортогональная — round(cos,4)=0.0 не съедает margin отчёта)."""
    now = datetime.now(timezone.utc)
    return [
        {
            "id": EDGE_STRONG, "source_id": GID, "target_id": NEAR,
            "src_cluster": "c-main", "tgt_cluster": "c-main", "created_at": now,
        },
        {
            "id": EDGE_WEAK, "source_id": GID, "target_id": FAR,
            "src_cluster": "c-main", "tgt_cluster": "c-main", "created_at": now,
        },
    ]


def neighbors(*ids: str) -> list[dict]:
    return [{"id": nid} for nid in ids]


# ══════════════════════════════════════════════════════════════════
# Сценарий 1: гейт — порог, off, fail-closed, точечный отсеив
# ══════════════════════════════════════════════════════════════════


class TestGateAcceptance:
    @pytest.mark.asyncio
    async def test_threshold_boundary_above_passes_below_rejected(self, mock_pool):
        """Граница порога 0.30: косинус выше на 1e-6 проходит в INSERT,
        ниже на 1e-6 — отсеян (сравнение >=, не >)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=neighbors(ABOVE, BELOW))
        conn.fetchrow = AsyncMock(return_value={"created": 1, "reinforced": 0})
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome["fail_closed"] is False
        assert conn.fetchrow.await_args_list[0].args[2] == [ABOVE]

    @pytest.mark.asyncio
    async def test_threshold_065_boundary_inclusive_exact_passes_just_below_rejected(
        self, mock_pool
    ):
        """Вердикт Эны (деплой 22.09): LINKER_L1C_GATE_MIN=0.65. Граница
        ИНКЛЮЗИВНАЯ (>=): пара одной сессии с косинусом ровно 0.6500 →
        ребро создаётся; 0.6499 → сосед отсеян гейтом (ребро не рождается,
        в prune-кампании такие пары — кандидаты на смерть).

        float-погрешность на границе ОТСУТСТВУЕТ (фактическое поведение,
        проверено численно; сравнение — numpy float64, linker.py:352):
        dot источника [1.0, 0.0] с [0.65, sqrt(0.5775)] даёт бит-в-бит
        литерал float64(0.65), а норма соседа округляется ровно к 1.0 —
        вычисленный косинус == gate, >= держится без ULP-виляний. У 0.6499
        фактический косинус 0.6499000000000001: зазор ~1e-4 против ULP
        ~1e-16 — отсев детерминирован. Оракул ниже фиксирует это в самом
        тесте: если на другой платформе факт-косинус просел под gate,
        assert оракула отличит float-сдвиг границы от бага сравнения.

        Мост с косинусом 0.40 отдельно НЕ тестируем: иммунитет моста — не
        граница, он исключён из prune-выборки ДО оценки косинуса — покрыт
        test_bridges_immune_never_selected_for_prune ниже по файлу."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=neighbors(BOUND65, UNDER65))
        conn.fetchrow = AsyncMock(return_value={"created": 1, "reinforced": 0})
        conn.execute = AsyncMock()
        linker = make_linker(
            mock_pool,
            qdrant=gate_qdrant(
                vectors={GID: _VEC_SRC, BOUND65: _VEC_BOUND65, UNDER65: _VEC_UNDER65}
            ),
            linker_l1c_gate_min=0.65,
        )

        # Оракул: фактический float64-косинус BOUND65 не ниже порога
        gate = linker.config.linker_l1c_gate_min
        src_arr = np.asarray(_VEC_SRC, dtype=np.float64)
        arr = np.asarray(_VEC_BOUND65, dtype=np.float64)
        actual = float(src_arr @ arr) / (
            float(np.linalg.norm(src_arr)) * float(np.linalg.norm(arr))
        )
        assert actual == gate  # бит-в-бит граница (см. докстринг)

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 1, "reinforced": 0, "fail_closed": False}
        # В INSERT уходит ТОЛЬКО граничный сосед; 0.6499 отсеян гейтом
        assert conn.fetchrow.await_args_list[0].args[2] == [BOUND65]
        assert conn.execute.await_args_list[0].args[0] == q.MARK_L1C_DONE

    @pytest.mark.asyncio
    async def test_gate_zero_off_all_neighbors_linked_without_qdrant(self, mock_pool):
        """Гейт 0.0 = выключен: все соседи линкуются, Qdrant не дёргается
        вовсе (экономия батча retrieve на каждой грануле сессии)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=neighbors(NEAR, ABOVE, BELOW))
        conn.fetchrow = AsyncMock(return_value={"created": 3, "reinforced": 0})
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, qdrant=gate_qdrant(), linker_l1c_gate_min=0.0)

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 3, "reinforced": 0, "fail_closed": False}
        linker.qdrant.retrieve_vectors.assert_not_called()
        assert conn.fetchrow.await_args_list[0].args[2] == [NEAR, ABOVE, BELOW]

    @pytest.mark.asyncio
    async def test_qdrant_down_zero_edges_marker_withheld_retry_keeps_granule(
        self, mock_pool
    ):
        """Qdrant недоступен → fail-closed: 0 рёбер, l1c_done НЕ записан.
        Ретрай: выборка кампании отсекает ТОЛЬКО по l1c_done/рёбрам — гранула
        без маркера вернётся следующим прогоном (не теряется молча)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=neighbors(NEAR))
        conn.fetchrow = AsyncMock()
        conn.execute = AsyncMock()
        qdrant = gate_qdrant()
        qdrant.retrieve_vectors = MagicMock(side_effect=RuntimeError("conn refused"))
        linker = make_linker(mock_pool, qdrant=qdrant)
        before = LINKER_L1C_GATE_FAILURES_TOTAL._value.get()

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 0, "reinforced": 0, "fail_closed": True}
        conn.fetchrow.assert_not_awaited()   # INSERT не вызван
        conn.execute.assert_not_awaited()    # l1c_done НЕ записан
        assert LINKER_L1C_GATE_FAILURES_TOTAL._value.get() == before + 1
        # Ретрай-инвариант: единственный маркерный фильтр выборки — l1c_done;
        # гранула fail-closed (без маркера, без рёбер, с соседями) видима кампании
        sql = q.SELECT_COOCCURRENCE_CANDIDATES
        assert "l1c_done" in sql
        assert "session_id" in sql
        assert not re.search(r"l1c_gate", sql), "выборка не знает про гейт"

    @pytest.mark.asyncio
    async def test_missing_source_vector_fail_closed_batch_withheld(self, mock_pool):
        """Вектора источника нет (PG↔Qdrant рассинхрон) → весь батч
        fail-closed: без оценки источника ни одна пара не рождается."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=neighbors(NEAR, ABOVE))
        conn.fetchrow = AsyncMock()
        conn.execute = AsyncMock()
        qdrant = gate_qdrant(vectors={NEAR: _VEC_NEAR, ABOVE: _VEC_ABOVE})
        linker = make_linker(mock_pool, qdrant=qdrant)
        before = LINKER_L1C_GATE_FAILURES_TOTAL._value.get()

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome["fail_closed"] is True
        conn.fetchrow.assert_not_awaited()
        conn.execute.assert_not_awaited()
        assert LINKER_L1C_GATE_FAILURES_TOTAL._value.get() == before + 1

    @pytest.mark.asyncio
    async def test_neighbor_without_vector_dropped_pointwise_others_inserted(
        self, mock_pool
    ):
        """Сосед без вектора — отсеивается ТОЧЕЧНО (не отказ батча):
        остальные соседи проходят до INSERT, маркер пишется."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=neighbors(NEAR, BELOW))
        conn.fetchrow = AsyncMock(return_value={"created": 1, "reinforced": 0})
        conn.execute = AsyncMock()
        # BELOW без вектора; NEAR коллинеарен — прошёл бы и с вектором
        qdrant = gate_qdrant(vectors={GID: _VEC_SRC, NEAR: _VEC_NEAR})
        linker = make_linker(mock_pool, qdrant=qdrant)

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 1, "reinforced": 0, "fail_closed": False}
        assert conn.fetchrow.await_args_list[0].args[2] == [NEAR]
        assert conn.execute.await_args_list[0].args[0] == q.MARK_L1C_DONE

    @pytest.mark.asyncio
    async def test_all_neighbors_filtered_marker_written_leaves_beat_sample(
        self, mock_pool
    ):
        """КЛЮЧЕВОЙ РЕГРЕСС (вердикт Эны): все соседи отсеяны гейтом →
        l1c_done ЗАПИСАН и гранула покидает beat-выборку — вечный цикл
        закрыт; INSERT с пустым батчем не выполняется вовсе."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=neighbors(BELOW))
        conn.fetchrow = AsyncMock()
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 0, "reinforced": 0, "fail_closed": False}
        conn.fetchrow.assert_not_awaited()  # пустой батч — INSERT пропущен
        assert conn.execute.await_args_list[0].args[0] == q.MARK_L1C_DONE
        # Инвариант выборки: маркер исключает гранулу из beat-выборки
        assert (
            "AND NOT COALESCE((m.metadata->>'l1c_done')::boolean, false)"
            in q.SELECT_COOCCURRENCE_CANDIDATES
        )


# ══════════════════════════════════════════════════════════════════
# Сценарий 2: mutual-reinforce — обратная пара, fresh-пара, без дублей
# ══════════════════════════════════════════════════════════════════


class TestMutualReinforceAcceptance:
    def test_reinforced_cte_matches_both_directions(self):
        """Существующая пара в ОБРАТНОМ направлении (neighbor→granule)
        матчится reinforced-CTE — повторная встреча в сессии усиливает
        существующее ребро, а не создаёт встречный дубль."""
        sql = q.INSERT_COOCCURRENCE_LINKS
        reinforced = sql.split("reinforced AS (")[1]
        assert "(r.source_id = $1::uuid AND r.target_id = c.neighbor_id)" in reinforced
        assert "(r.source_id = c.neighbor_id AND r.target_id = $1::uuid)" in reinforced
        # Хебб: +1 использование, якорь сейчас, вес к 1.0 по α
        assert "used_count   = r.used_count + 1" in reinforced
        assert "last_used_at = now()" in reinforced
        assert "LEAST(1.0, r.weight + (1.0 - r.weight) * $3::float8)" in reinforced

    def test_insert_branch_excludes_both_directions_no_counter_duplicates(self):
        """Свежая пара вставляется; пара, существующая в ЛЮБОМ направлении,
        до INSERT не доходит (NOT EXISTS OR-матч) — встречных related_to-
        дублей кампания не плодит; DO NOTHING гасит гонку partial-индекса."""
        sql = q.INSERT_COOCCURRENCE_LINKS
        insert_branch = sql.split("reinforced AS (")[0]
        assert "NOT EXISTS" in insert_branch
        assert "(r.source_id = $1::uuid AND r.target_id = c.neighbor_id)" in insert_branch
        assert "(r.source_id = c.neighbor_id AND r.target_id = $1::uuid)" in insert_branch
        assert "DO NOTHING" in insert_branch

    @pytest.mark.asyncio
    async def test_reverse_existing_pair_flows_through_reinforce_not_duplicate(
        self, mock_pool
    ):
        """Сквозной сценарий: сосед с существующим обратным ребром проходит
        гейт и уходит в один INSERT-вызов — решение create-vs-reinforce
        принимает SQL по факту состояния, α передаётся из конфигурации."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=neighbors(NEAR))
        conn.fetchrow = AsyncMock(return_value={"created": 0, "reinforced": 1})
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 0, "reinforced": 1, "fail_closed": False}
        args = conn.fetchrow.await_args_list[0].args
        assert args[0] == q.INSERT_COOCCURRENCE_LINKS
        assert args[2] == [NEAR]
        assert args[3] == linker.config.edge_reinforce_alpha

    def test_fresh_edge_metadata_contract(self):
        """Fresh-пара → обычная l1c-вставка: владение linker_v3/layer l1c,
        сессия происхождения в metadata (наблюдаемость кампании чистки)."""
        sql = q.INSERT_COOCCURRENCE_LINKS
        assert "'related_to'" in sql
        assert "0.5" in sql
        assert (
            "jsonb_build_object('source', 'linker_v3', 'layer', 'l1c',\n"
            "                                  'session_id', $4)" in sql
            or "'session_id', $4" in sql
        )


# ══════════════════════════════════════════════════════════════════
# Сценарий 3: кампания prune_cooccurrence_history
# ══════════════════════════════════════════════════════════════════


class TestPruneCampaignAcceptance:
    @pytest.mark.asyncio
    async def test_dry_run_report_only_zero_pruned_at(self, mock_pool):
        """dry_run: полный отчёт (кандидаты/выжившие/погибшие/кластеры/
        примеры с косинусом), ноль мутаций — только SELECT-выборки,
        PRUNE_EDGES_APPLY не выполняется ни разу."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(side_effect=[campaign_rows(), []])
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        report = await linker.run_prune_cooccurrence_history(dry_run=True)

        assert report["ok"] is True
        assert report["dry_run"] is True
        assert report["scanned"] == 2
        assert report["survived"] == 1
        assert report["would_prune"] == 1
        assert "pruned" not in report
        assert report["clusters"] == {"c-main": 1}
        assert report["examples"][0]["edge_id"] == EDGE_WEAK
        assert report["examples"][0]["cosine"] < 0.30
        for call in conn.fetch.await_args_list:
            assert call.args[0] == q.SELECT_COOCCURRENCE_PRUNE_CANDIDATES
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_live_run_noop_on_pruned_state(self, mock_pool):
        """Идемпотентность повторного боя: после первого прогона слабое
        ребро уже pruned (выборка его не вернёт — pruned_at IS NULL),
        повторный прогон сканирует только выживших и НЕ мутирует."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        # Прогон 1: батч(сильное+слабое) → APPLY(слабое) → батч пусто.
        # Прогон 2 (после прунинга): батч(только сильное) → батч пусто.
        strong_only = [campaign_rows()[0]]
        conn.fetch = AsyncMock(
            side_effect=[campaign_rows(), [{"id": EDGE_WEAK}], [], strong_only, []]
        )
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        first = await linker.run_prune_cooccurrence_history(dry_run=False)
        second = await linker.run_prune_cooccurrence_history(dry_run=False)

        assert first["pruned"] == 1 and first["survived"] == 1
        assert second["scanned"] == 1
        assert second["survived"] == 1
        assert second["pruned"] == 0
        # APPLY вызван ровно один раз за два прогона — повтор no-op по записи
        apply_calls = [
            c for c in conn.fetch.await_args_list if c.args[0] == q.PRUNE_EDGES_APPLY
        ]
        assert len(apply_calls) == 1
        assert [str(e) for e in apply_calls[0].args[1]] == [EDGE_WEAK]

    def test_bridges_immune_never_selected_for_prune(self):
        """Мосты между кластерами (оба cluster_id NOT NULL и разные) —
        иммунитет (вердикт Эны): выборка кампании исключает их ДО оценки,
        межкластерное ребро не может быть прунено этой кампанией."""
        sql = q.SELECT_COOCCURRENCE_PRUNE_CANDIDATES
        assert (
            "AND NOT (\n"
            "          src.cluster_id IS NOT NULL\n"
            "          AND tgt.cluster_id IS NOT NULL\n"
            "          AND src.cluster_id IS DISTINCT FROM tgt.cluster_id\n"
            "      )" in sql
        )
        # NULL-кластер мостом НЕ является (Д4): попадает в обычную обработку
        assert "IS DISTINCT FROM" in sql

    @pytest.mark.asyncio
    async def test_qdrant_down_ok_false_zero_mutations(self, mock_pool):
        """Qdrant недоступен → ok=False БЕЗ прунинга: отказ до первой
        мутации (иначе при массовом отказе погибли бы все неоценимые).
        Вариант A: qdrant не сконфигурирован — отказ до выборки вовсе."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock()
        linker = make_linker(mock_pool, qdrant=None)  # gate 0.30 > 0

        report = await linker.run_prune_cooccurrence_history(dry_run=False)

        assert report["ok"] is False
        assert report["reason"] == "qdrant_not_configured"
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_qdrant_down_mid_campaign_aborts_without_apply(self, mock_pool):
        """Вариант B: Qdrant падает на первом же батче retrieve — кампания
        прерывается, PRUNE_EDGES_APPLY не вызван (мутаций ноль)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(side_effect=[campaign_rows(), []])
        qdrant = gate_qdrant()
        qdrant.retrieve_vectors = MagicMock(side_effect=RuntimeError("down"))
        linker = make_linker(mock_pool, qdrant=qdrant)

        report = await linker.run_prune_cooccurrence_history(dry_run=False)

        assert report["ok"] is False
        assert "qdrant_unavailable" in report["reason"]
        for call in conn.fetch.await_args_list:
            assert call.args[0] == q.SELECT_COOCCURRENCE_PRUNE_CANDIDATES
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cursor_advances_by_stable_key_not_lost_on_live_mutations(
        self, mock_pool
    ):
        """Курсор не теряет рёбра при live-мутациях: ключ — стабильные
        (created_at, id); pruned_at-мутация их не трогает. Второй батч
        запрашивается строго ПОСЛЕ последней строки первого (курсорные
        аргументы $2/$3), одинаковые created_at развязываются id."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        batch1 = campaign_rows()
        batch2 = [campaign_rows()[0]]  # выживший после прунинга слабого
        conn.fetch = AsyncMock(side_effect=[batch1, [{"id": EDGE_WEAK}], batch2, []])
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        await linker.run_prune_cooccurrence_history(dry_run=False)

        select_calls = [
            c for c in conn.fetch.await_args_list
            if c.args[0] == q.SELECT_COOCCURRENCE_PRUNE_CANDIDATES
        ]
        assert len(select_calls) == 3  # батч1, батч2, контрольный пустой
        first, second = select_calls[0], select_calls[1]
        # сигнатура SELECT: ($1 batch, $2 cursor_ts, $3 cursor_id)
        assert first.args[2] is None and first.args[3] is None
        # второй батч: курсор = (created_at, id) последней строки первого
        assert second.args[2] == batch1[-1]["created_at"]
        assert second.args[3] == batch1[-1]["id"]
        # SQL-инвариант: курсор и сортировка — по одному стабильному ключу
        sql = q.SELECT_COOCCURRENCE_PRUNE_CANDIDATES
        assert "(r.created_at, r.id) > ($2::timestamptz, $3::uuid)" in sql
        assert "ORDER BY r.created_at, r.id" in sql
        # применяемая мутация не касается ключей курсора — только pruned_at
        assert q.PRUNE_EDGES_APPLY.count("SET") == 1
        assert "pruned_at = now()" in q.PRUNE_EDGES_APPLY
        assert "created_at" not in q.PRUNE_EDGES_APPLY

    def test_task_deploy_contract(self):
        """Celery-контракт one-off кампании: dry_run=True по умолчанию
        (первое касание прода — только отчёт), ретраев нет (отказ виден
        человеку сразу), очередь memory, лимит времени 1800/2400с."""
        from memory_server.tasks.linker_tasks import prune_cooccurrence_history

        task = prune_cooccurrence_history
        assert task.name == "memory_server.tasks.linker_tasks.prune_cooccurrence_history"
        sig = task.run
        import inspect

        assert inspect.signature(sig).parameters["dry_run"].default is True
        assert task.max_retries == 0
        assert task.soft_time_limit == 1800 and task.time_limit == 2400
