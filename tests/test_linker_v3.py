"""Линкер V3 (ADR-019), фазы V3.2/V3.3 — резолв имён, пирамида L1a/L1c/L2.

Lateral-резолв (V3.2): приоритет свой-проект → глобальный → свежейшая,
только asserted; UUID-проход не сломан; резолв сохраняет target_name как
происхождение. Reconciler: dry_run не пишет, идемпотентность, батчи.
Co-occurrence L1c: related_to 0.5, кап, дубликаты гасятся. L1a: непересе-
кающиеся зоны порогов (0.80/0.85/dedup[ns]). L2: один LLM-вызов на
гранулу, строгий JSON, verdict-cache в Redis (инвалидация по content_hash),
авто-supersede по duplicate запрещён. Живой БД нет — SQL-контракты
проверяются инвариантами на текстах констант (паттерн test_memory_v3),
поведение — юнитами на mock_pool/httpx_mock.
"""

import json
import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.llm_client import (
    GranuleText,
    LinkerLLMClient,
    Verdict,
    VerdictParseError,
)
from memory_server.memory.linker import (
    CROSS_NS_ALLOWED,
    Linker,
    validate_link_type,
    verdict_cache_key,
)

GID = "11111111-1111-1111-1111-111111111111"
CAND_A = "22222222-2222-2222-2222-222222222222"
CAND_B = "33333333-3333-3333-3333-333333333333"


def linker_config(**overrides) -> Settings:
    """Конфиг линкера для юнитов: L2 включён (зоны видны), дефолты ADR."""
    base = {
        "dedup_enabled": False,
        "hybrid_search_enabled": False,
        "linker_l1c_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def make_linker(mock_pool, qdrant=None, redis_provider=None, llm=None, **cfg) -> Linker:
    return Linker(
        pool=mock_pool,
        qdrant=qdrant,
        redis_provider=redis_provider,
        config=linker_config(**cfg),
        llm=llm,
    )


def granule_row(**overrides) -> dict:
    row = {
        "ns_uid": "code_knowledge",
        "ns_id": "00000000-0000-0000-0000-0000000000aa",
        "sid": None,
        "project_id": None,
    }
    row.update(overrides)
    return row


def mock_qdrant(scores: dict[str, float]) -> MagicMock:
    """QdrantStore-мок: вектор новой гранулы есть, ANN возвращает scores."""
    qdrant = MagicMock()
    qdrant.retrieve_vectors = MagicMock(return_value={GID: [0.1, 0.2]})
    qdrant.search_batch = MagicMock(
        return_value=[[{"id": cid, "score": score} for cid, score in scores.items()]]
    )
    return qdrant


def mock_redis(queue: list[str] | None = None, store: dict | None = None) -> AsyncMock:
    """Redis-мок с list-очередью L2 и dict-хранилищем кеша/счётчиков.

    Конвенция очереди как в проде: lpush — в начало, rpop/rpush — с конца
    (конец списка = старейшие элементы, lindex(-1) — peek старейшего)."""
    queue = queue if queue is not None else []
    store = store if store is not None else {}

    async def rpop(_key):
        return queue.pop() if queue else None

    async def lpush(_key, value):
        queue.insert(0, value)

    async def rpush(_key, value):
        queue.append(value)

    async def lindex(_key, index):
        try:
            return queue[index]
        except IndexError:
            return None

    async def lrange(_key, start, stop):
        return queue[start : stop + 1 if stop != -1 else None]

    async def lrem(_key, count, value):
        removed = 0
        for _ in range(count if count > 0 else queue.count(value)):
            try:
                queue.remove(value)
                removed += 1
            except ValueError:
                break
        return removed

    async def mget(keys):
        return [store.get(k) for k in keys]

    async def setex(key, _ttl, value):
        store[key] = value

    async def llen(_key):
        return len(queue)

    async def incr(key, amount=1):
        store[key] = store.get(key, 0) + amount
        return store[key]

    redis = AsyncMock()
    redis._queue = queue  # наблюдаемость содержимого очереди в тестах
    redis._store = store
    redis.rpop = rpop
    redis.lpush = lpush
    redis.rpush = rpush
    redis.lindex = lindex
    redis.lrange = lrange
    redis.lrem = lrem
    redis.mget = mget
    redis.setex = setex
    redis.llen = llen
    redis.incr = incr
    return redis


# ══════════════════════════════════════════════════════════════════
# V3.2 — lateral-резолв в sync-путях (SQL-инварианты)
# ══════════════════════════════════════════════════════════════════


class TestLateralResolveSql:
    def test_priority_own_project_then_global_then_fresh(self):
        """Порядок lateral: свой проект (false-first), затем глобальный,
        затем свежейшая created_at — детерминизм вместо LIMIT 1 без ORDER BY."""
        for sql in (q.BACKFILL_RELATIONS_FROM_METADATA, q.SYNC_LINKS_BATCH):
            assert "LEFT JOIN LATERAL" in sql
            assert re.search(
                r"ORDER BY m2\.project_id IS NULL,\s*m2\.created_at DESC", sql
            ), sql
            assert "m.project_id IS NOT DISTINCT FROM m2.project_id" in sql
            assert "OR m2.project_id IS NULL" in sql

    def test_lateral_resolves_only_live_granules(self):
        """Ребро не прилипает к трупу: только status='asserted' AND valid_to IS NULL."""
        for sql in (q.BACKFILL_RELATIONS_FROM_METADATA, q.SYNC_LINKS_BATCH):
            lateral = sql.split("LEFT JOIN LATERAL")[1].split(") res ON true")[0]
            assert "m2.status = 'asserted'" in lateral
            assert "m2.valid_to IS NULL" in lateral

    def test_uuid_pass_intact(self):
        """UUID-цели резолвятся как раньше (regex-CASE), lateral — только фолбэк."""
        for sql in (q.BACKFILL_RELATIONS_FROM_METADATA, q.SYNC_LINKS_BATCH):
            assert "WHEN link->>'target' ~" in sql
            assert "COALESCE(sl.uuid_target, sl.resolved_id)" in sql

    def test_resolved_name_kept_as_provenance(self):
        """Резолвнутое имя остаётся в target_name (происхождение + статистика);
        UUID-цель — target_name NULL как раньше."""
        for sql in (q.BACKFILL_RELATIONS_FROM_METADATA, q.SYNC_LINKS_BATCH):
            assert (
                "CASE WHEN sl.uuid_target IS NOT NULL THEN NULL ELSE sl.target_str END" in sql
            )
        assert (
            "count(*) FILTER (WHERE target_id IS NOT NULL) AS resolved"
            in q.SELECT_LINKER_NAME_STATS
        )

    def test_sync_reports_resolved_by_name(self):
        """RETURNING несёт флаг lateral-резолва (метрика имён в sync-пути)."""
        for sql in (q.BACKFILL_RELATIONS_FROM_METADATA, q.SYNC_LINKS_BATCH):
            assert "AS resolved_by_name" in sql

    @pytest.mark.asyncio
    async def test_sync_metric_counts_resolved_names(self, mock_pool):
        """pg.sync_links_* инкрементирует метрику только по lateral-резолвам."""
        from memory_server.memory.pg_repository import PostgreSQLRepository

        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[
                {"id": "r1", "resolved_by_name": True},
                {"id": "r2", "resolved_by_name": False},
                {"id": "r3", "resolved_by_name": True},
            ]
        )
        pg = PostgreSQLRepository(pool=mock_pool)
        assert await pg.sync_links_to_relations(GID) == 3
        assert await pg.sync_links_batch([GID]) == 3


