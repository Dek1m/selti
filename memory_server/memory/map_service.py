"""Снапшот и раскладка Полной карты 3D (PLAN_FULL_MAP_3D, M1/M2).

MapService — сервис уровня MemoryService: PG (только чтение снапшота,
запись map_layout) + Redis (кеш gz-байтов, мета, dirty-флаг, build-lock).
Вызывается исключительно Celery-задачами (tasks/map_tasks.py) — единый
путь исполнения REST/MCP; web-процесс читает готовые gz-байты из Redis.

Redis-ключи:
    map:meta                  — JSON меты (кеш 60с, PLAN §2.5 <50мс)
    map:snap:<ver>:<suffix>   — gz-байты снапшота (orjson+gzip)
    map:lock:<suffix>         — Celery-lock против параллельных билдов
    map:dirty                 — рёбра/кластеры менялись после layout
    map:layout:graph          — version-хэш последнего прогона layout

Версия снапшота: sha1(count(memories asserted), count(relations target_id
NOT NULL), max(memories.updated_at), max(relations.created_at), layout_rev)
— дёшево, ловит и вставку, и supersession, и новую раскладку. Изменения,
которые хэш не видит (кластеры, переписи весов рёбер), покрывает dirty-bump
от reconciler/refresh_clusters: снос map:snap:* → ленивая пересборка.
"""

from __future__ import annotations

import gzip
import hashlib
import time
from typing import Any, Callable, Iterable

import numpy as np

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.logger import get_logger
from memory_server.memory import galactic_layout as galaxy
from memory_server.memory import map_layout
from memory_server.memory.project_repository import ProjectRepository
from memory_server.metrics import (
    GALACTIC_EDGE_MEDIAN_LEN,
    GALACTIC_LAYOUT_PLACED,
    GALACTIC_LAYOUT_SECONDS,
    MAP_CACHE_HITS,
    MAP_LAYOUT_FALLBACKS,
    MAP_LAYOUT_SECONDS,
    MAP_SNAPSHOT_BUILD_SECONDS,
    MAP_SNAPSHOT_BYTES,
)

logger = get_logger(__name__)

try:
    import orjson

    def _json_bytes(payload: Any) -> bytes:
        return orjson.dumps(payload)
except ImportError:  # orjson опционален локально — прод-образ ставит всегда
    import json

    def _json_bytes(payload: Any) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


META_KEY = "map:meta"
SNAP_KEY_PREFIX = "map:snap:"
LOCK_KEY_PREFIX = "map:lock:"
DIRTY_KEY = "map:dirty"
LAYOUT_GRAPH_KEY = "map:layout:graph"

# Раз в сколько опрашиваем Redis, ждём билд-конкурента под lock
_BUILD_POLL_INTERVAL = 0.5

# Батч INSERT IGNORE галактики (§5): statement_timeout не должен ловить
# одиночную вставку 15k строк
_LAYOUT_BATCH = 5000


def truncate_preview(text: str | None, limit: int) -> str | None:
    """Усечение по границе слова + «…» (решение Мастера 1): русский контент
    без слов-границ в хвосте рвётся посреди — иначе лимит теряет смысл."""
    if text is None:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit * 0.75:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def _snap_suffix(with_preview: bool, project_id: str | None, namespace: str | None) -> str:
    """Суффикс ключа кеша: снапшот зависит не только от версии графа."""
    return f"{int(with_preview)}|{project_id or '-'}|{namespace or '-'}"


