"""Galactic Layout v2 (GALACTIC_LAYOUT.md GL-1/GL-2): астрофизическая раскладка.

Слои: чистая математика galactic_layout (космогония §1, Fiedler-порядок и
порез рукавов §2.1, посадка, инкремент §4, релаксация §2.4), MapService.
layout_galaxy на моках пула (контракты TRUNCATE / INSERT IGNORE, noop,
dirty-съём), регистрация таски + beat-слот.

Критерии Катерины (§8), проверяемые юнит-уровнем: побитовый детерминизм,
объём галактики/NaN, баланс рукавов ±20%, медиана ребра ≤150, балдж
топ-64, гало Пламмера, Fiedler на мини-графе.
"""

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from memory_server.config import Settings
from memory_server.db import queries as ms_q
from memory_server.memory import galactic_layout as gl
from memory_server.memory import map_service as ms


def uid(i: int) -> str:
    return f"00000000-0000-0000-0000-{i:012d}"


def chain_input(
    n_clusters: int = 120,
    per_cluster: int = 30,
    satellites: int = 30,
    lonely: int = 25,
    seed: int = 5,
) -> gl.GalacticInput:
    """Синтетика с читаемой структурой: 4 цепочки кластеров со слабыми швами
    (Fiedler-порядок и порез рукавов имеют что упорядочивать), спутники с
    рёбрами в кластеры и полностью изолированные одиночки."""
    rng = np.random.default_rng(seed)
    node_ids: list[str] = []
    cluster_ids: list[str | None] = []
    importance: list[float] = []
    edges: list[tuple[int, int]] = []
    weights: list[float] = []

    n_nodes = n_clusters * per_cluster + satellites + lonely
    for g in range(n_clusters):
        chain_id = f"cluster-{g:04d}"
        for k in range(per_cluster):
            node_ids.append(uid(len(node_ids)))
            cluster_ids.append(chain_id)
            importance.append(float(rng.integers(1, 6)))
    for _ in range(satellites + lonely):
        node_ids.append(uid(len(node_ids)))
        cluster_ids.append(None)
        importance.append(float(rng.integers(1, 6)))

    # внутригрупповые цепочки + редкие внутригрупповые доп. рёбра
    for g in range(n_clusters):
        base = g * per_cluster
        for k in range(per_cluster - 1):
            edges.append((base + k, base + k + 1))
            weights.append(1.0)
    # мосты между соседними кластерами; швы цепочек (каждая 1/4) — без моста
    seam = n_clusters // 4
    for g in range(n_clusters - 1):
        if (g + 1) % seam == 0:
            continue
        base = g * per_cluster
        edges.append((base, base + per_cluster))
        weights.append(2.0)
    # спутники: по ребру в случайный кластерный узел
    sat_base = n_clusters * per_cluster
    for s in range(satellites):
        edges.append((sat_base + s, int(rng.integers(0, n_clusters * per_cluster))))
        weights.append(0.7)
    assert len(node_ids) == n_nodes
    return gl.GalacticInput(
        node_ids, cluster_ids, np.array(importance),
        np.array(edges), np.array(weights),
    )


# ══════════════════════════════════════════════════════════════════
# Космогония (§1)
# ══════════════════════════════════════════════════════════════════