# ══════════════════════════════════════════════════════════════════
# V3.2 — name_reconciler
# ══════════════════════════════════════════════════════════════════


class TestNameReconciler:
    def test_sql_resolves_only_pending_edges(self):
        """Идемпотентность: выборка только WHERE target_id IS NULL —
        повторный прогон не трогает уже резолвнутое, дублей нет."""
        assert "r.target_id IS NULL" in q.RESOLVE_PENDING_TARGET_NAMES
        assert "AND r.target_name IS NOT NULL" in q.RESOLVE_PENDING_TARGET_NAMES

    def test_sql_duplicate_guard(self):
        """Резолв не создаёт дубль под partial unique: каноничное ребро
        (source, resolved_target, link_type) уже есть → висяк остаётся."""
        assert "dup.source_id = r.source_id" in q.RESOLVE_PENDING_TARGET_NAMES
        assert "dup.target_id = cand.id" in q.RESOLVE_PENDING_TARGET_NAMES

    def test_sql_marks_provenance(self):
        assert "'resolved_by', 'name_reconciler'" in q.RESOLVE_PENDING_TARGET_NAMES

    def test_dry_run_shares_where_chain_with_battle(self):
        """Сухой прогон считает ровно то, что написал бы боевой (тот же CTE)."""
        assert q.RESOLVE_PENDING_TARGET_NAMES_DRY.startswith(
            q.RESOLVE_PENDING_TARGET_NAMES[: q.RESOLVE_PENDING_TARGET_NAMES.index("UPDATE")]
        )

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchval = AsyncMock(side_effect=[11782, 2666])  # pending, would_resolve
        linker = make_linker(mock_pool)

        report = await linker.run_name_reconciler(dry_run=True)

        assert report == {
            "dry_run": True,
            "resolved": 0,
            "would_resolve": 2666,
            "pending": 11782,
            "batch": linker.config.linker_reconciler_batch,
        }
        conn.fetch.assert_not_awaited()  # ни одного UPDATE — только count

    @pytest.mark.asyncio
    async def test_battle_mode_loops_batches_until_empty(self, mock_pool):
        """Боевой прогон: цикл батчей по 500, счёт разрешённых, отчёт остатка."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        # pending_total, батч1 (450 строк), батч2 (0 строк — стоп), remaining
        conn.fetchval = AsyncMock(side_effect=[1000, 50])
        conn.fetch = AsyncMock(side_effect=[["r"] * 450, []])
        linker = make_linker(mock_pool)

        report = await linker.run_name_reconciler(dry_run=False)

        assert report["resolved"] == 450
        assert report["pending"] == 50
        assert report["dry_run"] is False
        assert conn.fetch.await_count == 2
        batch_arg = conn.fetch.await_args_list[0].args[1]
        assert batch_arg == linker.config.linker_reconciler_batch == 500

    @pytest.mark.asyncio
    async def test_dry_run_default_from_config(self, mock_pool):
        """Без аргумента сухость берётся из конфига (True до ручной кампании)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchval = AsyncMock(return_value=10)
        linker = make_linker(mock_pool, linker_reconciler_dry_run=True)
        report = await linker.run_name_reconciler()
        assert report["dry_run"] is True
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_battle_mode_nothing_to_do(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchval = AsyncMock(side_effect=[0, 0])
        conn.fetch = AsyncMock(return_value=[])
        linker = make_linker(mock_pool)
        report = await linker.run_name_reconciler(dry_run=False)
        assert report == {
            "dry_run": False,
            "resolved": 0,
            "pending": 0,
            "batch": 500,
        }


# ══════════════════════════════════════════════════════════════════
# V3.2 — co-occurrence L1c
# ══════════════════════════════════════════════════════════════════


class TestCoOccurrence:
    def test_sql_shape(self):
        """related_to 0.5, соседи той же сессии (project IS NOT DISTINCT FROM
        включает NULL=NULL), свежие, кап, дубли в обоих направлениях гасятся."""
        sql = q.INSERT_COOCCURRENCE_LINKS
        assert "'related_to', 0.5" in sql
        assert "m.project_id IS NOT DISTINCT FROM $2::uuid" in sql
        assert "m.metadata->>'session_id' = $4" in sql
        assert "m.namespace_id = $3::uuid" in sql
        assert "ORDER BY m.created_at DESC" in sql
        assert "LIMIT $5" in sql
        assert "ON CONFLICT (source_id, target_id, link_type)" in sql
        assert "DO NOTHING" in sql
        # встречный дубль: пара уже связана в любом направлении — не дублируем
        assert "(r.source_id = $1::uuid AND r.target_id = n.id)" in sql
        assert "(r.source_id = n.id AND r.target_id = $1::uuid)" in sql

    @pytest.mark.asyncio
    async def test_creates_edges_with_cap(self, mock_pool):
        """Кампания: INSERT на каждую гранулу-кандидата, кап из конфига."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(
            side_effect=[
                [  # SELECT_COOCCURRENCE_CANDIDATES
                    {"id": GID, "project_id": None, "namespace_id": "ns", "session_id": "s1"},
                    {"id": CAND_A, "project_id": None, "namespace_id": "ns", "session_id": "s1"},
                ],
                [{"id": "rel-1"}, {"id": "rel-2"}],  # INSERT для GID
                [],  # INSERT для CAND_A: соседи уже связаны NOT EXISTS-ом
            ]
        )
        linker = make_linker(mock_pool, linker_l1c_enabled=True)

        report = await linker.run_co_occurrence()

        assert report == {"candidates": 2, "links_created": 2}
        cap = conn.fetch.await_args_list[1].args[-1]
        assert cap == linker.config.linker_cooccurrence_cap == 10

    @pytest.mark.asyncio
    async def test_duplicate_edges_not_created(self, mock_pool):
        """NOT EXISTS в SQL гасит дубли — INSERT возвращает пусто, кампания
        идемпотентна (повтор по обработанному — no-op)."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(
            side_effect=[
                [{"id": GID, "project_id": None, "namespace_id": "ns", "session_id": "s1"}],
                [],  # все пары уже связаны
            ]
        )
        linker = make_linker(mock_pool, linker_l1c_enabled=True)
        assert await linker.run_co_occurrence() == {"candidates": 1, "links_created": 0}

    def test_candidates_query_requires_living_neighbors(self):
        """Гранулы без соседей не крутятся в выборке вечно (идемпотентность пула)."""
        sql = q.SELECT_COOCCURRENCE_CANDIDATES
        assert "m.metadata->>'session_id' IS NOT NULL" in sql
        assert "r.metadata->>'layer' = 'l1c'" in sql  # уже обработанные выпадают
        assert "AND EXISTS (" in sql

    @pytest.mark.asyncio
    async def test_new_granule_gets_l1c_same_session(self, mock_pool):
        """link_new_granule: сессия у гранулы → co-occurrence рёбра сразу."""
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(return_value=granule_row(sid="ses-1"))
        conn.fetch = AsyncMock(return_value=[{"id": "rel-1"}])
        linker = make_linker(mock_pool, linker_l1c_enabled=True)  # без qdrant

        report = await linker.link_new_granule(GID)

        assert report["l1c_created"] == 1
        insert_sql = conn.fetch.await_args_list[0].args[0]
        assert insert_sql == q.INSERT_COOCCURRENCE_LINKS


