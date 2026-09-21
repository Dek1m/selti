"""Полная карта 3D (PLAN_FULL_MAP_3D M1): сборка снапшота и кеш.

Слои: чистая сборка columnar (build_snapshot без PG), усечения, кеш
меты, get-or-build под lock (FakeRedis + мок pool), dirty-bump,
version-хэш. REST-контракт — test_map_api.py, раскладка — test_map_layout.py.
"""

import gzip
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.memory import map_service as ms
from memory_server.memory.map_service import MapService, build_snapshot, truncate_preview

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


def version_row(node_count=2, edge_count=1, cluster_count=1):
    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "cluster_count": cluster_count,
        "mem_updated": NOW,
        "rel_created": NOW,
    }


def node_row(nid, **kw):
    row = {
        "id": nid,
        "entity_name": "Имя гранулы",
        "content": "контент гранулы",
        "namespace": "project_meta",
        "cluster_id": None,
        "importance": 3,
        "frozen": False,
        "x": None,
        "y": None,
        "z": None,
    }
    row.update(kw)
    return row


def edge_row(src, tgt, link_type="related_to", weight=1.0):
    return {"source_id": src, "target_id": tgt, "link_type": link_type, "weight": weight}


class FakeRedis:
    """Минимальный бинарный Redis: bytes-ключи/значения, nx/ex, scan_iter."""

    def __init__(self):
        self.data: dict[bytes, bytes] = {}
        self.ttl: dict[bytes, int] = {}
        self.get_calls: list[bytes] = []

    @staticmethod
    def _k(key) -> bytes:
        return key if isinstance(key, bytes) else str(key).encode()

    async def get(self, key):
        key = self._k(key)
        self.get_calls.append(key)
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False):
        key = self._k(key)
        if isinstance(value, str):
            value = value.encode()
        if nx and key in self.data:
            return None
        self.data[key] = bytes(value)
        if ex is not None:
            self.ttl[key] = ex
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            key = self._k(key)
            removed += int(self.data.pop(key, None) is not None)
            self.ttl.pop(key, None)
        return removed

    async def exists(self, key) -> int:
        return int(self._k(key) in self.data)

    async def expire(self, key, ttl):
        if self._k(key) in self.data:
            self.ttl[self._k(key)] = ttl
            return True
        return False

    async def scan_iter(self, match=b"map:snap:*"):
        import fnmatch

        pattern = match.decode() if isinstance(match, bytes) else match
        for key in list(self.data):
            if fnmatch.fnmatch(key.decode(), pattern):
                yield key


def map_pool(
    v_row=None,
    nodes=None,
    edges=None,
    clusters=None,
    layout_row=None,
    layout_exists=True,
    next_rev=7,
    old_rows=None,
):
    """Мок asyncpg.Pool, маршрутизирующий по SQL-константам queries.py."""
    fetched_sql: list[str] = []
    pool = MagicMock()
    conn = MagicMock()

    async def fetchrow(sql, *args):
        fetched_sql.append(sql)
        if sql is q.MAP_VERSION_SQL:
            return v_row if v_row is not None else version_row()
        if sql is q.MAP_LAYOUT_VERSION_SQL:
            return layout_row if layout_row is not None else {"layout_rev": 3, "layout_at": NOW}
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(sql, *args):
        fetched_sql.append(sql)
        if sql is q.MAP_LAYOUT_EXISTS_SQL:
            return layout_exists
        if sql is q.MAP_LAYOUT_NEXT_REV_SQL:
            return next_rev
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(sql, *args):
        fetched_sql.append(sql)
        if sql is q.MAP_NODES_SQL or sql is q.MAP_NODES_NO_LAYOUT_SQL:
            return nodes or []
        if sql is q.MAP_EDGES_SQL:
            return edges or []
        if sql is q.MAP_CLUSTERS_SQL:
            return clusters or []
        if sql is q.MAP_LAYOUT_NODES_SQL:
            return nodes or []
        if sql is q.MAP_LAYOUT_EDGES_SQL:
            return edges or []
        if sql is q.MAP_LAYOUT_EXISTING_SQL:
            return old_rows or []
        raise AssertionError(f"unexpected fetch: {sql}")

    conn.fetchrow = fetchrow
    conn.fetchval = fetchval
    conn.fetch = fetch
    acm = AsyncMock()
    acm.__aenter__.return_value = conn
    acm.__aexit__.return_value = None
    pool.acquire.return_value = acm
    pool.fetched_sql = fetched_sql
    return pool