class TestCosmography:
    def test_spiral_point_follows_log_spiral(self):
        # θ=0 → r=120 около старта рукава; θ_max → r=900 (край диска)
        start = np.array([
            gl.arm_place(np.random.default_rng(s), 0.0, 0) for s in range(50)
        ])
        radii = np.linalg.norm(start[:, :2], axis=1)
        assert float(radii.mean()) == pytest.approx(gl.SPIRAL_A, abs=60)
        assert float(np.abs(start[:, 2]).max()) < 120  # σ_z(120)≈24, 3σ гуляет

    def test_theta_max_reaches_disk_edge(self):
        radius = gl.SPIRAL_A * float(np.exp(gl.SPIRAL_B * gl.THETA_MAX))
        assert radius == pytest.approx(gl.R_DISK, rel=1e-9)
        assert gl.THETA_MAX == pytest.approx(7.2, abs=0.1)  # ~1.15 витка

    def test_cluster_sigma_log_growth_with_cap(self):
        assert gl.cluster_sigma(3) == pytest.approx(14 + 7 * np.log(3), rel=1e-9)
        assert gl.cluster_sigma(10) == pytest.approx(30, abs=1)
        assert gl.cluster_sigma(10_000) == gl.SIGMA_CL_CAP

    def test_halo_plummer_radii_bounded(self):
        points = np.array([gl.halo_point(np.random.default_rng(s)) for s in range(400)])
        radii = np.linalg.norm(points, axis=1)
        assert float(radii.max()) <= gl.R_HALO + 1e-9   # клип 1600 (§1.3)
        assert float(radii.min()) > 150                 # u≥0.05 → r≳237
        assert float(np.median(radii)) > 400            # медиана Пламмера ~783

    def test_bulge_places_min_distance_and_extent(self):
        places = gl.bulge_places(gl.BULGE_TOP)
        assert places.shape == (gl.BULGE_TOP, 3)
        diff = places[:, None, :] - places[None, :, :]
        dist = np.linalg.norm(diff, axis=-1)
        np.fill_diagonal(dist, np.inf)
        assert float(dist.min()) >= 0.9 * gl.BULGE_MIN_DIST  # релаксация ±10%
        assert np.abs(places[:, :2]).max() < 500             # ~4σ гауссианы
        # побитовая воспроизводимость мест хабов
        assert np.array_equal(places, gl.bulge_places(gl.BULGE_TOP))

    def test_hash_arm_slot_deterministic_and_spread(self):
        arm, s = gl.hash_arm_slot("cluster-0001")
        assert 0 <= arm < gl.N_ARMS and 0.0 <= s <= 1.0
        assert gl.hash_arm_slot("cluster-0001") == (arm, s)
        arms = {gl.hash_arm_slot(f"cluster-{i:04d}")[0] for i in range(200)}
        assert arms == set(range(gl.N_ARMS))  # изолированные не слипаются в один рукав


# ══════════════════════════════════════════════════════════════════
# Спектральный порядок (§2.1)
# ══════════════════════════════════════════════════════════════════


def two_blobs_adjacency() -> np.ndarray:
    """Клика A (0..4) — мост — клика B (5..9): классика Fiedler."""
    w = np.zeros((10, 10))
    for blob in (range(5), range(5, 10)):
        for i in blob:
            for j in blob:
                if i != j:
                    w[i, j] = w[j, i] = 1.0
    w[4, 5] = w[5, 4] = 0.5
    return w


class TestFiedler:
    def test_two_blobs_separated_by_order(self):
        order = gl.fiedler_order(two_blobs_adjacency())
        assert sorted(order[:5].tolist()) == [0, 1, 2, 3, 4]
        assert sorted(order[5:].tolist()) == [5, 6, 7, 8, 9]

    def test_path_graph_ordered_along_path(self):
        n = 8
        w = np.zeros((n, n))
        for i in range(n - 1):
            w[i, i + 1] = w[i + 1, i] = 1.0
        order = gl.fiedler_order(w)
        # Fiedler path-графа монотонен: порядок — путь от конца до конца
        steps = np.diff(order)
        assert bool(np.all(steps == 1)) or bool(np.all(steps == -1))

    def test_isolated_vertices_excluded(self):
        w = two_blobs_adjacency()
        w[9, :] = w[:, 9] = 0.0  # 9-я — изолированная
        order = gl.fiedler_order(w)
        assert 9 not in order.tolist()

    def test_spectral_order_big_component_first(self):
        # несвязанные компоненты 5 и 3: крупная впереди (стабильность пореза)
        w = np.zeros((8, 8))
        for i, j in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 0), (1, 3)):
            w[i, j] = w[j, i] = 1.0
        for i, j in ((5, 6), (6, 7), (7, 5)):
            w[i, j] = w[j, i] = 1.0
        order = gl.spectral_order(w)
        assert set(order[:5].tolist()) == {0, 1, 2, 3, 4}
        assert set(order[5:].tolist()) == {5, 6, 7}

    def test_cut_into_arms_balanced_mass(self):
        # 4 цепочки по 10 вершин, швы с нулевой связностью → 4 равных рукава
        n = 40
        w = np.zeros((n, n))
        for block in range(4):
            for i in range(block * 10, block * 10 + 9):
                w[i, i + 1] = w[i + 1, i] = 1.0
        order = np.arange(n)
        arm_of, slot = gl.cut_into_arms(order, w, np.ones(n))
        counts = [int((arm_of == a).sum()) for a in range(gl.N_ARMS)]
        assert counts == [10, 10, 10, 10]
        assert 0.0 <= slot.min() and slot.max() <= 1.0

    def test_cut_prefers_weak_seams_inside_window(self):
        # цепочка с ОДНИМ слабым мостом в центре: разрез стремится к мосту
        n = 40
        w = np.zeros((n, n))
        for i in range(n - 1):
            weight = 0.1 if i == 19 else 5.0
            w[i, i + 1] = w[i + 1, i] = weight
        arm_of, _ = gl.cut_into_arms(np.arange(n), w, np.ones(n))
        assert arm_of[19] != arm_of[20]  # мост разрезан