# ══════════════════════════════════════════════════════════════════
# V3.3 — L1a: зоны порогов
# ══════════════════════════════════════════════════════════════════


class TestL1aZones:
    def _prepared(self, mock_pool, scores):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(return_value=granule_row())
        qdrant = mock_qdrant(scores)
        return conn, qdrant

    @pytest.mark.asyncio
    async def test_below_080_nothing(self, mock_pool):
        """< 0.80 — тишина (HippoRAG 2): ни рёбер, ни очереди L2."""
        conn, qdrant = self._prepared(mock_pool, {CAND_A: 0.79})
        linker = make_linker(mock_pool, qdrant=qdrant)

        report = await linker.link_new_granule(GID)

        assert report["l1a_created"] == 0 and report["l2_enqueued"] == 0
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_082_auto_related_to(self, mock_pool):
        """[0.80, 0.85) — L1 auto: related_to с weight=score, без LLM."""
        conn, qdrant = self._prepared(mock_pool, {CAND_A: 0.82})
        conn.fetchrow = AsyncMock(
            side_effect=[granule_row(), {"id": "rel-l1a"}]
        )
        linker = make_linker(mock_pool, qdrant=qdrant)

        report = await linker.link_new_granule(GID)

        assert report["l1a_created"] == 1
        args = conn.fetchrow.await_args_list[1].args
        assert args[0] == q.INSERT_LINKER_RELATION
        assert args[1] == GID and args[2] == CAND_A
        assert args[3] == "related_to"
        assert args[4] is None  # description
        assert args[5] == 0.82  # weight = score
        assert args[6] == {"source": "linker_v3", "layer": "l1a"}

    @pytest.mark.asyncio
    async def test_090_goes_to_l2_queue(self, mock_pool):
        """[0.85, dedup) — серая зона: НЕ ребро, а элемент очереди L2."""
        conn, qdrant = self._prepared(mock_pool, {CAND_A: 0.90})
        redis = mock_redis()
        linker = make_linker(
            mock_pool,
            qdrant=qdrant,
            redis_provider=None,
            llm=object(),  # включён — очередь наполняется
        )
        linker._get_redis = AsyncMock(return_value=redis)

        report = await linker.link_new_granule(GID)

        assert report["l1a_created"] == 0 and report["l2_enqueued"] == 1
        conn.fetchrow.assert_called_once()  # только SELECT гранулы, без INSERT
        enqueued = json.loads(redis_l2_queue(redis)[0])
        assert enqueued["granule_id"] == GID
        assert enqueued["candidates"] == [{"id": CAND_A, "score": 0.9}]

    @pytest.mark.asyncio
    async def test_096_dedup_wins(self, mock_pool):
        """≥ dedup_thresholds[ns] — территория DedupEngine: линкер молчит."""
        conn, qdrant = self._prepared(mock_pool, {CAND_A: 0.96})
        redis = mock_redis()
        linker = make_linker(mock_pool, qdrant=qdrant, llm=object())
        linker._get_redis = AsyncMock(return_value=redis)

        report = await linker.link_new_granule(GID)

        assert report["l1a_created"] == 0 and report["l2_enqueued"] == 0
        assert not redis_l2_queue(redis)

    @pytest.mark.asyncio
    async def test_zone_upper_bound_is_per_namespace(self, mock_pool):
        """dialogue_insights дедупит с 0.85 — серой зоны L2 там нет:
        0.86 уходит в dedup-зону (слои не пересекаются ни в одном ns)."""
        conn, qdrant = self._prepared(mock_pool, {CAND_A: 0.86})
        conn.fetchrow = AsyncMock(return_value=granule_row(ns_uid="dialogue_insights"))
        linker = make_linker(mock_pool, qdrant=qdrant, llm=object())
        linker._get_redis = AsyncMock(return_value=mock_redis())

        report = await linker.link_new_granule(GID)

        assert report["l1a_created"] == 0 and report["l2_enqueued"] == 0

    @pytest.mark.asyncio
    async def test_self_match_skipped(self, mock_pool):
        """HNSW возвращает саму точку (score ≈ 1.0) — self не линкуется."""
        conn, qdrant = self._prepared(mock_pool, {GID: 0.99})
        linker = make_linker(mock_pool, qdrant=qdrant, llm=object())
        linker._get_redis = AsyncMock(return_value=mock_redis())

        report = await linker.link_new_granule(GID)

        assert report["l1a_created"] == 0 and report["l2_enqueued"] == 0

    @pytest.mark.asyncio
    async def test_l2_queue_filled_in_manual_mode_without_llm(self, mock_pool):
        """Manual mode (дефолт, приказ Мастера 20.09): LLM нет, а очередь
        серой зоны КОПИТСЯ — разбирать будет Тишь тулами review/verdict."""
        conn, qdrant = self._prepared(mock_pool, {CAND_A: 0.90})
        redis = mock_redis()
        linker = make_linker(mock_pool, qdrant=qdrant, llm=None)  # LLM выключен
        linker._get_redis = AsyncMock(return_value=redis)

        report = await linker.link_new_granule(GID)

        assert report["l2_enqueued"] == 1
        assert len(redis_l2_queue(redis)) == 1

    @pytest.mark.asyncio
    async def test_l2_queue_not_filled_when_fully_off(self, mock_pool):
        """linker_l2_manual=False + нет LLM → серая зона игнорируется
        (l2_mode "off"): очередь не наполняется."""
        conn, qdrant = self._prepared(mock_pool, {CAND_A: 0.90})
        redis = mock_redis()
        linker = make_linker(mock_pool, qdrant=qdrant, llm=None, linker_l2_manual=False)
        linker._get_redis = AsyncMock(return_value=redis)

        report = await linker.link_new_granule(GID)

        assert report["l2_enqueued"] == 0
        assert not redis_l2_queue(redis)

    @pytest.mark.asyncio
    async def test_l1a_flag_off(self, mock_pool):
        """Флаг выключения L1a (риск шума ADR-019.1) — ANN не выполняется."""
        conn, qdrant = self._prepared(mock_pool, {CAND_A: 0.82})
        linker = make_linker(mock_pool, qdrant=qdrant, linker_l1a_enabled=False)

        report = await linker.link_new_granule(GID)

        assert report["l1a_created"] == 0
        qdrant.search_batch.assert_not_called()


