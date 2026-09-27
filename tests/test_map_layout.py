"""Полная карта 3D (PLAN_FULL_MAP_3D M2): раскладка.

Слои: чистая математика map_layout (нормировка/релаксация/сфера/seed),
rebuild_layout на моках пула (DrL замокан детерминированной функцией —
igraph здесь не детерминирован между машинами), fallback-ветка,
миграция 024, beat-слот, dirty-съём.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from memory_server.config import Settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.db import queries as ms_q
from memory_server.memory import map_layout, map_service as ms
from memory_server.memory.map_layout import (
    drl_layout,
    normalize_bbox,
    relax_min_distance,
    seed_positions,
    spherical_layout,
)

try:
    import igraph as _igraph_probe  # noqa: F401

    HAS_IGRAPH = True
except ImportError:
    HAS_IGRAPH = False

A1 = "00000000-0000-0000-0000-0000000000a1"
A2 = "00000000-0000-0000-0000-0000000000a2"
A3 = "00000000-0000-0000-0000-0000000000a3"
C1 = "00000000-0000-0000-0000-0000000000c1"


# ══════════════════════════════════════════════════════════════════
# Чистая математика
# ══════════════════════════════════════════════════════════════════


class TestNormalizeBbox:
    def test_fits_cube_and_preserves_shape(self):
        coords = np.array([[0.0, 0.0, 0.0], [500.0, 100.0, -300.0], [-900.0, 0.0, 50.0]])
        result = normalize_bbox(coords, 1000)
        assert np.abs(result).max() <= 1000
        # max-норма растянута точно до границы
        assert np.isclose(np.abs(result).max(), 1000)

    def test_degenerate_all_in_point(self):
        coords = np.zeros((4, 3))
        assert np.abs(normalize_bbox(coords, 1000)).max() == 0


class TestRelaxMinDistance:
    def test_close_pair_pushed_apart(self):
        coords = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        result = relax_min_distance(coords, min_dist=100.0, iterations=5)
        dist = float(np.linalg.norm(result[0] - result[1]))
        assert dist >= 99.0

    def test_far_pair_untouched(self):
        coords = np.array([[0.0, 0.0, 0.0], [500.0, 0.0, 0.0]])
        result = relax_min_distance(coords, min_dist=100.0, iterations=3)
        assert np.allclose(result, coords)

    def test_coincident_points_pushed_apart(self):
        rng_coords = np.repeat(np.array([[1.0, 1.0, 1.0]]), 20, axis=0)
        result = relax_min_distance(rng_coords, min_dist=5.0, iterations=10)
        # полный разлёт гроздьи — все пары дальше порога
        from itertools import combinations

        for i, j in combinations(range(20), 2):
            assert float(np.linalg.norm(result[i] - result[j])) >= 4.9


class TestSphericalLayout:
    def test_deterministic_and_in_bbox(self):
        cluster_of = np.array([0, 0, 1, -1, -1])
        first = spherical_layout(cluster_of, 1000)
        second = spherical_layout(cluster_of, 1000)
        assert np.array_equal(first, second)
        assert np.abs(first).max() <= 1000
        # члены одного кластера рядом (ближе, чем к чужому кластеру)
        same = float(np.linalg.norm(first[0] - first[1]))
        cross = float(np.linalg.norm(first[0] - first[2]))
        assert same < cross

    def test_empty(self):
        assert spherical_layout(np.array([], dtype=np.int64), 1000).shape == (0, 3)


class TestSeedPositions:
    def test_new_node_seeded_from_placed_neighbors(self):
        old = np.full((3, 3), np.nan)
        old[0] = (0.0, 0.0, 0.0)
        old[1] = (10.0, 0.0, 0.0)
        edges = np.array([[0, 2], [1, 2]])
        seed = seed_positions(3, edges, old, 1000)
        # новый узел 2 — среднее соседей 0 и 1
        assert np.allclose(seed[2], (5.0, 0.0, 0.0))
        assert np.allclose(seed[0], (0.0, 0.0, 0.0))

    def test_first_run_returns_none(self):
        # Нет размещённых узлов вовсе — идеальная сфера сидов провоцирует
        # density-grid краш C-core (фикс F1.1): сеять нечего
        seed = seed_positions(3, np.empty((0, 2), dtype=np.int64), np.full((3, 3), np.nan), 1000)
        assert seed is None

    def test_unseeded_tail_gets_deterministic_jitter(self):
        old = np.full((4, 3), np.nan)
        old[0] = (5.0, 5.0, 5.0)
        # рёбра только между новыми узлами: хвост 1..3 без размещённых соседей
        edges = np.array([[1, 2], [2, 3]])
        first = seed_positions(4, edges, old, 1000)
        second = seed_positions(4, edges, old, 1000)
        assert not np.isnan(first).any()
        assert np.allclose(first[0], (5.0, 5.0, 5.0))  # старый на месте
        assert np.array_equal(first, second)  # детерминизм между прогонами
        assert np.abs(first).max() <= 1000
        # джиттер-облако, не идеальная сфера: радиусы хвоста разные
        radii = np.linalg.norm(first[1:], axis=1)
        assert np.ptp(radii) > 1.0


# ── Контракт subprocess-изоляции DrL (daemon-безопасность, 27.09) ──


class TestDaemonSafety:
    """Прод-регрессия 27.09: задача исполняется в daemonic prefork-чайлде
    celery, где multiprocessing.Process.start() падает AssertionError
    «daemonic processes are not allowed to have children». DrL-путь обязан
    работать под daemon'ом: subprocess.Popen daemon-флаг не наследует."""

    def test_drl_layout_survives_daemonic_parent(self):
        import multiprocessing

        ctx = multiprocessing.get_context("spawn")
        recv, send = ctx.Pipe(duplex=False)
        child = ctx.Process(target=_daemon_drl_child, args=(send,), daemon=True)
        child.start()
        try:
            assert recv.poll(120), "daemon-чайлд не ответил (упал до send)"
            result = recv.recv()
        finally:
            child.join(15)
            send.close()
            recv.close()
        assert child.exitcode == 0, f"daemon-чайлд умер: exit={child.exitcode}"
        assert result["ok"] is True, result
        assert result["status"] in ("drl", "sphere"), result