# ══════════════════════════════════════════════════════════════════
# Полный пересев (GL-1)
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def full_result():
    inp = chain_input()
    coords, report = gl.layout_full(inp)
    return inp, coords, report


class TestLayoutFull:
    def test_bitwise_deterministic(self, full_result):
        inp, coords, _ = full_result
        rerun, _ = gl.layout_full(inp)
        assert rerun.tobytes() == coords.tobytes()  # §3: перноудовые сиды

    def test_all_positions_finite_and_inside_galaxy(self, full_result):
        _, coords, _ = full_result
        assert np.isfinite(coords).all()
        radii = np.linalg.norm(coords, axis=1)
        assert float(radii.max()) <= gl.R_HALO + 1e-6  # объём галактики §1

    def test_regions_populated(self, full_result):
        inp, _, report = full_result
        n_satellites = 30
        n_lonely = 25
        assert report.regions[gl.REGION_ARM] > 0
        assert report.regions[gl.REGION_BULGE] > 0      # кластеров 120 > 64
        assert report.regions[gl.REGION_HALO] == n_lonely
        assert report.regions[gl.REGION_SATELLITE] == n_satellites

    def test_arm_balance_within_20_percent(self, full_result):
        _, _, report = full_result
        assert report.arm_balance_pct is not None
        assert report.arm_balance_pct <= 20.0  # §8.3: нет «пустого рукава»

    def test_median_edge_length_below_150(self, full_result):
        _, _, report = full_result
        assert report.edge_median_len is not None
        assert report.edge_median_len <= 150.0  # §8.3, R_disk=900

    def test_bulge_nodes_near_core(self, full_result):
        inp, coords, report = full_result
        group_of, group_keys = gl.assign_groups(inp)
        mass = gl._group_masses(group_of, inp.importance, len(group_keys))
        bulge_groups = np.argsort(-mass, kind="stable")[: gl.BULGE_TOP]
        bulge_nodes = np.isin(group_of, bulge_groups)
        assert report.regions[gl.REGION_BULGE] == int(bulge_nodes.sum())
        assert float(np.abs(coords[bulge_nodes, :2]).max()) < 500  # ~4σ(90)
        assert float(np.abs(coords[bulge_nodes, 2]).max()) < 400   # σ_cl·0.6 ≤ 36

    def test_thin_disk_vertical_extent(self, full_result):
        inp, coords, _ = full_result
        clustered = np.array([c is not None for c in inp.cluster_ids])
        assert clustered.any()
        # 3·(σ_z(900) + 0.6·σ_cl)_max = 3·(36+36) = 216 — тонкий диск §8.3
        assert float(np.abs(coords[clustered, 2]).max()) <= 250

    def test_satellites_near_anchor_clusters(self, full_result):
        inp, coords, report = full_result
        sat_base = 120 * 30  # спутники идут после кластерных узлов синтетики
        satellites = coords[sat_base : sat_base + 30]
        clustered = coords[:sat_base]
        for point in satellites:
            dist = np.linalg.norm(clustered - point, axis=1)
            assert float(dist.min()) < 600  # 3σ_field + σ_cl