def redis_l2_queue(redis_mock) -> list[str]:
    """Элементы, лежащие в очереди L2 мока (lpush → insert(0, value))."""
    return redis_mock._queue


# ══════════════════════════════════════════════════════════════════
# V3.3 — CNLM-матрица
# ══════════════════════════════════════════════════════════════════


class TestCnlmMatrix:
    def test_matrix_from_changelog(self):
        """Матрица ровно из CHANGELOG-auto-linker-cnlm.md v1.1.0."""
        assert CROSS_NS_ALLOWED[("project_meta", "code_knowledge")] == frozenset(
            {"implements_adr", "solves", "motivates"}
        )
        assert CROSS_NS_ALLOWED[("dialogue_insights", "code_knowledge")] == frozenset(
            {"solves", "derived_from", "motivates"}
        )
        assert CROSS_NS_ALLOWED[("user_facts", "code_knowledge")] == frozenset(
            {"motivates", "derived_from"}
        )
        assert CROSS_NS_ALLOWED[("project_meta", "dialogue_insights")] == frozenset(
            {"informed_by"}
        )

    def test_intra_namespace_any_type(self):
        assert validate_link_type("code_knowledge", "code_knowledge", "calls") == "calls"

    def test_cross_ns_allowed_type_passes(self):
        assert (
            validate_link_type("project_meta", "code_knowledge", "implements_adr")
            == "implements_adr"
        )

    def test_cross_ns_wrong_type_rejected(self):
        """implements_adr вне пары (project_meta → code_knowledge) — не допускается."""
        assert validate_link_type("user_facts", "project_meta", "implements_adr") is None
        assert validate_link_type("code_knowledge", "project_meta", "solves") is None

    def test_unknown_pair_neutral_related_to_only(self):
        assert validate_link_type("user_facts", "infrastructure", "related_to") == "related_to"
        assert validate_link_type("user_facts", "infrastructure", "calls") is None


# ══════════════════════════════════════════════════════════════════
# V3.3 — LLM-клиент L2
# ══════════════════════════════════════════════════════════════════