def _daemon_drl_child(conn) -> None:
    """Тело daemonic-потомка: реальный прогон drl_layout (spawn интерпретатора
    воркера). AssertionError от multiprocessing убил бы чайлда до send —
    родитель увидел бы тишину и тест покраснел."""
    try:
        from memory_server.memory import map_layout

        # Без igraph find_spec уводит в sphere ДО запуска потомка; подмена
        # спека гоняет полный путь daemon → subprocess → JSON-канал (внутри
        # потомка честный ImportError → {"status": "no_igraph"} → sphere)
        map_layout.importlib.util.find_spec = lambda name: object()
        coords, status = map_layout.drl_layout(
            3, np.array([[0, 1], [1, 2]]), None, None, timeout=60
        )
        conn.send({
            "ok": status in ("drl", "sphere"),
            "status": status,
            "shape": None if coords is None else list(coords.shape),
        })
    except BaseException as exc:
        try:
            conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        conn.close()


class TestDrlIsolation:
    def test_empty_graph_is_planned_sphere(self):
        result, status = drl_layout(3, np.empty((0, 2), dtype=np.int64), None, None)
        assert (result, status) == (None, "sphere")

    def test_missing_igraph_is_planned_sphere(self, monkeypatch):
        monkeypatch.setattr(
            map_layout.importlib.util, "find_spec", lambda name: None
        )
        result, status = drl_layout(2, np.array([[0, 1]]), None, None)
        assert (result, status) == (None, "sphere")

    def _assume_igraph(self, monkeypatch):
        """Тесты контракта _run_isolated не должны зависеть от наличия
        igraph в окружении: спек «установлен» — гейт пройден, дело доходит
        до (замоканного) потомка."""
        monkeypatch.setattr(
            map_layout.importlib.util, "find_spec", lambda name: object()
        )

    def test_no_answer_from_subprocess_is_failed_fallback(self, monkeypatch):
        # щит F1.3: потомок умер/завис/ошибся — статус drl_failed_fallback
        self._assume_igraph(monkeypatch)
        monkeypatch.setattr(map_layout, "_run_isolated", lambda *a, **kw: None)
        result, status = drl_layout(2, np.array([[0, 1]]), None, None)
        assert (result, status) == (None, "drl_failed_fallback")

    def test_worker_error_is_failed_fallback(self, monkeypatch):
        self._assume_igraph(monkeypatch)
        monkeypatch.setattr(
            map_layout, "_run_isolated", lambda *a, **kw: {"status": "error", "value": "boom"}
        )
        result, status = drl_layout(2, np.array([[0, 1]]), None, None)
        assert (result, status) == (None, "drl_failed_fallback")

    def test_worker_answer_roundtrip(self, monkeypatch):
        """JSON-контракт потомка: {"status": "ok", "value": [[x,y,z],...]}."""
        self._assume_igraph(monkeypatch)
        monkeypatch.setattr(
            map_layout,
            "_run_isolated",
            lambda *a, **kw: {"status": "ok", "value": [[0.5, 1.0, -0.5], [1.5, 2.0, 0.0]]},
        )
        result, status = drl_layout(2, np.array([[0, 1]]), None, None)
        assert status == "drl"
        assert result.shape == (2, 3)
        assert result.dtype == np.float64

    def test_subprocess_crash_returns_none(self, monkeypatch):
        """Потомок убит (exit 137: segfault/OOM-kill) — родитель получает None,
        а не исключение: try/except на segfault не работает в принципе."""
        import subprocess as sp_mod
        from unittest.mock import patch

        with patch.object(
            map_layout.subprocess, "run", return_value=sp_mod.CompletedProcess(
                args=[], returncode=137, stdout=b"", stderr=b"fatal"
            )
        ) as mock_run:
            assert map_layout._run_isolated({"node_count": 1}, timeout=5) is None
        assert mock_run.call_args.kwargs["input"]

    def test_subprocess_timeout_returns_none_fast(self, monkeypatch):
        """Таймаут: subprocess.run прибивает потомка сам и бросает
        TimeoutExpired — _run_isolated переводит его в None."""
        import subprocess as sp_mod
        from unittest.mock import patch

        with patch.object(
            map_layout.subprocess, "run", side_effect=sp_mod.TimeoutExpired(cmd="x", timeout=0.5)
        ):
            assert map_layout._run_isolated({"node_count": 1}, timeout=0.5) is None

    def test_subprocess_garbage_stdout_returns_none(self, monkeypatch):
        import subprocess as sp_mod
        from unittest.mock import patch

        with patch.object(
            map_layout.subprocess, "run", return_value=sp_mod.CompletedProcess(
                args=[], returncode=0, stdout=b"\xff\xfe not json", stderr=b""
            )
        ):
            assert map_layout._run_isolated({"node_count": 1}, timeout=5) is None