def build_snapshot(
    version: str,
    node_rows: Iterable[Any],
    edge_rows: Iterable[Any],
    cluster_rows: Iterable[Any],
    with_preview: bool,
    preview_chars: int,
    name_chars: int,
    bbox: int,
) -> dict[str, Any]:
    """Columnar-снапшот (PLAN §3). Чистая функция — напрямую тестируется.

    nodes: [id, name(80), preview(180|null), nsIdx, clusterIdx|-1, size,
    flags(frozen=1), x, y, z]; рёбра ссылаются индексами узлов, et —
    словарь типов рёбер (расширение плана: без него typeIdx нечитаем).
    Узлы без координат map_layout получают сферический fallback (M1 —
    карта жива до первого прогона layout_map).
    """
    nodes = list(node_rows)
    edges = list(edge_rows)
    clusters = list(cluster_rows)

    ns_list = sorted({r["namespace"] for r in nodes})
    ns_index = {ns: i for i, ns in enumerate(ns_list)}

    used_clusters = {r["cluster_id"] for r in nodes if r["cluster_id"] is not None}
    clusters = sorted(
        (c for c in clusters if c["id"] in used_clusters), key=lambda c: c["id"]
    )
    cluster_index = {c["id"]: i for i, c in enumerate(clusters)}

    node_index = {r["id"]: i for i, r in enumerate(nodes)}
    missing = {i for i, r in enumerate(nodes) if r["x"] is None}
    sphere: dict[int, np.ndarray] = {}
    if missing:
        missing_sorted = sorted(missing)
        cluster_of_missing = np.array(
            [cluster_index.get(nodes[i]["cluster_id"], -1) for i in missing_sorted],
            dtype=np.int64,
        )
        sphere = dict(
            zip(missing_sorted, map_layout.spherical_layout(cluster_of_missing, bbox * 0.9))
        )

    nodes_out: list[list[Any]] = []
    for i, row in enumerate(nodes):
        if i in sphere:
            x, y, z = (int(v) for v in sphere[i])
        else:
            x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
        nodes_out.append([
            row["id"],
            truncate_preview(row["entity_name"], name_chars) or "",
            truncate_preview(row["content"], preview_chars) if with_preview else None,
            ns_index[row["namespace"]],
            cluster_index.get(row["cluster_id"], -1),
            int(row["importance"] or 3),
            1 if row["frozen"] else 0,
            x, y, z,
        ])

    edge_types = sorted({r["link_type"] for r in edges})
    type_index = {t: i for i, t in enumerate(edge_types)}
    edges_out: list[list[Any]] = []
    for row in edges:
        src = node_index.get(row["source_id"])
        tgt = node_index.get(row["target_id"])
        if src is None or tgt is None:
            continue  # ребро за пределами фильтра — страховка индексов
        edges_out.append([src, tgt, type_index[row["link_type"]], float(row["weight"])])

    return {
        "v": version,
        "ns": ns_list,
        "et": edge_types,
        "clusters": [
            {
                "i": i,
                "ns": ns_index.get(c["namespace"], 0),
                "label": c["label"],
                "m": int(c["member_count"]),
            }
            for i, c in enumerate(clusters)
        ],
        "nodes": nodes_out,
        "edges": edges_out,
    }