def llm_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _client() -> LinkerLLMClient:
    return LinkerLLMClient(
        base_url="http://llm-test:8000/v1",
        api_key="test-key",
        model="glm-4.7-flash",
        timeout=10.0,
        max_retries=1,
    )


def _source() -> GranuleText:
    return GranuleText(GID, "src-title", "source content", "project_meta", "hash-a")


def _candidate() -> GranuleText:
    return GranuleText(CAND_A, "cand-title", "candidate content", "code_knowledge", "hash-b")


class TestLinkerLLMClient:
    @pytest.mark.asyncio
    async def test_valid_json_verdicts(self, httpx_mock):
        httpx_mock.add_response(
            url="http://llm-test:8000/v1/chat/completions",
            json=llm_response(
                json.dumps(
                    {
                        "verdicts": [
                            {
                                "id": CAND_A,
                                "verdict": "link",
                                "link_type": "implements_adr",
                                "confidence": 0.9,
                                "rationale": "module implements the ADR",
                            }
                        ]
                    }
                )
            ),
        )
        verdicts = await _client().verdict_batch(_source(), [_candidate()])
        assert verdicts[CAND_A] == Verdict(
            verdict="link",
            link_type="implements_adr",
            confidence=0.9,
            rationale="module implements the ADR",
        )

    @pytest.mark.asyncio
    async def test_json_inside_markdown_fence_parsed(self, httpx_mock):
        """Модели любят оборачивать JSON в ```json — вырезаем блок."""
        fenced = '```json\n{"verdicts": [{"id": "%s", "verdict": "none", "confidence": 0.1}]}\n```' % CAND_A
        httpx_mock.add_response(
            url="http://llm-test:8000/v1/chat/completions",
            json=llm_response(fenced),
        )
        verdicts = await _client().verdict_batch(_source(), [_candidate()])
        assert verdicts[CAND_A].verdict == "none"

    @pytest.mark.asyncio
    async def test_broken_json_raises_parse_error(self, httpx_mock):
        httpx_mock.add_response(
            url="http://llm-test:8000/v1/chat/completions",
            json=llm_response("Sorry, I cannot answer in JSON"),
        )
        with pytest.raises(VerdictParseError):
            await _client().verdict_batch(_source(), [_candidate()])

    @pytest.mark.asyncio
    async def test_unknown_link_type_degrades_to_null(self, httpx_mock):
        httpx_mock.add_response(
            url="http://llm-test:8000/v1/chat/completions",
            json=llm_response(
                json.dumps(
                    {"verdicts": [{"id": CAND_A, "verdict": "link", "link_type": "quantum_entangle", "confidence": 2.0}]}
                )
            ),
        )
        verdicts = await _client().verdict_batch(_source(), [_candidate()])
        assert verdicts[CAND_A].link_type is None
        assert verdicts[CAND_A].confidence == 1.0  # clamp 0..1

    @pytest.mark.asyncio
    async def test_timeout_raises_after_retry(self, httpx_mock):
        """Таймаут — транспортный сбой: ретрай, затем исключение (элемент
        очереди вернётся). Один WARN, не зацикливаем."""
        for _ in range(2):  # max_retries=1 → 2 попытки
            httpx_mock.add_exception(httpx.ConnectTimeout("timed out"))
        with pytest.raises(httpx.ConnectTimeout):
            await _client().verdict_batch(_source(), [_candidate()])
        assert len(httpx_mock.get_requests()) == 2


# ══════════════════════════════════════════════════════════════════
# V3.3 — verdict-cache + применение вердиктов
# ══════════════════════════════════════════════════════════════════


def granule_rows_for_verdict() -> list[dict]:
    return [
        {
            "id": GID,
            "content": "source content",
            "entity_name": "src-title",
            "content_hash": "hash-a",
            "namespace": "project_meta",
        },
        {
            "id": CAND_A,
            "content": "candidate content",
            "entity_name": "cand-title",
            "content_hash": "hash-b",
            "namespace": "code_knowledge",
        },
    ]