@pytest.mark.skipif(not HAS_IGRAPH, reason="python-igraph not installed")
class TestDrlLayout:
    def test_real_igraph_three_dimensions(self):
        edges = np.array([[i, (i + 1) % 6] for i in range(6)])
        weights = np.ones(6)
        seed = seed_positions(6, edges, np.full((6, 3), np.nan), 1000)
        result, status = drl_layout(6, edges, weights, seed, timeout=30)
        assert status == "drl"
        assert result is not None
        assert result.shape == (6, 3)
        assert np.isfinite(result).all()

    def test_seed_from_real_positions_accepted(self):
        edges = np.array([[i, (i + 1) % 8] for i in range(8)])
        old = np.full((8, 3), np.nan)
        old[:4] = ((-100.0, 50.0, 0.0), (100.0, -50.0, 0.0), (0.0, 100.0, -80.0), (10.0, 10.0, 10.0))
        seed = seed_positions(8, edges, old, 1000)
        result, status = drl_layout(8, edges, np.ones(8), seed, timeout=30)
        assert status == "drl" and result is not None


# ══════════════════════════════════════════════════════════════════
# rebuild_layout (моки пула + FakeRedis)
# ══════════════════════════════════════════════════════════════════


def layout_pool(nodes, edges, old_rows=None, layout_exists=True, next_rev=7, executed=None):
    """Мок пула для rebuild_layout: три fetch + execute-журнал."""
    from tests.test_map_snapshot import version_row

    if executed is None:
        executed = []
    pool = MagicMock()
    conn = MagicMock()

    async def fetchrow(sql, *args):
        if sql is ms_q.MAP_VERSION_SQL:
            return version_row(node_count=len(nodes), edge_count=len(edges))
        if sql is ms_q.MAP_LAYOUT_VERSION_SQL:
            return {"layout_rev": 3, "layout_at": None}
        raise AssertionError(sql)

    async def fetchval(sql, *args):
        if sql is ms_q.MAP_LAYOUT_EXISTS_SQL:
            return layout_exists
        if sql is ms_q.MAP_LAYOUT_NEXT_REV_SQL:
            return next_rev
        raise AssertionError(sql)

    async def fetch(sql, *args):
        if sql is ms_q.MAP_LAYOUT_NODES_SQL:
            return nodes
        if sql is ms_q.MAP_LAYOUT_EDGES_SQL:
            return edges
        if sql is ms_q.MAP_LAYOUT_EXISTING_SQL:
            return old_rows or []
        raise AssertionError(sql)

    async def execute(sql, *args):
        executed.append((sql, args))

    conn.fetchrow = fetchrow
    conn.fetchval = fetchval
    conn.fetch = fetch
    conn.execute = execute
    acm = AsyncMock()
    acm.__aenter__.return_value = conn
    acm.__aexit__.return_value = None
    pool.acquire.return_value = acm
    return pool


