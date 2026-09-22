"""Фаза 3 (волна 3 V3.5): L1c-гейт + l1c_done + mutual-reinforce + кампания
чистки истории co-occurrence.

Гейт: одна сессия ≠ смысловая близость — пара проходит в Qdrant (батч
retrieve source+соседи), непрошедшие порог не доходят до SQL. Fail-closed:
Qdrant недоступен / нет вектора источника → рёбер нет, l1c_done НЕ пишется
(гранула вернётся на ретрай), метрика l1c_gate_failures растёт. l1c_done:
маркер пишется и при нулевых вставках (все соседи отсеяны), выборка кампании
его уважает. Mutual: существующая пара в любом направлении — reinforce-касание
(used_count+1, last_used_at, weight → 1.0 по α), встречные дубли не плодятся.
Кампания prune_cooccurrence_history: ретроспективный гейт исторических l1c
(81k singleton), pruned_at вместо DELETE, мосты между кластерами иммунны,
dry_run default, идемпотентность. Живой БД нет — SQL-контракты инвариантами
на текстах констант, поведение — юнитами на mock_pool/MagicMock (паттерн
test_linker_v3).
"""

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.memory.linker import Linker, _cosine_similarity
from memory_server.metrics import LINKER_L1C_GATE_FAILURES_TOTAL

GID = "11111111-1111-1111-1111-111111111111"
NEAR = "22222222-2222-2222-2222-222222222222"  # косинус к источнику ~1.0
FAR = "33333333-3333-3333-3333-333333333333"   # косинус к источнику ~0.0
EDGE_OK = "44444444-4444-4444-4444-444444444444"
EDGE_WEAK = "55555555-5555-5555-5555-555555555555"

# Ортонормированный базис: источник и NEAR коллинеарны (cos=1), FAR
# ортогонален (cos=0) — порог 0.3 проходит только NEAR.
_VEC_SRC = [1.0, 0.0]
_VEC_NEAR = [0.8, 0.0]
_VEC_FAR = [0.0, 1.0]


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
    qdrant = MagicMock()
    if vectors is None:
        vectors = {GID: _VEC_SRC, NEAR: _VEC_NEAR, FAR: _VEC_FAR}
    qdrant.retrieve_vectors = MagicMock(return_value=vectors)
    return qdrant


def campaign_rows() -> list[dict]:
    """Батч исторических l1c-кандидатов кампании: сильная и слабая пары."""
    now = datetime.now(timezone.utc)
    return [
        {
            "id": EDGE_OK, "source_id": GID, "target_id": NEAR,
            "src_cluster": "c-main", "tgt_cluster": "c-main", "created_at": now,
        },
        {
            "id": EDGE_WEAK, "source_id": GID, "target_id": FAR,
            "src_cluster": "c-main", "tgt_cluster": "c-main", "created_at": now,
        },
    ]


# ══════════════════════════════════════════════════════════════════
# Гейт L1c: вкл/выкл, фильтрация, fail-closed
# ══════════════════════════════════════════════════════════════════