class MapService:
    """Снапшот /api/map/full + мета + раскладка layout_map.

    redis_provider возвращает БИНАРНЫЙ клиент (decode_responses=False):
    в кеше лежат gz-байты — текстовый клиент их молча испортит.
    """

    def __init__(
        self,
        pool: Any,
        redis_provider: Callable[[], Any],
        project_repository: ProjectRepository,
        config: Settings,
    ) -> None:
        self._pool = pool
        self._redis_provider = redis_provider
        self._project_repo = project_repository
        self._config = config

    async def _redis(self) -> Any:
        return await self._redis_provider()

    # ── Мета: version + счётчики (кеш 60с) ──────────────────────────

    async def meta(self) -> dict[str, Any]:
        redis = await self._redis()
        try:
            cached = await redis.get(META_KEY)
            if cached is not None:
                MAP_CACHE_HITS.inc()
                return _meta_from_json(cached)
        except Exception as exc:
            logger.warning("map: meta cache read failed", extra={"error": str(exc)[:200]})
        computed = await self._compute_meta()
        try:
            await redis.set(META_KEY, _json_bytes(computed), ex=self._config.map_meta_ttl)
        except Exception as exc:
            logger.warning("map: meta cache write failed", extra={"error": str(exc)[:200]})
        return computed

    async def _compute_meta(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(q.MAP_VERSION_SQL)
            layout_row = None
            if await conn.fetchval(q.MAP_LAYOUT_EXISTS_SQL):
                layout_row = await conn.fetchrow(q.MAP_LAYOUT_VERSION_SQL)
        version = hashlib.sha1("|".join(str(part) for part in (
            row["node_count"], row["edge_count"], row["mem_updated"],
            row["rel_created"], layout_row["layout_rev"] if layout_row else 0,
        )).encode()).hexdigest()[:12]
        return {
            "version": version,
            "node_count": int(row["node_count"]),
            "edge_count": int(row["edge_count"]),
            "cluster_count": int(row["cluster_count"]),
            "layout_at": (
                layout_row["layout_at"].isoformat()
                if layout_row and layout_row["layout_at"]
                else None
            ),
        }

    async def layout_stale(self, meta_version: str) -> bool:
        """Честный бейдж HUD: рёбра/кластеры менялись после последнего layout."""
        try:
            redis = await self._redis()
            last = await redis.get(LAYOUT_GRAPH_KEY)
            if last is not None and last.decode() == meta_version:
                return False
            return bool(await redis.exists(DIRTY_KEY))
        except Exception:
            return False

    # ── Снапшот: get-or-build под lock ─────────────────────────────

    async def ensure_snapshot(
        self, with_preview: bool, project_id: str | None, namespace: str | None
    ) -> dict[str, Any]:
        meta = await self.meta()
        suffix = _snap_suffix(with_preview, project_id, namespace)
        key = f"{SNAP_KEY_PREFIX}{meta['version']}:{suffix}"
        lock_key = f"{LOCK_KEY_PREFIX}{suffix}"
        redis = await self._redis()

        cached = await redis.get(key)
        if cached is not None:
            MAP_CACHE_HITS.inc()
            return {"cached": True, "key": key, "bytes": len(cached), **meta}

        started = time.monotonic()
        project_uuid = await self._resolve_project(project_id)
        locked = await redis.set(lock_key, "1", nx=True, ex=120)
        if not locked:
            # Конкурент собирает тот же suffix: ждём ключ, не дублируем работу
            gz = await self._await_snapshot(redis, key)
            if gz is not None:
                return {"cached": True, "key": key, "bytes": len(gz), **meta}
            return {"stalled": True, "key": key, **meta}

        try:
            gz = await self._build_snapshot_gz(
                meta["version"], with_preview, project_uuid, namespace
            )
            await redis.set(key, gz, ex=self._config.map_snapshot_ttl)
            await self._expire_stale_snapshots(redis, meta["version"])
        finally:
            await redis.delete(lock_key)
        elapsed = time.monotonic() - started
        MAP_SNAPSHOT_BUILD_SECONDS.observe(elapsed)
        MAP_SNAPSHOT_BYTES.observe(len(gz))
        logger.info(
            "map: snapshot built",
            extra={"version": meta["version"], "bytes": len(gz), "seconds": round(elapsed, 3)},
        )
        return {"cached": False, "key": key, "bytes": len(gz), **meta}

    async def _await_snapshot(self, redis: Any, key: str) -> bytes | None:
        deadline = time.monotonic() + self._config.map_build_wait_seconds
        while time.monotonic() < deadline:
            gz = await redis.get(key)
            if gz is not None:
                return gz
            await _sleep(_BUILD_POLL_INTERVAL)
        return None

    async def _build_snapshot_gz(
        self, version: str, with_preview: bool, project_uuid: str | None, namespace: str | None
    ) -> bytes:
        async with self._pool.acquire() as conn:
            # До применения 024 map_layout нет — симметричный guard меты и
            # rebuild_layout (фикс F2): SQL без LEFT JOIN, координаты NULL
            # → сборка подставит сферический fallback, /full отвечает 200
            nodes_sql = q.MAP_NODES_SQL
            if not await conn.fetchval(q.MAP_LAYOUT_EXISTS_SQL):
                nodes_sql = q.MAP_NODES_NO_LAYOUT_SQL
            node_rows = await conn.fetch(nodes_sql, namespace, project_uuid)
            edge_rows = await conn.fetch(q.MAP_EDGES_SQL, namespace, project_uuid)
            cluster_rows = await conn.fetch(q.MAP_CLUSTERS_SQL, namespace)
        snapshot = build_snapshot(
            version, node_rows, edge_rows, cluster_rows, with_preview,
            self._config.map_preview_chars, self._config.map_name_chars,
            self._config.map_layout_bbox,
        )
        return gzip.compress(_json_bytes(snapshot), compresslevel=6)

    async def _expire_stale_snapshots(self, redis: Any, version: str) -> None:
        """EXPIRE 300 ключам чужих версий (PLAN §7: Redis не копит 2.5МБ × N)."""
        current = version.encode()
        async for key in redis.scan_iter(match=f"{SNAP_KEY_PREFIX}*"):
            parts = key.split(b":")
            if len(parts) >= 3 and parts[2] != current:
                await redis.expire(key, self._config.map_stale_ttl)
    async def _resolve_project(self, project_id: str | None) -> str | None:
        if project_id is None:
            return None
        return await self._project_repo.resolve_id(project_id)

    # ── Раскладка (M2): beat-таска layout_map ──────────────────────

    async def rebuild_layout(self) -> dict[str, Any]:
        """DrL dim=3 (seeding старых координат) → bbox → релаксация → UPSERT.

        Идемпотентность: без dirty и с неизменным version-хэшем — no-op.
        Fallback: DrL/igraph недоступны → сферическая раскладка кластеров.
        """
        started = time.monotonic()
        meta = await self._compute_meta()
        redis = await self._redis()
        if not await redis.exists(DIRTY_KEY):
            last = await redis.get(LAYOUT_GRAPH_KEY)
            if last is not None and last.decode() == meta["version"]:
                return {"noop": True, "version": meta["version"]}

        async with self._pool.acquire() as conn:
            if not await conn.fetchval(q.MAP_LAYOUT_EXISTS_SQL):
                return {"ok": False, "reason": "migration 024 pending"}
            node_rows = await conn.fetch(q.MAP_LAYOUT_NODES_SQL)
            edge_rows = await conn.fetch(q.MAP_LAYOUT_EDGES_SQL)
            old_rows = await conn.fetch(q.MAP_LAYOUT_EXISTING_SQL)
            rev = int(await conn.fetchval(q.MAP_LAYOUT_NEXT_REV_SQL))

        index = {row["id"]: i for i, row in enumerate(node_rows)}
        edge_list: list[tuple[int, int]] = []
        weights: list[float] = []
        for row in edge_rows:
            src = index.get(row["source_id"])
            tgt = index.get(row["target_id"])
            if src is not None and tgt is not None:
                edge_list.append((src, tgt))
                weights.append(float(row["weight"]))
        edge_indices = np.array(edge_list, dtype=np.int64).reshape(-1, 2)
        weight_array = np.array(weights, dtype=np.float64)

        bbox = self._config.map_layout_bbox
        old_coords = np.full((len(node_rows), 3), np.nan)
        for row in old_rows:
            pos = index.get(row["node_id"])
            if pos is not None:
                old_coords[pos] = (row["x"], row["y"], row["z"])

        seed = map_layout.seed_positions(len(node_rows), edge_indices, old_coords, bbox)
        coords, drl_status = map_layout.drl_layout(
            len(node_rows), edge_indices, weight_array, seed,
            timeout=self._config.map_drl_timeout,
        )
        method = drl_status
        if coords is None:
            if drl_status == map_layout.DRL_FAILED:
                MAP_LAYOUT_FALLBACKS.labels(reason="drl_failed").inc()
            cluster_ids = {row["cluster_id"] for row in node_rows if row["cluster_id"]}
            cluster_pos = {cid: i for i, cid in enumerate(sorted(cluster_ids))}
            cluster_of = np.array(
                [cluster_pos.get(row["cluster_id"], -1) for row in node_rows], dtype=np.int64
            )
            coords = map_layout.spherical_layout(cluster_of, bbox)
        else:
            coords = map_layout.normalize_bbox(coords, bbox)
            coords = map_layout.relax_min_distance(
                coords, self._config.map_min_dist, self._config.map_relax_iterations
            )
            coords = np.clip(coords, -bbox, bbox)

        node_ids = [row["id"] for row in node_rows]
        async with self._pool.acquire() as conn:
            await conn.execute(
                q.MAP_LAYOUT_UPSERT_SQL,
                node_ids,
                coords[:, 0].tolist(),
                coords[:, 1].tolist(),
                coords[:, 2].tolist(),
                rev,
            )
        await redis.delete(DIRTY_KEY)
        # Фикс F3: маркер no-op пишется ПОСЛЕ UPSERT и пересчитывается —
        # версия несёт НОВЫЙ rev. Иначе прошлая запись (rev-1) никогда не
        # совпадала с будущим подсчётом (rev) и каждая ночь гоняла layout
        # впустую, перекачивая снапшот всем клиентам.
        post_meta = await self._compute_meta()
        await redis.set(LAYOUT_GRAPH_KEY, post_meta["version"])
        elapsed = time.monotonic() - started
        MAP_LAYOUT_SECONDS.observe(elapsed)
        logger.info(
            "map: layout rebuilt",
            extra={
                "method": method, "nodes": len(node_rows), "edges": len(edge_list),
                "rev": rev, "seconds": round(elapsed, 3),
            },
        )
        return {
            "ok": True, "method": method, "nodes": len(node_rows),
            "edges": len(edge_list), "rev": rev, "version": post_meta["version"],
            "seconds": round(elapsed, 3),
        }

    # ── Galactic Layout v2 (GALACTIC_LAYOUT.md GL-1/GL-2) ───────────

    async def layout_galaxy(self, force: bool = False) -> dict[str, Any]:
        """Астрофизическая раскладка (спираль + балдж + гало) вместо DrL.

        force=False (beat): только гранулы без строки map_layout — правила
        инкремента §4; размещённые строки не пересчитываются НИКОГДА.
        force=True: полный побитово детерминированный пересев (перноудовые
        RNG §3) — только ручной запуск по команде Мастера/Рэя. Снапшот и
        API не меняются: rev в version-хэше сам инвалидирует ETag.
        """
        started = time.monotonic()
        async with self._pool.acquire() as conn:
            if not await conn.fetchval(q.MAP_LAYOUT_EXISTS_SQL):
                return {"ok": False, "reason": "migration 024 pending"}
            # ПЕРВОЙ фазой — дешёвые COUNTы: воркер жив, даже если корпус
            # перерос память-профиль таски (прод-OOM 20.09). Карта остаётся
            # на сфере; порог поднять после подтверждения прод-замеров.
            scale = await conn.fetchrow(q.GALACTIC_SCALE_SQL)
        counts = tuple(int(scale[name]) for name in ("node_count", "edge_count", "cluster_count"))
        limits = (
            self._config.galactic_max_nodes,
            self._config.galactic_max_edges,
            self._config.galactic_max_clusters,
        )
        if any(c > lim for c, lim in zip(counts, limits)):
            logger.warning(
                "galactic layout skipped: dataset too large for current memory profile",
                extra={"nodes": counts[0], "edges": counts[1], "clusters": counts[2],
                       "limits": dict(zip(("nodes", "edges", "clusters"), limits))},
            )
            return {"ok": True, "skipped": True,
                    "reason": "dataset too large for current memory profile",
                    "nodes": counts[0], "edges": counts[1], "clusters": counts[2]}

        async with self._pool.acquire() as conn:
            node_rows = await conn.fetch(q.GALACTIC_NODES_SQL)
            edge_rows = await conn.fetch(q.MAP_LAYOUT_EDGES_SQL)
            old_rows = await conn.fetch(q.MAP_LAYOUT_EXISTING_SQL)

        index = {row["id"]: i for i, row in enumerate(node_rows)}
        edge_list: list[tuple[int, int]] = []
        edge_weights: list[float] = []
        for row in edge_rows:
            src = index.get(row["source_id"])
            tgt = index.get(row["target_id"])
            if src is not None and tgt is not None:
                edge_list.append((src, tgt))
                edge_weights.append(float(row["weight"]))
        placed_coords = np.full((len(node_rows), 3), np.nan)
        for row in old_rows:
            pos = index.get(row["node_id"])
            if pos is not None:
                placed_coords[pos] = (row["x"], row["y"], row["z"])
        inp = galaxy.GalacticInput(
            node_ids=[row["id"] for row in node_rows],
            cluster_ids=[row["cluster_id"] for row in node_rows],
            importance=[float(row["importance"] or 0.0) for row in node_rows],
            edges=edge_list,
            edge_weights=edge_weights,
        )
        # Records (104k рёбер ≈ десятки МБ) и списки-посредники не нужны:
        # GalacticInput держит собственные numpy-копии — подушка под расчёт.
        node_id_list = inp.node_ids
        del node_rows, edge_rows, old_rows, edge_list, edge_weights, index

        if force:
            todo_indices = np.arange(len(node_id_list), dtype=np.int64)
            coords, report = galaxy.layout_full(inp)
        else:
            todo_indices, coords, report = galaxy.layout_increment(inp, placed_coords)

        elapsed = time.monotonic() - started
        if report.placed == 0:
            return {"ok": True, "noop": True, "force": force,
                    "seconds": round(elapsed, 3)}

        # Расчёт вне транзакции (секунды eigh/релаксации не держат блокировки);
        # TRUNCATE+INSERT атомарны, rev читается ДО сноса — поколение живёт
        # через пересев. ON CONFLICT DO NOTHING: конкурент уже разместил —
        # пропускаем, старые строки неприкосновенны.
        inserted, rev = 0, 0
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rev = int(await conn.fetchval(q.MAP_LAYOUT_NEXT_REV_SQL))
                if force:
                    await conn.execute(q.MAP_LAYOUT_TRUNCATE_SQL)
                for start in range(0, len(todo_indices), _LAYOUT_BATCH):
                    stop = start + _LAYOUT_BATCH
                    status = await conn.execute(
                        q.MAP_LAYOUT_INSERT_IGNORE_SQL,
                        [node_id_list[i] for i in todo_indices[start:stop]],
                        coords[start:stop, 0].tolist(),
                        coords[start:stop, 1].tolist(),
                        coords[start:stop, 2].tolist(),
                        rev,
                    )
                    inserted += int(status.rsplit(" ", 1)[-1]) if status else 0

        redis = await self._redis()
        await redis.delete(DIRTY_KEY)
        await redis.delete(META_KEY)
        async for key in redis.scan_iter(match=f"{SNAP_KEY_PREFIX}*"):
            await redis.delete(key)
        post_meta = await self._compute_meta()
        await redis.set(LAYOUT_GRAPH_KEY, post_meta["version"])

        GALACTIC_LAYOUT_SECONDS.labels(mode=report.mode).observe(elapsed)
        for region, count in report.regions.items():
            if count:
                GALACTIC_LAYOUT_PLACED.labels(mode=report.mode, region=region).inc(count)
        if report.edge_median_len is not None:
            GALACTIC_EDGE_MEDIAN_LEN.set(report.edge_median_len)
        logger.info(
            "map: galactic layout done",
            extra={
                "mode": report.mode, "placed": inserted, "rev": rev,
                "regions": report.regions, "arm_mass": report.arm_mass,
                "arm_balance_pct": report.arm_balance_pct,
                "edge_median_len": report.edge_median_len,
                "seconds": round(elapsed, 3),
            },
        )
        return {
            "ok": True, "force": force, "mode": report.mode, "placed": inserted,
            "rev": rev, "regions": report.regions, "arm_mass": report.arm_mass,
            "arm_balance_pct": report.arm_balance_pct,
            "edge_median_len": report.edge_median_len,
            "version": post_meta["version"], "seconds": round(elapsed, 3),
        }

    # ── Dirty-bump: reconciler / refresh_clusters ──────────────────

    async def bump_dirty(self) -> None:
        """После изменения рёбер/кластеров: dirty-флаг + снос кешей карты.

        Сами события version-хэш не всегда видит (кластеры не трогают
        memories.updated_at, переписи weight рёбер — вообще ничей маркер),
        поэтому снос map:snap:* — единственный честный инвалидатор: первый
        /full пересоберёт снапшот с новыми данными под тем же ключом.
        """
        redis = await self._redis()
        await redis.set(DIRTY_KEY, "1")
        await redis.delete(META_KEY)
        async for key in redis.scan_iter(match=f"{SNAP_KEY_PREFIX}*"):
            await redis.delete(key)


def _meta_from_json(cached: bytes | str) -> dict[str, Any]:
    raw = cached.decode() if isinstance(cached, bytes) else cached
    try:
        import orjson

        return orjson.loads(raw)
    except ImportError:
        import json

        return json.loads(raw)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