def make_layout_service(pool, redis):
    async def redis_provider():
        return redis

    return ms.MapService(
        pool=pool,
        redis_provider=redis_provider,
        project_repository=MagicMock(),
        runtime=RuntimeConfig(db_values={}),
    )


def deterministic_drl(node_count, edge_indices, weights, seed, timeout=None):
    """Детерминированная замена DrL: seed + линейный сдвиг по индексу."""
    base = seed if seed is not None else np.zeros((node_count, 3))
    coords = np.asarray(base, dtype=np.float64) + np.linspace(-50, 50, node_count)[:, None]
    return coords, "drl"


LAYOUT_NODES = [
    {"id": A1, "cluster_id": C1},
    {"id": A2, "cluster_id": C1},
    {"id": A3, "cluster_id": None},
]
LAYOUT_EDGES = [{"source_id": A1, "target_id": A2, "weight": 0.8}]


class TestRebuildLayout:
    @pytest.mark.asyncio
    async def test_upserts_all_nodes_with_rev(self, monkeypatch):
        from tests.test_map_snapshot import FakeRedis

        monkeypatch.setattr(map_layout, "drl_layout", deterministic_drl)
        executed: list = []
        old = [{"node_id": A1, "x": -100.0, "y": 0.0, "z": 50.0}]
        redis = FakeRedis()
        redis.data[ms.DIRTY_KEY.encode()] = b"1"

        result = await make_layout_service(
            layout_pool(LAYOUT_NODES, LAYOUT_EDGES, old_rows=old, executed=executed), redis
        ).rebuild_layout()

        assert result["ok"] is True and result["method"] == "drl"
        assert result["rev"] == 7
        sql, args = executed[-1]
        assert sql is ms_q.MAP_LAYOUT_UPSERT_SQL
        assert args[0] == [A1, A2, A3]      # все узлы получили координаты
        assert args[4] == 7                  # rev
        for axis in (args[1], args[2], args[3]):
            assert all(-1000 <= v <= 1000 for v in axis)
        # dirty снят; маркер no-op — версия ПОСЛЕ UPSERT (фикс F3)
        assert not await redis.exists(ms.DIRTY_KEY)
        assert redis.data[ms.LAYOUT_GRAPH_KEY.encode()] == result["version"].encode()

    @pytest.mark.asyncio
    async def test_rebuild_idempotent_noop(self, monkeypatch):
        """F3: повторный rebuild без изменений — no-op, UPSERT не повторяется."""
        from tests.test_map_snapshot import FakeRedis

        monkeypatch.setattr(map_layout, "drl_layout", deterministic_drl)
        executed: list = []
        redis = FakeRedis()
        redis.data[ms.DIRTY_KEY.encode()] = b"1"
        service = make_layout_service(
            layout_pool(LAYOUT_NODES, LAYOUT_EDGES, executed=executed), redis
        )

        first = await service.rebuild_layout()
        assert first.get("rev") == 7
        upserts_after_first = len(executed)

        # граф не менялся, dirty снят — повтор обязан быть no-op
        second = await service.rebuild_layout()
        assert second == {"noop": True, "version": first["version"]}
        assert len(executed) == upserts_after_first  # rev не перекатывается

    @pytest.mark.asyncio
    async def test_noop_when_graph_unchanged(self):
        from tests.test_map_snapshot import FakeRedis

        redis = FakeRedis()
        pool = layout_pool(LAYOUT_NODES, LAYOUT_EDGES)
        service = make_layout_service(pool, redis)
        # meta() и rebuild_layout считают версию одного и того же мок-пула
        meta = await service.meta()
        redis.data[ms.LAYOUT_GRAPH_KEY.encode()] = meta["version"].encode()

        result = await service.rebuild_layout()
        assert result == {"noop": True, "version": meta["version"]}

    @pytest.mark.asyncio
    async def test_fallback_sphere_when_drl_unavailable(self, monkeypatch):
        from tests.test_map_snapshot import FakeRedis

        def sphere_drl(*args, **kwargs):
            return None, "sphere"

        monkeypatch.setattr(map_layout, "drl_layout", sphere_drl)
        executed: list = []
        redis = FakeRedis()
        redis.data[ms.DIRTY_KEY.encode()] = b"1"

        result = await make_layout_service(
            layout_pool(LAYOUT_NODES, LAYOUT_EDGES, executed=executed), redis
        ).rebuild_layout()

        assert result["ok"] is True and result["method"] == "sphere"
        sql, args = executed[-1]
        assert sql is ms_q.MAP_LAYOUT_UPSERT_SQL
        for axis in (args[1], args[2], args[3]):
            assert all(-1000 <= v <= 1000 for v in axis)

    @pytest.mark.asyncio
    async def test_drl_death_reports_failed_fallback_and_metric(self, monkeypatch):
        """F1: смерть DrL-субпроцесса — статус drl_failed_fallback + счётчик."""
        from memory_server.metrics import MAP_LAYOUT_FALLBACKS
        from tests.test_map_snapshot import FakeRedis

        def dead_drl(*args, **kwargs):
            return None, "drl_failed_fallback"

        monkeypatch.setattr(map_layout, "drl_layout", dead_drl)
        executed: list = []
        redis = FakeRedis()
        redis.data[ms.DIRTY_KEY.encode()] = b"1"

        before = MAP_LAYOUT_FALLBACKS.labels(reason="drl_failed")._value.get()
        result = await make_layout_service(
            layout_pool(LAYOUT_NODES, LAYOUT_EDGES, executed=executed), redis
        ).rebuild_layout()

        assert result["ok"] is True
        assert result["method"] == "drl_failed_fallback"
        after = MAP_LAYOUT_FALLBACKS.labels(reason="drl_failed")._value.get()
        assert after - before == 1
        # раскладка при этом состоялась (сферическая) — воркер жив
        assert len(executed) == 1

    @pytest.mark.asyncio
    async def test_drl_death_keeps_existing_layout(self, monkeypatch):
        """Приказ Мастера 27.09: потомок DrL умер, а карта уже размещена —
        прежний layout неприкосновенен (сфера НЕ перетасовывает карту),
        UPSERT не выполнялся, dirty остаётся взведённым для ретрая."""
        from tests.test_map_snapshot import FakeRedis

        def dead_drl(*args, **kwargs):
            return None, "drl_failed_fallback"

        monkeypatch.setattr(map_layout, "drl_layout", dead_drl)
        executed: list = []
        redis = FakeRedis()
        redis.data[ms.DIRTY_KEY.encode()] = b"1"
        old = [{"node_id": A1, "x": -100.0, "y": 0.0, "z": 50.0}]

        result = await make_layout_service(
            layout_pool(LAYOUT_NODES, LAYOUT_EDGES, old_rows=old, executed=executed), redis
        ).rebuild_layout()

        assert result["ok"] is False and result["reason"] == "drl_failed"
        assert executed == []  # координаты не тронуты
        assert await redis.exists(ms.DIRTY_KEY)  # часовой тик ретрает

    @pytest.mark.asyncio
    async def test_pending_migration_graceful(self):
        from tests.test_map_snapshot import FakeRedis

        redis = FakeRedis()
        redis.data[ms.DIRTY_KEY.encode()] = b"1"
        result = await make_layout_service(
            layout_pool(LAYOUT_NODES, LAYOUT_EDGES, layout_exists=False), redis
        ).rebuild_layout()
        assert result == {"ok": False, "reason": "migration 024 pending"}

    @pytest.mark.asyncio
    async def test_seeding_from_existing_layout(self, monkeypatch):
        """Новый узел стартует от среднего соседей: фиксируем seed DrL."""
        from tests.test_map_snapshot import FakeRedis

        captured: dict = {}

        def spy_drl(node_count, edge_indices, weights, seed, timeout=None):
            captured["seed"] = np.array(seed, dtype=np.float64)
            return deterministic_drl(node_count, edge_indices, weights, seed)

        monkeypatch.setattr(map_layout, "drl_layout", spy_drl)
        old = [{"node_id": A1, "x": 0.0, "y": 0.0, "z": 0.0},
               {"node_id": A2, "x": 100.0, "y": 0.0, "z": 0.0}]
        edges = [{"source_id": A1, "target_id": A3, "weight": 1.0},
                 {"source_id": A2, "target_id": A3, "weight": 1.0}]
        nodes = [{"id": A1, "cluster_id": None}, {"id": A2, "cluster_id": None},
                 {"id": A3, "cluster_id": None}]
        redis = FakeRedis()
        redis.data[ms.DIRTY_KEY.encode()] = b"1"

        await make_layout_service(layout_pool(nodes, edges, old_rows=old), redis).rebuild_layout()

        seed = captured["seed"]
        assert np.allclose(seed[0], (0.0, 0.0, 0.0))     # старые на месте
        assert np.allclose(seed[2], (50.0, 0.0, 0.0))    # новый = среднее соседей