# ══════════════════════════════════════════════════════════════════
# Инкремент (§4, GL-2)
# ══════════════════════════════════════════════════════════════════


class TestLayoutIncrement:
    def test_only_todo_nodes_returned_and_placed_untouched(self):
        inp = chain_input()
        base, _ = gl.layout_full(inp)
        placed = base.copy()
        placed[-80:] = np.nan  # «новые»: хвост спутников и одиночек
        todo, coords, report = gl.layout_increment(inp, placed)
        assert len(todo) == 80 and report.placed == 80
        assert np.isfinite(coords).all()
        # старые строки не пересчитываются: todo — ровно узлы без координат
        assert set(todo.tolist()) == set(range(len(base) - 80, len(base)))

    def test_new_cluster_member_near_placed_centroid(self):
        inp = chain_input(n_clusters=80, per_cluster=10, satellites=5, lonely=3)
        base, _ = gl.layout_full(inp)
        placed = base.copy()
        placed[:4] = np.nan  # 4 «новых» члена первого кластера
        todo, coords, _ = gl.layout_increment(inp, placed)
        centroid = base[4:10].mean(axis=0)
        for point in coords:
            assert float(np.linalg.norm(point - centroid)) < 150  # ~3σ_cl(m=10)

    def test_lonely_node_goes_to_halo(self):
        inp = chain_input(n_clusters=80, per_cluster=10, satellites=2, lonely=1)
        base, _ = gl.layout_full(inp)
        placed = base.copy()
        last = len(base) - 1
        placed[last] = np.nan  # одиночка без рёбер
        todo, coords, report = gl.layout_increment(inp, placed)
        assert report.regions[gl.REGION_HALO] == 1
        radius = float(np.linalg.norm(coords[0]))
        assert radius > 200  # гало, не диск

    def test_empty_placement_processes_everything(self):
        inp = chain_input(n_clusters=80, per_cluster=10, satellites=2, lonely=3)
        placed = np.full((len(inp.node_ids), 3), np.nan)
        todo, coords, report = gl.layout_increment(inp, placed)
        assert len(todo) == len(inp.node_ids)
        assert np.isfinite(coords).all()

    def test_no_todo_is_noop(self):
        inp = chain_input(n_clusters=80, per_cluster=10, satellites=2, lonely=2)
        base, _ = gl.layout_full(inp)
        todo, coords, report = gl.layout_increment(inp, base)
        assert report.placed == 0 and len(coords) == 0

    def test_increment_deterministic(self):
        inp = chain_input(n_clusters=80, per_cluster=10, satellites=4, lonely=3)
        base, _ = gl.layout_full(inp)
        placed = base.copy()
        placed[7] = placed[100] = np.nan
        first = gl.layout_increment(inp, placed)[1]
        second = gl.layout_increment(inp, placed)[1]
        assert first.tobytes() == second.tobytes()


# ══════════════════════════════════════════════════════════════════
# Релаксация §2.4
# ══════════════════════════════════════════════════════════════════


class TestRelaxInGroups:
    def test_group_stays_compact_and_finite(self):
        inp = chain_input(n_clusters=8, per_cluster=40, satellites=0, lonely=0)
        coords, _ = gl.layout_full(inp)
        group_of, _ = gl.assign_groups(inp)
        center_before = coords[group_of == 0].mean(axis=0)
        spread = float(np.abs(coords[group_of == 0] - center_before).max())
        assert np.isfinite(coords).all()
        assert spread < 250  # сгусток, не разлёт: 4σ_cl(m=40)≈180


# ══════════════════════════════════════════════════════════════════
# MapService.layout_galaxy (моки пула)
# ══════════════════════════════════════════════════════════════════