class TestVerdictCacheAndApply:
    def _linker_with_queue(self, mock_pool, redis, llm):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=granule_rows_for_verdict())
        conn.fetchrow = AsyncMock(return_value={"id": "new-rel"})
        linker = make_linker(mock_pool, redis_provider=lambda: None, llm=llm)
        linker._get_redis = AsyncMock(return_value=redis)
        return linker, conn

    @staticmethod
    def _queue_item() -> str:
        return json.dumps({"granule_id": GID, "candidates": [{"id": CAND_A, "score": 0.9}], "attempts": 0})

    @pytest.mark.asyncio
    async def test_verdict_creates_cnlm_checked_edge(self, mock_pool):
        """link + валидный CNLM тип → ребро с link_type и weight=confidence."""
        redis = mock_redis(queue=[self._queue_item()])
        llm = AsyncMock()
        llm.verdict_batch = AsyncMock(
            return_value={CAND_A: Verdict("link", "implements_adr", 0.88, "rationale")}
        )
        linker, conn = self._linker_with_queue(mock_pool, redis, llm)

        report = await linker.run_l2_verdicts()

        assert report["processed"] == 1 and report["links_created"] == 1
        args = conn.fetchrow.await_args_list[0].args
        assert args[0] == q.INSERT_LINKER_RELATION
        assert args[3] == "implements_adr"  # CNLM-валидный тип сохранён
        assert args[5] == 0.88  # weight = confidence
        assert args[6]["layer"] == "l2"

    @pytest.mark.asyncio
    async def test_cnlm_rejected_type_falls_back_to_related_to(self, mock_pool):
        """LLM предложил тип вне CNLM-пары → нейтральный related_to."""
        redis = mock_redis(queue=[self._queue_item()])
        llm = AsyncMock()
        llm.verdict_batch = AsyncMock(
            return_value={CAND_A: Verdict("link", "informs", 0.8)}  # вне пары pm→ck
        )
        linker, conn = self._linker_with_queue(mock_pool, redis, llm)

        await linker.run_l2_verdicts()

        assert conn.fetchrow.await_args_list[0].args[3] == "related_to"

    @pytest.mark.asyncio
    async def test_duplicate_never_auto_supersedes(self, mock_pool):
        """duplicate → WARN + ничего не пишется (merge без человека запрещён)."""
        redis = mock_redis(queue=[self._queue_item()])
        llm = AsyncMock()
        llm.verdict_batch = AsyncMock(return_value={CAND_A: Verdict("duplicate", None, 0.95)})
        linker, conn = self._linker_with_queue(mock_pool, redis, llm)

        report = await linker.run_l2_verdicts()

        assert report["links_created"] == 0
        conn.fetchrow.assert_not_called()  # INSERT не выполнялся вовсе

    @pytest.mark.asyncio
    async def test_contradiction_creates_contradicts_edge(self, mock_pool):
        redis = mock_redis(queue=[self._queue_item()])
        llm = AsyncMock()
        llm.verdict_batch = AsyncMock(return_value={CAND_A: Verdict("contradiction", None, 0.7)})
        linker, conn = self._linker_with_queue(mock_pool, redis, llm)

        await linker.run_l2_verdicts()

        assert conn.fetchrow.await_args_list[0].args[3] == "contradicts"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm(self, mock_pool):
        """Второй прогон той же пары: вердикт из кеша, LLM не зовётся."""
        redis = mock_redis(queue=[self._queue_item(), self._queue_item()])
        llm = AsyncMock()
        llm.verdict_batch = AsyncMock(return_value={CAND_A: Verdict("link", "solves", 0.9)})
        linker, conn = self._linker_with_queue(mock_pool, redis, llm)

        await linker.run_l2_verdicts()
        assert llm.verdict_batch.await_count == 1
        await linker.run_l2_verdicts()

        assert llm.verdict_batch.await_count == 1  # hit — LLM не позван

    @pytest.mark.asyncio
    async def test_cache_invalidated_by_content_hash(self):
        """Изменился контент → другой ключ → miss (инвалидация по хэшу)."""
        key_old = verdict_cache_key(GID, CAND_A, "hash-a", "hash-b")
        key_new = verdict_cache_key(GID, CAND_A, "hash-a-EDITED", "hash-b")
        assert key_old != key_new

    def test_cache_key_symmetric_pair_shares_entry(self):
        """Встречные пары a→b / b→a — одна запись кеша (баг приёмки В3:
        хэши раньше не канонизировались вместе с id → промах и второй
        LLM-вызов на ту же пару)."""
        key_ab = verdict_cache_key("a-uuid", "b-uuid", "hash-a", "hash-b")
        key_ba = verdict_cache_key("b-uuid", "a-uuid", "hash-b", "hash-a")
        assert key_ab == key_ba

    @pytest.mark.asyncio
    async def test_broken_json_does_not_poison_cache(self, mock_pool):
        """Битый JSON → деградация none БЕЗ записи в кеш (баг приёмки В2:
        один мусорный ответ LLM не должен месяц закрывать линк пары)."""
        redis = mock_redis(queue=[self._queue_item()])
        llm = AsyncMock()
        llm.verdict_batch = AsyncMock(side_effect=VerdictParseError("garbage"))
        linker, conn = self._linker_with_queue(mock_pool, redis, llm)

        await linker.run_l2_verdicts()

        # Ни одного ключа кеша: следующая попытка снова пойдёт в LLM
        vdc_keys = [k for k in redis._store if k.startswith("linker:vdc")]
        assert not vdc_keys, f"cache poisoned: {vdc_keys}"

    @pytest.mark.asyncio
    async def test_transport_failure_requeues(self, mock_pool):
        """Таймаут LLM → элемент возвращается в очередь (не теряется)."""
        redis = mock_redis(queue=[self._queue_item()])
        llm = AsyncMock()
        llm.verdict_batch = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
        linker, _ = self._linker_with_queue(mock_pool, redis, llm)

        report = await linker.run_l2_verdicts()

        assert report["requeued"] == 1
        assert json.loads(redis_l2_queue(redis)[0])["attempts"] == 1

    @pytest.mark.asyncio
    async def test_disabled_llm_leaves_queue_untouched(self, mock_pool):
        """base_url пуст ⇒ llm=None ⇒ очередь НЕ разбирается."""
        redis = mock_redis(queue=[self._queue_item()])
        linker = make_linker(mock_pool, redis_provider=lambda: None, llm=None)
        linker._get_redis = AsyncMock(return_value=redis)

        report = await linker.run_l2_verdicts()

        assert report == {"ok": False, "reason": "llm_disabled", "processed": 0}
        assert len(redis_l2_queue(redis)) == 1  # элемент остался

    @pytest.mark.asyncio
    async def test_broken_json_degrades_to_none(self, mock_pool):
        """Битый JSON → none по всем кандидатам + вердикт-error; очередь чистится."""
        redis = mock_redis(queue=[self._queue_item()])
        llm = AsyncMock()
        llm.verdict_batch = AsyncMock(side_effect=VerdictParseError("garbage"))
        linker, conn = self._linker_with_queue(mock_pool, redis, llm)

        report = await linker.run_l2_verdicts()

        assert report["verdicts"] == {"none": 1}
        assert report["links_created"] == 0
        conn.fetchrow.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# V3.3 — store диспетчеризует линкер асинхронно
# ══════════════════════════════════════════════════════════════════