class TestCosineHelper:
    def test_collinear_vectors_similarity_one(self):
        assert _cosine_similarity([1.0, 0.0], [0.8, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_similarity_zero(self):
        assert _cosine_similarity(_VEC_SRC, _VEC_FAR) == pytest.approx(0.0)

    def test_zero_vector_is_zero_similarity(self):
        """Вырожденный модуль → 0.0: пара гейт не проходит, не NaN."""
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestL1cGate:
    @pytest.mark.asyncio
    async def test_gate_off_passes_all_without_qdrant(self, mock_pool):
        """0.0 = гейт выключен: все соседи доходят до INSERT, Qdrant не дёргается."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": NEAR}, {"id": FAR}])
        conn.fetchrow = AsyncMock(return_value={"created": 2, "reinforced": 0})
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, linker_l1c_gate_min=0.0)

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 2, "reinforced": 0, "fail_closed": False}
        inserted_neighbors = conn.fetchrow.await_args_list[0].args[2]
        assert inserted_neighbors == [NEAR, FAR]
        assert conn.execute.await_args_list[0].args[0] == q.MARK_L1C_DONE

    @pytest.mark.asyncio
    async def test_gate_filters_by_cosine(self, mock_pool):
        """Порог 0.30: коллинеарный сосед проходит, ортогональный отсеян —
        до INSERT доходит только прошедший гейт."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": NEAR}, {"id": FAR}])
        conn.fetchrow = AsyncMock(return_value={"created": 1, "reinforced": 0})
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 1, "reinforced": 0, "fail_closed": False}
        inserted_neighbors = conn.fetchrow.await_args_list[0].args[2]
        assert inserted_neighbors == [NEAR]
        linker.qdrant.retrieve_vectors.assert_called_once_with([GID, NEAR, FAR])

    @pytest.mark.asyncio
    async def test_gate_fail_closed_qdrant_down(self, mock_pool):
        """Qdrant бросает исключение → fail-closed: рёбер нет, l1c_done НЕ
        пишется (гранула вернётся на ретрай), метрика растёт."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": NEAR}])
        conn.fetchrow = AsyncMock()
        conn.execute = AsyncMock()
        qdrant = gate_qdrant()
        qdrant.retrieve_vectors = MagicMock(side_effect=RuntimeError("connection refused"))
        linker = make_linker(mock_pool, qdrant=qdrant)
        before = LINKER_L1C_GATE_FAILURES_TOTAL._value.get()

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 0, "reinforced": 0, "fail_closed": True}
        conn.fetchrow.assert_not_awaited()
        conn.execute.assert_not_awaited()
        assert LINKER_L1C_GATE_FAILURES_TOTAL._value.get() == before + 1

    @pytest.mark.asyncio
    async def test_gate_fail_closed_no_source_vector(self, mock_pool):
        """Вектора источника нет (рассинхрон PG ↔ Qdrant) → весь батч
        fail-closed, сосед без вектора — точечный отсеив, не отказ."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": NEAR}, {"id": FAR}])
        conn.fetchrow = AsyncMock(return_value={"created": 0, "reinforced": 0})
        conn.execute = AsyncMock()
        qdrant = gate_qdrant(vectors={GID: _VEC_SRC})  # соседи без векторов
        linker = make_linker(mock_pool, qdrant=qdrant)

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        # источник жив: НЕ fail-closed; соседей без векторов гейт отсеял —
        # INSERT с пустым батчем пропущен, маркер пишется (0 вставок)
        assert outcome == {"created": 0, "reinforced": 0, "fail_closed": False}
        conn.fetchrow.assert_not_awaited()
        assert conn.execute.await_args_list[0].args[0] == q.MARK_L1C_DONE

    @pytest.mark.asyncio
    async def test_l1c_done_written_with_zero_inserts(self, mock_pool):
        """Все соседи отсеяны гейтом → 0 вставок, но маркер l1c_done пишется:
        гранула не остаётся вечно в beat-выборке (вердикт Эны по гейту)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": FAR}])
        conn.fetchrow = AsyncMock()
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 0, "reinforced": 0, "fail_closed": False}
        conn.fetchrow.assert_not_awaited()
        assert conn.execute.await_args_list[0].args[0] == q.MARK_L1C_DONE

    @pytest.mark.asyncio
    async def test_no_neighbors_no_marker(self, mock_pool):
        """Без соседей обработка тривиальна: маркер не нужен — EXISTS-гард
        выборки уже исключает гранулу."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 0, "reinforced": 0, "fail_closed": False}
        conn.execute.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════
# SQL-инварианты: l1c_done / mutual-reinforce / выборка кампании
# ══════════════════════════════════════════════════════════════════