def make_service(pool, redis, **config_overrides):
    async def redis_provider():
        return redis

    project_repo = AsyncMock()
    project_repo.resolve_id = AsyncMock(side_effect=lambda key: key)
    return MapService(
        pool=pool,
        redis_provider=redis_provider,
        project_repository=project_repo,
        config=Settings(**config_overrides),
    )


# ══════════════════════════════════════════════════════════════════
# Усечение preview / name
# ══════════════════════════════════════════════════════════════════


class TestTruncatePreview:
    def test_short_text_untouched(self):
        assert truncate_preview("короткий текст", 180) == "короткий текст"

    def test_long_text_cut_on_word_boundary_with_ellipsis(self):
        words = " ".join(f"слово{i}" for i in range(100))
        result = truncate_preview(words, 50)
        assert len(result) <= 51  # 50 + «…»
        assert result.endswith("…")
        # не рвёт слово: перед «…» — конец слова или пробел срезан
        assert not result[:-1].endswith(" ")

    def test_long_text_without_spaces_hard_cut(self):
        result = truncate_preview("а" * 300, 50)
        assert len(result) == 51
        assert result.endswith("…")

    def test_none_passthrough(self):
        assert truncate_preview(None, 180) is None


# ══════════════════════════════════════════════════════════════════
# Columnar-контракт снапшота
# ══════════════════════════════════════════════════════════════════


class TestBuildSnapshot:
    def _fixture(self):
        nodes = [
            node_row("00000000-0000-0000-0000-0000000000a1", x=-500.0, y=0.0, z=100.0, cluster_id="c-1", frozen=True, importance=5),
            node_row("00000000-0000-0000-0000-0000000000a2", x=300.0, y=250.0, z=-50.0, cluster_id="c-1"),
            node_row("00000000-0000-0000-0000-0000000000a3", x=0.0, y=-800.0, z=0.0),
        ]
        edges = [
            edge_row("00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000a2"),
            edge_row("00000000-0000-0000-0000-0000000000a2", "00000000-0000-0000-0000-0000000000a3", "solves", 0.7),
        ]
        clusters = [{"id": "c-1", "namespace": "project_meta", "label": "кластер", "member_count": 2}]
        return nodes, edges, clusters

    def test_columnar_contract(self):
        nodes, edges, clusters = self._fixture()
        snap = build_snapshot("ver123", nodes, edges, clusters, True, 180, 80, 1000)

        assert snap["v"] == "ver123"
        assert snap["ns"] == ["project_meta"]
        assert snap["et"] == ["related_to", "solves"]
        # кластеры сузились до используемых, m — member_count из таблицы
        assert snap["clusters"] == [{"i": 0, "ns": 0, "label": "кластер", "m": 2}]

        assert len(snap["nodes"]) == 3
        first = snap["nodes"][0]
        assert len(first) == 10
        assert first[0] == "00000000-0000-0000-0000-0000000000a1"
        assert first[3] == 0          # nsIdx
        assert first[4] == 0          # clusterIdx
        assert first[5] == 5          # size = importance
        assert first[6] == 1          # flags: frozen
        assert first[7:10] == [-500, 0, 100]
        assert snap["nodes"][2][4] == -1  # вне кластера

        assert snap["edges"] == [[0, 1, 0, 1.0], [1, 2, 1, 0.7]]
        # индексы рёбер в диапазоне узлов
        for src, tgt, tidx, _ in snap["edges"]:
            assert 0 <= src < 3 and 0 <= tgt < 3 and 0 <= tidx < len(snap["et"])

    def test_preview_off_keeps_name(self):
        nodes, edges, clusters = self._fixture()
        snap = build_snapshot("v", nodes, edges, clusters, False, 180, 80, 1000)
        assert snap["nodes"][0][2] is None
        assert snap["nodes"][0][1] == "Имя гранулы"

    def test_missing_coords_get_spherical_fallback(self):
        nodes, edges, clusters = self._fixture()
        snap = build_snapshot("v", nodes, edges, clusters, True, 180, 80, 1000)
        for row in snap["nodes"]:
            assert row[7] is not None and row[8] is not None and row[9] is not None
            assert all(-1000 <= c <= 1000 for c in row[7:10])
        # детерминизм: тот же вход — те же координаты
        snap2 = build_snapshot("v", nodes, edges, clusters, True, 180, 80, 1000)
        assert [r[7:10] for r in snap["nodes"]] == [r[7:10] for r in snap2["nodes"]]

    def test_edges_to_unknown_nodes_dropped(self):
        nodes = [node_row("00000000-0000-0000-0000-0000000000a1", x=1.0, y=2.0, z=3.0)]
        edges = [edge_row("00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-00000000dead")]
        snap = build_snapshot("v", nodes, edges, [], True, 180, 80, 1000)
        assert snap["edges"] == []