GAL_NODES = [{"id": uid(i), "cluster_id": "cluster-0001" if i < 2 else None,
              "importance": 3} for i in range(3)]
GAL_EDGES = [{"source_id": uid(0), "target_id": uid(1), "weight": 1.0}]


def galaxy_pool(nodes, edges, old_rows=None, layout_exists=True, next_rev=9,
                executed=None):
    if executed is None:
        executed = []
    from tests.test_map_snapshot import version_row

    pool = MagicMock()
    conn = MagicMock()

    async def fetchrow(sql, *args):
        if sql is ms_q.MAP_VERSION_SQL:
            return version_row(node_count=len(nodes), edge_count=len(edges))
        if sql is ms_q.MAP_LAYOUT_VERSION_SQL:
            return {"layout_rev": 8, "layout_at": None}
        raise AssertionError(sql)

    async def fetchval(sql, *args):
        if sql is ms_q.MAP_LAYOUT_EXISTS_SQL:
            return layout_exists
        if sql is ms_q.MAP_LAYOUT_NEXT_REV_SQL:
            return next_rev
        raise AssertionError(sql)

    async def fetch(sql, *args):
        if sql is ms_q.GALACTIC_NODES_SQL:
            return nodes
        if sql is ms_q.MAP_LAYOUT_EDGES_SQL:
            return edges
        if sql is ms_q.MAP_LAYOUT_EXISTING_SQL:
            return old_rows or []
        raise AssertionError(sql)

    async def execute(sql, *args):
        executed.append((sql, args))
        if sql is ms_q.MAP_LAYOUT_TRUNCATE_SQL:
            return "TRUNCATE TABLE"
        if sql is ms_q.MAP_LAYOUT_INSERT_IGNORE_SQL:
            return f"INSERT 0 {len(args[0])}"
        raise AssertionError(sql)

    class _Tx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return None

    conn.fetchrow = fetchrow
    conn.fetchval = fetchval
    conn.fetch = fetch
    conn.execute = execute
    conn.transaction = MagicMock(return_value=_Tx())
    acm = AsyncMock()
    acm.__aenter__.return_value = conn
    acm.__aexit__.return_value = None
    pool.acquire.return_value = acm
    return pool


def make_galaxy_service(pool, redis):
    async def redis_provider():
        return redis

    return ms.MapService(
        pool=pool,
        redis_provider=redis_provider,
        project_repository=MagicMock(),
        config=Settings(),
    )