class TestStoreDispatch:
    @pytest.fixture
    def service(self, mock_repository, mock_embedding_provider, mock_namespace_repository):
        from memory_server.memory.service import MemoryService

        return MemoryService(
            repository=mock_repository,
            embedding_provider=mock_embedding_provider,
            namespace_repository=mock_namespace_repository,
            config=Settings(dedup_enabled=False, hybrid_search_enabled=False),
        )

    @pytest.mark.asyncio
    async def test_store_does_not_wait_for_linker(self, service):
        """Диспетчер вызывается синхронно-неблокирующе после INSERT: тул
        store возвращается без ожидания задач линкера (dispatch = постановка)."""
        from datetime import datetime, timezone

        from memory_server.models import MemoryRecord

        dispatch = MagicMock()
        service.linker_dispatch = dispatch
        service.embedding.embed = AsyncMock(return_value=[0.1])
        service.repository.insert = AsyncMock(return_value="new-id")
        service.repository.get_by_id = AsyncMock(
            return_value=MemoryRecord(
                id="new-id",
                user_id="u1",
                content="x",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )

        record, action = await service.store(content="x", user_id="u1")

        assert action.value == "insert"
        dispatch.assert_called_once_with("new-id")

    @pytest.mark.asyncio
    async def test_store_survives_dispatch_failure(self, service):
        """Сбой диспетчеризации не роняет запись (best-effort)."""
        from datetime import datetime, timezone

        from memory_server.models import MemoryRecord

        dispatch = MagicMock(side_effect=RuntimeError("broker down"))
        service.linker_dispatch = dispatch
        service.embedding.embed = AsyncMock(return_value=[0.1])
        service.repository.insert = AsyncMock(return_value="new-id")
        service.repository.get_by_id = AsyncMock(
            return_value=MemoryRecord(
                id="new-id",
                user_id="u1",
                content="x",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )

        record, action = await service.store(content="x", user_id="u1")
        assert record.id == "new-id"

    def test_enqueue_link_respects_master_flag(self, monkeypatch):
        """linker_enabled=False → send_task не зовётся вовсе."""
        from memory_server.tasks import linker_tasks

        monkeypatch.setattr(linker_tasks.settings, "linker_enabled", False, raising=False)
        send_task = MagicMock()
        monkeypatch.setattr(
            "memory_server.celery_app.app.send_task", send_task, raising=True
        )
        linker_tasks.enqueue_link(GID)
        send_task.assert_not_called()

    def test_enqueue_link_sends_memory_task(self, monkeypatch):
        from memory_server.tasks import linker_tasks

        monkeypatch.setattr(linker_tasks.settings, "linker_enabled", True, raising=False)
        send_task = MagicMock()
        monkeypatch.setattr(
            "memory_server.celery_app.app.send_task", send_task, raising=True
        )
        linker_tasks.enqueue_link(GID)
        kwargs = send_task.call_args.kwargs
        assert send_task.call_args.args[0] == "memory_server.tasks.linker_tasks.link_new_granule"
        assert kwargs["kwargs"] == {"granule_id": GID}
        assert kwargs["queue"] == "memory"


# ══════════════════════════════════════════════════════════════════
# memory_linker_stats
# ══════════════════════════════════════════════════════════════════


class TestLinkerStats:
    @pytest.mark.asyncio
    async def test_stats_aggregates_layers_names_queue(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(
            return_value=[
                {"layer": "l1a", "count": 120},
                {"layer": "l2", "count": 7},
            ]
        )
        conn.fetchrow = AsyncMock(return_value={"resolved": 2666, "pending": 9116})
        redis = mock_redis(queue=["item-1", "item-2"])
        # mget: 5 LLM-вердиктов + 4 manual + cache hit/miss
        redis.mget = AsyncMock(return_value=[3, 1, 0, 5, 2, 7, 0, 0, 1, 10, 4])
        linker = make_linker(mock_pool, redis_provider=lambda: None, llm=object())
        linker._get_redis = AsyncMock(return_value=redis)

        stats = await linker.stats()

        assert stats["links_by_layer"] == {"l1a": 120, "l2": 7}
        assert stats["links_total"] == 127
        assert stats["names_resolved"] == 2666
        assert stats["names_pending"] == 9116
        assert stats["l2_queue_size"] == 2
        assert stats["verdicts"]["link"] == 3
        assert stats["verdicts_manual"]["link"] == 7
        assert stats["verdict_cache"] == {"hits": 10, "misses": 4}
        assert stats["l2_enabled"] is True


# ══════════════════════════════════════════════════════════════════
# Manual mode L2 (приказ Мастера 20.09): разбор очереди Тишью
# ══════════════════════════════════════════════════════════════════


def queue_item(granule_id: str, candidate_ids: list[str]) -> str:
    return json.dumps(
        {
            "granule_id": granule_id,
            "candidates": [{"id": cid, "score": 0.9} for cid in candidate_ids],
            "attempts": 0,
        }
    )


class TestManualMode:
    def _linker(self, mock_pool, redis, llm=None, **cfg) -> Linker:
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=granule_rows_for_verdict())
        conn.fetchrow = AsyncMock(return_value={"id": "rel-manual"})
        linker = make_linker(mock_pool, redis_provider=lambda: None, llm=llm, **cfg)
        linker._get_redis = AsyncMock(return_value=redis)
        return linker

    @pytest.mark.asyncio
    async def test_peek_empty_queue(self, mock_pool):
        linker = self._linker(mock_pool, mock_redis())
        assert await linker.peek_l2() == {"empty": True}

    @pytest.mark.asyncio
    async def test_peek_returns_oldest_without_removing(self, mock_pool):
        """Peek показывает старейший (конец очереди) и НЕ извлекает его:
        повторный review вернёт то же, пока пара не закрыта verdict-ом."""
        newer = queue_item(CAND_A, [GID])
        older = queue_item(GID, [CAND_A])
        redis = mock_redis(queue=[newer, older])  # конец списка = старейший
        linker = self._linker(mock_pool, redis)

        result = await linker.peek_l2()

        assert result["empty"] is False
        assert result["granule_id"] == GID
        assert result["source"]["id"] == GID
        assert result["source"]["title"] == "src-title"
        assert result["source"]["content"] == "source content"
        assert result["source"]["namespace"] == "project_meta"
        assert result["candidates"] == [
            {
                "id": CAND_A,
                "title": "cand-title",
                "content": "candidate content",
                "namespace": "code_knowledge",
                "score": 0.9,
            }
        ]
        # очередь не тронута: тот же размер, тот же старейший
        assert redis_l2_queue(redis) == [newer, older]
        assert (await linker.peek_l2())["granule_id"] == GID

    @pytest.mark.asyncio
    async def test_peek_cleans_stale_source(self, mock_pool):
        """Источник элемента закрылся (superseded/retracted) — элемент протух:
        peek чистит его и показывает следующий живой."""
        stale = queue_item("99999999-9999-9999-9999-999999999999", [GID])
        fresh = queue_item(GID, [CAND_A])
        redis = mock_redis(queue=[fresh, stale])
        linker = self._linker(mock_pool, redis)

        result = await linker.peek_l2()

        assert result["granule_id"] == GID
        assert redis_l2_queue(redis) == [fresh]  # труп убран

    @pytest.mark.asyncio
    async def test_manual_verdict_link_creates_edge_and_cache(self, mock_pool):
        """link: ребро с weight=confidence + verdict-cache тем же симметричным
        ключом, что у LLM-пути — будущий воркер решение человека не перекроет."""
        redis = mock_redis(queue=[queue_item(GID, [CAND_A])])
        linker = self._linker(mock_pool, redis)
        conn = mock_pool.acquire.return_value.__aenter__.return_value

        result = await linker.apply_manual_verdict(
            GID, CAND_A, "link", link_type="implements_adr", confidence=0.95
        )

        assert result["verdict"] == "link"
        assert result["link_created"] is True
        args = conn.fetchrow.await_args_list[0].args
        assert args[0] == q.INSERT_LINKER_RELATION
        assert args[3] == "implements_adr"
        assert args[5] == 0.95
        assert args[6]["layer"] == "l2"
        assert args[6]["rationale"] == "manual (memory-granulator)"
        cache_key = verdict_cache_key(GID, CAND_A, "hash-a", "hash-b")
        assert cache_key in redis._store
        cached = json.loads(redis._store[cache_key])
        assert cached["verdict"] == "link" and cached["link_type"] == "implements_adr"
        assert redis._store.get("linker:c:verdict:manual:link") == 1

    @pytest.mark.asyncio
    async def test_manual_verdict_duplicate_never_supersedes(self, mock_pool):
        """duplicate → WARN + метка, ребра и supersede нет (merge без человека)."""
        redis = mock_redis(queue=[queue_item(GID, [CAND_A])])
        linker = self._linker(mock_pool, redis)
        conn = mock_pool.acquire.return_value.__aenter__.return_value

        result = await linker.apply_manual_verdict(GID, CAND_A, "duplicate")

        assert result["link_created"] is False
        assert "auto-supersede NOT started" in result["note"]
        conn.fetchrow.assert_not_called()  # INSERT не выполнялся вовсе

    @pytest.mark.asyncio
    async def test_manual_verdict_cnlm_fallback(self, mock_pool):
        """CNLM-невалидный тип для пары → нейтральный related_to."""
        redis = mock_redis(queue=[queue_item(GID, [CAND_A])])
        linker = self._linker(mock_pool, redis)
        conn = mock_pool.acquire.return_value.__aenter__.return_value

        await linker.apply_manual_verdict(
            GID, CAND_A, "link", link_type="informs"  # вне пары pm→ck
        )

        assert conn.fetchrow.await_args_list[0].args[3] == "related_to"

    @pytest.mark.asyncio
    async def test_manual_verdict_invalid_kind_rejected(self, mock_pool):
        linker = self._linker(mock_pool, mock_redis())
        with pytest.raises(ValueError, match="link\|duplicate\|contradiction\|none"):
            await linker.apply_manual_verdict(GID, CAND_A, "maybe")

    @pytest.mark.asyncio
    async def test_manual_verdict_consumes_pair_both_sides(self, mock_pool):
        """Разобранная пара извлекается из ОБИХ элементов очереди: из элемента
        source удаляется кандидат, опустевший встречный элемент исчезает,
        непустые братья сохраняются."""
        item_source = queue_item(GID, [CAND_A, CAND_B])
        item_mirror = queue_item(CAND_A, [GID])
        item_foreign = queue_item(CAND_B, [GID])
        redis = mock_redis(queue=[item_foreign, item_mirror, item_source])
        linker = self._linker(mock_pool, redis)

        result = await linker.apply_manual_verdict(GID, CAND_A, "link")

        assert result["queue_items_updated"] == 2
        remaining = redis_l2_queue(redis)
        assert len(remaining) == 2
        # источник: остался только CAND_B
        kept_source = next(json.loads(r) for r in remaining if json.loads(r)["granule_id"] == GID)
        assert [c["id"] for c in kept_source["candidates"]] == [CAND_B]
        # встречный элемент CAND_A исчез целиком (была единственная пара)
        assert all(json.loads(r)["granule_id"] != CAND_A for r in remaining)

    def test_l2_mode_matrix(self, mock_pool):
        """llm → "llm"; без llm + manual (дефолт) → "manual"; manual=False → "off"."""
        assert self._linker(mock_pool, mock_redis(), llm=object()).l2_mode() == "llm"
        assert self._linker(mock_pool, mock_redis()).l2_mode() == "manual"
        assert (
            self._linker(mock_pool, mock_redis(), linker_l2_manual=False).l2_mode() == "off"
        )

    @pytest.mark.asyncio
    async def test_stats_reports_l2_mode(self, mock_pool):
        linker = self._linker(mock_pool, mock_redis())  # без llm → manual
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchrow = AsyncMock(return_value={"resolved": 0, "pending": 0})
        stats = await linker.stats()
        assert stats["l2_mode"] == "manual"
        assert stats["l2_enabled"] is False