# ══════════════════════════════════════════════════════════════════
# Мета: version-хэш, кеш, layout_stale
# ══════════════════════════════════════════════════════════════════


class TestMeta:
    @pytest.mark.asyncio
    async def test_version_changes_on_granule_insert(self):
        # Инсерт гранулы: node_count вырос → другая version (мок данных
        # вместо мока времени: хэш зависит только от состояния таблиц)
        s1 = make_service(map_pool(v_row=version_row(node_count=100)), FakeRedis())
        s2 = make_service(map_pool(v_row=version_row(node_count=101)), FakeRedis())
        m1, m2 = await s1.meta(), await s2.meta()
        assert m1["version"] != m2["version"]
        assert m1["node_count"] == 100 and m2["node_count"] == 101

    @pytest.mark.asyncio
    async def test_layout_absent_before_migration_024(self):
        redis = FakeRedis()
        service = make_service(map_pool(layout_exists=False, layout_row=None), redis)
        meta = await service.meta()
        assert meta["layout_at"] is None
        # layout_rev=0 в хэше — не падает до применения 024
        assert len(meta["version"]) == 12

    @pytest.mark.asyncio
    async def test_snapshot_builds_without_layout_table(self):
        """F2: /full не падает до применения 024 — guard выбирает SQL без
        LEFT JOIN, узлы получают сферические координаты."""
        redis = FakeRedis()
        nodes = [
            node_row(f"00000000-0000-0000-0000-{i:012d}", x=None, y=None, z=None)
            for i in range(2)
        ]
        pool = map_pool(nodes=nodes, edges=[], clusters=[], layout_exists=False)
        report = await make_service(pool, redis).ensure_snapshot(True, None, None)

        assert report["cached"] is False  # не упало — снапшот собран и отдан
        assert q.MAP_NODES_NO_LAYOUT_SQL in pool.fetched_sql
        payload = json.loads(gzip.decompress(redis.data[report["key"].encode()]))
        for row in payload["nodes"]:
            assert row[7] is not None and -1000 <= row[7] <= 1000  # сфера

    def test_version_sql_ignores_dangling_edges(self):
        """F4: маркер свежести рёбер — только по резолвленным; висячие
        (target_id IS NULL) снапшот не меняют и инвалидировать его не могут."""
        normalized = " ".join(q.MAP_VERSION_SQL.split())
        assert "max(created_at) FROM relations WHERE target_id IS NOT NULL" in normalized

    @pytest.mark.asyncio
    async def test_meta_cached_in_redis(self):
        redis = FakeRedis()
        pool = map_pool()
        service = make_service(pool, redis)
        first = await service.meta()
        assert ms.META_KEY.encode() in redis.data
        # второй вызов — из кеша, без PG (fetchrow не растёт)
        pg_calls = len(pool.fetched_sql)
        second = await service.meta()
        assert second == first
        assert len(pool.fetched_sql) == pg_calls

    @pytest.mark.asyncio
    async def test_layout_stale_flag(self):
        redis = FakeRedis()
        service = make_service(map_pool(), redis)
        meta = await service.meta()
        assert await service.layout_stale(meta["version"]) is False
        await service.bump_dirty()
        assert await service.layout_stale(meta["version"]) is True


# ══════════════════════════════════════════════════════════════════
# ensure_snapshot: кеш / сборка / lock / TTL старых версий
# ══════════════════════════════════════════════════════════════════