class TestServiceLayoutGalaxy:
    @pytest.mark.asyncio
    async def test_force_truncates_and_ignores_conflicts(self):
        from tests.test_map_snapshot import FakeRedis

        executed: list = []
        redis = FakeRedis()
        redis.data[ms.DIRTY_KEY.encode()] = b"1"
        result = await make_galaxy_service(
            galaxy_pool(GAL_NODES, GAL_EDGES, executed=executed), redis
        ).layout_galaxy(force=True)

        assert result["ok"] is True and result["mode"] == "full"
        assert result["rev"] == 9
        sqls = [sql for sql, _ in executed]
        assert ms_q.MAP_LAYOUT_TRUNCATE_SQL in sqls  # force сносит сферу (§3)
        inserts = [(sql, args) for sql, args in executed
                   if sql is ms_q.MAP_LAYOUT_INSERT_IGNORE_SQL]
        assert inserts and all(len(args) == 5 for _, args in inserts)
        inserted_ids = [i for _, args in inserts for i in args[0]]
        assert inserted_ids == [n["id"] for n in GAL_NODES]  # все узлы
        # dirty снят, версия перечитана ПОСЛЕ вставки — no-op маркер честный
        assert not await redis.exists(ms.DIRTY_KEY)
        assert ms.LAYOUT_GRAPH_KEY.encode() in redis.data

    @pytest.mark.asyncio
    async def test_increment_touches_only_new_nodes(self):
        from tests.test_map_snapshot import FakeRedis

        executed: list = []
        old = [{"node_id": uid(0), "x": 1.0, "y": 2.0, "z": 3.0}]
        result = await make_galaxy_service(
            galaxy_pool(GAL_NODES, GAL_EDGES, old_rows=old, executed=executed),
            FakeRedis(),
        ).layout_galaxy()

        assert result["ok"] is True and result["mode"] == "incremental"
        sqls = [sql for sql, _ in executed]
        assert ms_q.MAP_LAYOUT_TRUNCATE_SQL not in sqls  # старые неприкосновенны
        inserted_ids = [i for sql, args in executed
                        if sql is ms_q.MAP_LAYOUT_INSERT_IGNORE_SQL
                        for i in args[0]]
        assert uid(0) not in inserted_ids            # размещённый не трогаем
        assert set(inserted_ids) == {uid(1), uid(2)}  # только новые

    @pytest.mark.asyncio
    async def test_noop_when_nothing_to_place(self):
        from tests.test_map_snapshot import FakeRedis

        old = [{"node_id": n["id"], "x": 1.0, "y": 2.0, "z": 3.0}
               for n in GAL_NODES]
        executed: list = []
        result = await make_galaxy_service(
            galaxy_pool(GAL_NODES, GAL_EDGES, old_rows=old, executed=executed),
            FakeRedis(),
        ).layout_galaxy()
        assert result["noop"] is True
        assert not executed  # ни одной записи в БД — rev не тратится

    @pytest.mark.asyncio
    async def test_pending_migration_graceful(self):
        from tests.test_map_snapshot import FakeRedis

        result = await make_galaxy_service(
            galaxy_pool(GAL_NODES, GAL_EDGES, layout_exists=False), FakeRedis()
        ).layout_galaxy(force=True)
        assert result == {"ok": False, "reason": "migration 024 pending"}

    @pytest.mark.asyncio
    async def test_placed_metric_incremented(self):
        from tests.test_map_snapshot import FakeRedis

        from memory_server.metrics import GALACTIC_LAYOUT_PLACED

        before = sum(
            GALACTIC_LAYOUT_PLACED.labels(mode="incremental", region=region)
            ._value.get()
            for region in ("arm", "bulge", "halo", "satellite")
        )
        await make_galaxy_service(
            galaxy_pool(GAL_NODES, GAL_EDGES), FakeRedis()
        ).layout_galaxy()
        after = sum(
            GALACTIC_LAYOUT_PLACED.labels(mode="incremental", region=region)
            ._value.get()
            for region in ("arm", "bulge", "halo", "satellite")
        )
        assert after - before == len(GAL_NODES)


# ══════════════════════════════════════════════════════════════════
# Таска, beat-слот, SQL-контракты
# ══════════════════════════════════════════════════════════════════


class TestTaskAndContracts:
    def test_task_registered(self):
        from memory_server.celery_app import app

        assert "memory_server.tasks.map_tasks.galactic_layout" in app.tasks

    def test_beat_slot_taken_from_drl(self):
        from memory_server.celery_app import app

        schedule = app.conf.beat_schedule
        # galactic занимает слот layout_map (§7): 02:30 UTC после кластеров
        assert schedule["layout-map"]["task"] == (
            "memory_server.tasks.map_tasks.galactic_layout"
        )
        cron = schedule["layout-map"]["schedule"]
        assert (set(cron.hour), set(cron.minute)) == ({2}, {30})

    def test_drl_task_survives_until_v2_acceptance(self):
        from memory_server.celery_app import app

        # старый путь жив до приёмки v2 — удаляется вместе со щитом (§7)
        assert "memory_server.tasks.map_tasks.layout_map" in app.tasks

    def test_insert_ignore_never_updates_existing_rows(self):
        sql = ms_q.MAP_LAYOUT_INSERT_IGNORE_SQL
        assert "ON CONFLICT (node_id) DO NOTHING" in sql
        assert "DO UPDATE" not in sql  # размещённые строки неприкосновенны

    def test_truncate_statement(self):
        assert "TRUNCATE TABLE map_layout" in ms_q.MAP_LAYOUT_TRUNCATE_SQL