# ══════════════════════════════════════════════════════════════════
# Миграция 024 + beat-слот
# ══════════════════════════════════════════════════════════════════


class TestMigrationAndSchedule:
    def test_migration_024_shape(self):
        sql = (Path(__file__).resolve().parents[1] / "migrations" / "024_map_layout.sql").read_text(
            encoding="utf-8"
        )
        assert "CREATE TABLE IF NOT EXISTS map_layout" in sql
        assert "node_id    UUID PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE" in sql
        assert "x          REAL NOT NULL" in sql
        assert "rev        INTEGER NOT NULL DEFAULT 1" in sql
        assert "CREATE INDEX IF NOT EXISTS idx_map_layout_rev" in sql
        assert "ALTER TABLE map_layout OWNER TO svc_athene_ai" in sql
        assert "DROP TABLE IF EXISTS map_layout" in sql  # DOWN-секция

    def test_beat_slot_hourly_after_linker(self):
        from celery.schedules import crontab

        from memory_server.celery_app import app

        schedule = app.conf.beat_schedule
        # Приказ Мастера 27.09: слот layout-map вернулся на layout_map и
        # стал часовым (:10, сразу после часового co_occurrence :00);
        # galactic_layout остаётся ручным force-пересевом без beat-слота
        assert schedule["layout-map"]["task"] == (
            "memory_server.tasks.map_tasks.layout_map"
        )
        # crontab(hour="*") разворачивается в диапазон 0..23 — сверка
        # через __eq__ (все поля ежечасных расписаний совпадают с литералом)
        assert schedule["layout-map"]["schedule"] == crontab(minute=10)
        assert schedule["linker-co-occurrence"]["schedule"] == crontab(minute=0)