class TestPhase3SqlContracts:
    def test_candidates_exclude_processed_granules(self):
        """l1c_done в выборке кампании: обработанные (в т.ч. 0 вставок)
        выпадают; COALESCE — NULL-метадата не роняет выборку."""
        assert (
            "AND NOT COALESCE((m.metadata->>'l1c_done')::boolean, false)"
            in q.SELECT_COOCCURRENCE_CANDIDATES
        )

    def test_marker_is_idempotent_jsonb_merge(self):
        """JSONB-merge по одной грануле; гард в WHERE — повтор no-op."""
        sql = q.MARK_L1C_DONE
        assert "metadata || '{\"l1c_done\": true}'::jsonb" in sql
        assert "WHERE id = $1::uuid" in sql
        assert "NOT COALESCE((metadata->>'l1c_done')::boolean, false)" in sql

    def test_mutual_reinforce_in_insert(self):
        """Существующая пара в ЛЮБОМ направлении — reinforce-касание вместо
        DO NOTHING: used_count+1, last_used_at, weight → 1.0 по α; DO NOTHING
        сохранён как страховка гонки индекса."""
        sql = q.INSERT_COOCCURRENCE_LINKS
        assert "reinforced AS (" in sql
        assert "used_count   = r.used_count + 1" in sql
        assert "last_used_at = now()" in sql
        assert "LEAST(1.0, r.weight + (1.0 - r.weight) * $3::float8)" in sql
        assert "(r.source_id = $1::uuid AND r.target_id = c.neighbor_id)" in sql
        assert "(r.source_id = c.neighbor_id AND r.target_id = $1::uuid)" in sql
        insert_branch = sql.split("reinforced AS (")[0]
        assert "NOT EXISTS" in insert_branch
        assert "DO NOTHING" in insert_branch

    def test_prune_candidates_immunity_filters(self):
        """Кампания берёт ТОЛЬКО живые линкерные l1c; мосты между кластерами
        иммунны (вердикт Эны); inherited/frozen — защитные фильтры."""
        sql = q.SELECT_COOCCURRENCE_PRUNE_CANDIDATES
        assert "r.pruned_at IS NULL" in sql  # идемпотентность: уже pruned не берём
        assert "r.metadata->>'source' = 'linker_v3'" in sql
        assert "r.metadata->>'layer' = 'l1c'" in sql
        assert "r.inherited_from IS NULL" in sql
        assert "NOT src.frozen" in sql
        assert "NOT tgt.frozen" in sql
        assert (
            "src.cluster_id IS NOT NULL\n"
            "          AND tgt.cluster_id IS NOT NULL\n"
            "          AND src.cluster_id IS DISTINCT FROM tgt.cluster_id" in sql
        )

    def test_prune_candidates_cursor_progress(self):
        """Курсор (created_at, id): live-мутации не зацикливают прогон."""
        sql = q.SELECT_COOCCURRENCE_PRUNE_CANDIDATES
        assert "(r.created_at, r.id) > ($2::timestamptz, $3::uuid)" in sql
        assert re.search(r"ORDER BY r\.created_at, r\.id", sql)
        assert "LIMIT $1" in sql


# ══════════════════════════════════════════════════════════════════
# Mutual-reinforce: поведение
# ══════════════════════════════════════════════════════════════════