class TestEnsureSnapshot:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_build(self):
        redis = FakeRedis()
        pool = map_pool(nodes=[node_row("00000000-0000-0000-0000-0000000000a1", x=1, y=2, z=3)])
        service = make_service(pool, redis)
        first = await service.ensure_snapshot(True, None, None)
        assert first["cached"] is False
        key = first["key"]
        assert key in [k.decode() for k in redis.data]

        pg_calls = len(pool.fetched_sql)
        second = await service.ensure_snapshot(True, None, None)
        assert second["cached"] is True
        assert second["key"] == key
        assert len(pool.fetched_sql) == pg_calls

    @pytest.mark.asyncio
    async def test_build_stores_gzip_parseable_json(self):
        redis = FakeRedis()
        nodes = [node_row(f"00000000-0000-0000-0000-{i:012d}", x=1.0, y=2.0, z=3.0) for i in range(3)]
        edges = [edge_row(nodes[0]["id"], nodes[1]["id"])]
        service = make_service(map_pool(nodes=nodes, edges=edges, clusters=[]), redis)

        report = await service.ensure_snapshot(True, None, None)
        assert report["cached"] is False
        assert report["bytes"] > 0

        payload = json.loads(gzip.decompress(redis.data[report["key"].encode()]))
        assert payload["v"] == report["version"]
        assert len(payload["nodes"]) == 3
        # координаты layout доехали до снапшота
        assert payload["nodes"][0][7:10] == [1, 2, 3]
        assert payload["edges"] == [[0, 1, 0, 1.0]]

    @pytest.mark.asyncio
    async def test_filters_change_cache_key(self):
        redis = FakeRedis()
        service = make_service(map_pool(), redis)
        full = await service.ensure_snapshot(True, None, None)
        filtered = await service.ensure_snapshot(True, "selti", "project_meta")
        assert full["key"] != filtered["key"]
        assert "selti" in filtered["key"]

    @pytest.mark.asyncio
    async def test_lock_waits_for_competitor(self, monkeypatch):
        async def instant_sleep(_seconds):
            return None

        monkeypatch.setattr(ms, "_sleep", instant_sleep)

        gz = gzip.compress(b'{"v":"x"}')
        meta_json = ms._json_bytes({
            "version": "ver123", "node_count": 0, "edge_count": 0,
            "cluster_count": 0, "layout_at": None,
        })
        redis = FakeRedis()
        redis.data[ms.META_KEY.encode()] = meta_json
        redis.data[b"map:lock:1|-|-"] = b"1"  # конкурент держит lock

        # ключ появляется после первого опроса (конкурент дописал)
        snap_key = "map:snap:ver123:1|-|-"
        original_get = redis.get
        polls = {"count": 0}

        async def get_after_first_poll(key):
            value = await original_get(key)
            if key == snap_key and value is None:
                polls["count"] += 1
                if polls["count"] >= 1:
                    redis.data[snap_key.encode()] = gz
                    return gz
            return value

        redis.get = get_after_first_poll
        pool = map_pool()
        service = make_service(pool, redis)

        report = await service.ensure_snapshot(True, None, None)
        assert report["cached"] is True
        # сборки не было: MAP_NODES_SQL не выполнялся
        assert q.MAP_NODES_SQL not in pool.fetched_sql

    @pytest.mark.asyncio
    async def test_stalled_when_wait_exhausted(self):
        redis = FakeRedis()
        meta_json = ms._json_bytes({
            "version": "ver123", "node_count": 0, "edge_count": 0,
            "cluster_count": 0, "layout_at": None,
        })
        redis.data[ms.META_KEY.encode()] = meta_json
        redis.data[b"map:lock:1|-|-"] = b"1"  # lock занят, ключ не появляется

        service = make_service(map_pool(), redis, map_build_wait_seconds=0)
        report = await service.ensure_snapshot(True, None, None)
        assert report.get("stalled") is True

    @pytest.mark.asyncio
    async def test_stale_versions_get_short_ttl(self):
        redis = FakeRedis()
        redis.data[b"map:snap:oldver:1|-|-"] = b"old"
        service = make_service(map_pool(), redis)

        report = await service.ensure_snapshot(True, None, None)
        # старая версия — EXPIRE 300, текущая живёт без короткого TTL
        assert redis.ttl.get(b"map:snap:oldver:1|-|-") == 300
        current = report["key"].encode()
        assert redis.ttl.get(current) != 300


# ══════════════════════════════════════════════════════════════════
# Dirty-bump
# ══════════════════════════════════════════════════════════════════


class TestBumpDirty:
    @pytest.mark.asyncio
    async def test_bump_clears_snapshots_and_meta(self):
        redis = FakeRedis()
        redis.data[ms.META_KEY.encode()] = b"{}"
        redis.data[b"map:snap:v1:1|-|-"] = b"gz"
        redis.data[b"map:snap:v2:0|-|-"] = b"gz"

        await make_service(map_pool(), redis).bump_dirty()

        assert await redis.exists(ms.DIRTY_KEY)
        assert ms.META_KEY.encode() not in redis.data
        assert not [k for k in redis.data if k.startswith(b"map:snap:")]