class TestMutualReinforce:
    @pytest.mark.asyncio
    async def test_existing_pairs_counted_as_reinforced(self, mock_pool):
        """INSERT отчитывает созданные/усиленные раздельно; α передаётся
        из edge_reinforce_alpha (канонизация направлений — в SQL OR-матчем)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[{"id": NEAR}, {"id": FAR}])
        conn.fetchrow = AsyncMock(return_value={"created": 1, "reinforced": 1})
        conn.execute = AsyncMock()
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        async with mock_pool.acquire() as c:
            outcome = await linker._process_cooccurrence(c, GID, None, "ns", "s1")

        assert outcome == {"created": 1, "reinforced": 1, "fail_closed": False}
        insert_args = conn.fetchrow.await_args_list[0].args
        assert insert_args[3] == linker.config.edge_reinforce_alpha


# ══════════════════════════════════════════════════════════════════
# Кампания prune_cooccurrence_history
# ══════════════════════════════════════════════════════════════════


class TestPruneCooccurrenceHistory:
    @pytest.mark.asyncio
    async def test_dry_run_reports_without_writes(self, mock_pool):
        """dry_run: сильная пара выживает, слабая — кандидат на отсечение
        (would_prune), распределение по кластерам, пример с косинусом;
        pruned_at НЕ пишется."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(side_effect=[campaign_rows(), []])
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        report = await linker.run_prune_cooccurrence_history(dry_run=True)

        assert report["ok"] is True
        assert report["dry_run"] is True
        assert report["scanned"] == 2
        assert report["survived"] == 1
        assert report["would_prune"] == 1
        assert report["clusters"] == {"c-main": 1}
        assert report["examples"] == [{
            "edge_id": EDGE_WEAK, "source_id": GID, "target_id": FAR,
            "cosine": pytest.approx(0.0, abs=1e-4),
        }]
        assert "pruned" not in report
        # только выборки кандидатов, никакого PRUNE_EDGES_APPLY
        for call in conn.fetch.await_args_list:
            assert call.args[0] == q.SELECT_COOCCURRENCE_PRUNE_CANDIDATES

    @pytest.mark.asyncio
    async def test_live_prunes_weak_edges_only(self, mock_pool):
        """Бой: слабое ребро получает pruned_at (PRUNE_EDGES_APPLY, НЕ
        DELETE), сильное живёт."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        # SELECT батч1 → PRUNE_EDGES_APPLY (тоже fetch) → SELECT батч2 пустой
        conn.fetch = AsyncMock(side_effect=[campaign_rows(), [{"id": EDGE_WEAK}], []])
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        report = await linker.run_prune_cooccurrence_history(dry_run=False)

        assert report["pruned"] == 1
        assert report["survived"] == 1
        apply_call = conn.fetch.await_args_list[1]
        assert apply_call.args[0] == q.PRUNE_EDGES_APPLY
        assert len(apply_call.args[1]) == 1  # только слабое ребро

    @pytest.mark.asyncio
    async def test_live_run_is_idempotent(self, mock_pool):
        """Повтор по прогнанному: кандидатов нет (pruned_at IS NULL в выборке)
        — no-op по записи."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(side_effect=[[]])
        linker = make_linker(mock_pool, qdrant=gate_qdrant())

        report = await linker.run_prune_cooccurrence_history(dry_run=False)

        assert report["scanned"] == 0
        assert report["pruned"] == 0
        assert conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_qdrant_down_aborts_campaign(self, mock_pool):
        """Qdrant недоступен → отказ кампании: без векторов прунить нельзя
        (иначе погибли бы все 81k), ок=False с причиной."""
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

    @pytest.mark.asyncio
    async def test_gate_off_everything_survives(self, mock_pool):
        """Гейт выключен (0.0): все исторические выживают, Qdrant не дёргается."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(side_effect=[campaign_rows(), []])
        linker = make_linker(mock_pool, linker_l1c_gate_min=0.0)
        linker.qdrant = MagicMock()  # если дёрнется — тест упадёт на mock-методе

        report = await linker.run_prune_cooccurrence_history(dry_run=True)

        assert report["survived"] == 2
        assert report["would_prune"] == 0
        linker.qdrant.retrieve_vectors.assert_not_called()

    @pytest.mark.asyncio
    async def test_unverifiable_pairs_not_pruned(self, mock_pool):
        """Пара без вектора конца неоценима — не пруним, отдельный счётчик
        (защита от массового отсечения при частичном рассинхроне)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(side_effect=[campaign_rows(), []])
        qdrant = gate_qdrant(vectors={GID: _VEC_SRC})  # концы без векторов
        linker = make_linker(mock_pool, qdrant=qdrant)

        report = await linker.run_prune_cooccurrence_history(dry_run=False)

        assert report["unverifiable"] == 2
        assert report["pruned"] == 0
        assert report["survived"] == 0

    def test_task_registered_in_celery(self):
        """Таска кампании зарегистрирована под каноническим именем."""
        from memory_server.tasks.linker_tasks import prune_cooccurrence_history

        assert (
            prune_cooccurrence_history.name
            == "memory_server.tasks.linker_tasks.prune_cooccurrence_history"
        )
