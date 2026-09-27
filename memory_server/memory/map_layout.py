"""Раскладка Полной карты 3D (PLAN_FULL_MAP_3D M2, §2.2) — чистая математика.

stdlib + numpy: модуль не знает про PG/Redis и напрямую юнит-тестируется.
Единственная инфраструктурная деталь — запуск DrL-воркера потомком
(subprocess, daemon-безопасность celery).
Пайплайн таски layout_map:

    DrL(dim=3, weights=|w|, seed=старые координаты)   — igraph, физика,
      в отдельном интерпретаторе-потомке (сегфолт C-core не роняет
      воркер, фикс F1) → normalize_bbox(±half)        — фиксированный масштаб
      → relax_min_distance(d_min)                      — разлёт близких пар
      → clip(±half)                                    — жёсткая граница куба

Релаксация ПОСЛЕ нормировки: d_min из конфига имеет смысл абсолютных
единиц куба (±1000); релаксировать в сыром масштабе DrL бессмысленно —
его разброс не нормирован. Сдвиги релаксации малы относительно куба,
структуру DrL не разрушают.

Fallback (DrL упал / igraph недоступен / прогонов ещё не было):
сферическая полярная раскладка кластеров — без физики, детерминированная
(сфера Фибоначчи по индексам отсортированных кластеров).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from memory_server.logger import get_logger

logger = get_logger(__name__)


def _json_dumps(payload: object) -> bytes:
    try:
        import orjson

        return orjson.dumps(payload)
    except ImportError:  # orjson опционален локально — прод-образ ставит всегда
        import json

        return json.dumps(payload, separators=(",", ":")).encode()


def _json_loads(raw: bytes):
    try:
        import orjson

        return orjson.loads(raw)
    except ImportError:
        import json

        return json.loads(raw)

# Смещения 26 соседних ячеек + своя: обход spatial-hash при релаксации
_CELL_OFFSETS = tuple(
    (dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
)

# Детерминированный RNG: seed-джиттер новых узлов и igraph-генератор
# (ночные прогоны воспроизводимы — фикс F1.2)
_RNG_SEED = 20260921

# Статусы DrL-прогона (контракт drl_layout → вызывающий)
DRL_OK = "drl"
DRL_SPHERE = "sphere"  # плановый fallback: нет рёбер / igraph не установлен
DRL_FAILED = "drl_failed_fallback"  # subprocess умер/завис/ошибся — щит сработал


# Корень пакета для PYTHONPATH потомка: cwd celery-чайлда не обязан
# содержать memory_server (запуск -m ищет пакет по sys.path)
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])


def _run_isolated(payload: dict, timeout: float) -> dict | None:
    """Выполнить DrL-воркер отдельным интерпретатором с таймаутом.

    multiprocessing.Process из daemonic prefork-чайлда celery запрещён
    (AssertionError «daemonic processes are not allowed to have children»,
    прод-инцидент 27.09) — subprocess.Popen daemon-флаг не наследует,
    работает из любого процесса. Обмен — JSON по stdin/stdout (payload
    ~единицы МБ на 130k рёбер), pickle-канал multiprocessing не нужен.

    Смерть потомка (segfault C-core, OOM-kill, exit != 0), зависание и
    битый ответ выглядят одинаково: None. Родитель всегда жив, потомок
    всегда прибит (subprocess.run при таймауте делает kill сам) —
    это и есть щит F1.3.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{_PACKAGE_ROOT}{os.pathsep}{existing}" if existing else _PACKAGE_ROOT
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "memory_server.memory.map_drl_worker"],
            input=_json_dumps(payload),
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0:
        logger.warning(
            "map_layout: DrL worker process died",
            extra={
                "exit": completed.returncode,
                "stderr": completed.stderr.decode(errors="replace")[:200],
            },
        )
        return None
    try:
        answer = _json_loads(completed.stdout)
    except ValueError:
        return None  # потомок умер посреди записи — пустой/битый stdout
    return answer if isinstance(answer, dict) else None


def drl_layout(
    node_count: int,
    edge_indices: np.ndarray,
    weights: np.ndarray | None = None,
    seed: np.ndarray | None = None,
    timeout: float = 120.0,
) -> tuple[np.ndarray | None, str]:
    """igraph DrL в 3D внутри изолированного интерпретатора-потомка.

    Возвращает (координаты | None, статус): DRL_OK при успехе; иначе None и
    DRL_SPHERE (плановый fallback: пустой граф / igraph не установлен) или
    DRL_FAILED (потомок умер, завис или ошибся — считать нельзя, вызывающий
    держит прежний layout).
    """
    if len(edge_indices) == 0:
        # Пустой граф роняет DrL 3D (density grid) — физике нечего считать
        return None, DRL_SPHERE
    if importlib.util.find_spec("igraph") is None:
        return None, DRL_SPHERE

    edge_pairs = [(int(a), int(b)) for a, b in edge_indices]
    weight_list = (
        [float(w) for w in weights] if weights is not None and len(weights) else None
    )
    seed_list = (
        np.asarray(seed, dtype=np.float64).tolist() if seed is not None else None
    )
    answer = _run_isolated(
        {
            "node_count": node_count,
            "edges": edge_pairs,
            "weights": weight_list,
            "seed": seed_list,
            "rng_seed": _RNG_SEED,
        },
        timeout,
    )
    if answer is not None:
        status, value = answer.get("status"), answer.get("value")
        if status == "ok":
            return np.asarray(value, dtype=np.float64), DRL_OK
        if status == "no_igraph":
            return None, DRL_SPHERE
        logger.warning("map_layout: DrL subprocess reported error", extra={
            "error": str(value)[:200], "nodes": node_count,
        })
    else:
        logger.warning("map_layout: DrL subprocess died or timed out", extra={
            "nodes": node_count, "timeout": timeout,
        })
    return None, DRL_FAILED


def normalize_bbox(coords: np.ndarray, half: float) -> np.ndarray:
    """Центрирование (медиана устойчива к выбросам DrL) + масштаб max|x|,|y|,|z| → half."""
    shifted = coords - np.median(coords, axis=0)
    scale = float(np.abs(shifted).max()) if len(shifted) else 0.0
    if scale < 1e-9:
        # Вырожденный граф (все в точке) — оставляем у центра, не делим на 0
        return shifted
    return shifted * (half / scale)


def relax_min_distance(
    coords: np.ndarray, min_dist: float, iterations: int
) -> np.ndarray:
    """Раздвигание пар ближе min_dist, несколько итераций (решение Мастера 4).

    Jacobi-стиль: смещения итерации считаются от одного среза координат и
    применяются пачкой — Gauss-Seidel («сдвигай сразу») осциллирует на
    плотных гроздьях. Кандидатные пары — spatial hash по ячейкам размера
    min_dist: пара ближе d_min обязана лежать в соседних 27 ячейках, полный
    перебор O(n²) не нужен. Чистый Python поверх numpy: 15k узлов × 8
    итераций × 27 lookup'ов ≈ секунды в ночной таске — векторизация тут
    не окупается сложностью.
    """
    coords = np.array(coords, dtype=np.float64, copy=True)
    n = len(coords)
    for _ in range(iterations):
        delta = np.zeros_like(coords)
        keys = np.floor(coords / min_dist).astype(np.int64)
        buckets: dict[tuple[int, int, int], list[int]] = {}
        for i in range(n):
            buckets.setdefault((int(keys[i, 0]), int(keys[i, 1]), int(keys[i, 2])), []).append(i)
        max_push = 0.0
        for i in range(n):
            kx, ky, kz = int(keys[i, 0]), int(keys[i, 1]), int(keys[i, 2])
            for dx, dy, dz in _CELL_OFFSETS:
                for j in buckets.get((kx + dx, ky + dy, kz + dz), ()):
                    if j <= i:
                        continue  # каждая пара один раз
                    diff = coords[i] - coords[j]
                    dist = float(np.sqrt(diff @ diff))
                    if dist >= min_dist:
                        continue
                    if dist < 1e-9:
                        # Совпадающие точки (вырожденный кластер): разводим
                        # детерминированным направлением от индексов пары
                        diff = np.array([
                            np.sin(1.0 + i * 12.9898 + j),
                            np.cos(1.0 + i * 4.1414 + j * 7.7),
                            np.sin(1.0 + i * 3.7 - j * 2.3),
                        ])
                        dist = float(np.sqrt(diff @ diff)) or 1.0
                    push = (min_dist - min(dist, min_dist)) * 0.5
                    step = diff / dist * push
                    delta[i] += step
                    delta[j] -= step
                    max_push = max(max_push, push)
        if max_push == 0.0:
            break  # стабилизировалось раньше бюджета итераций
        coords += delta
    return coords


def _fibonacci_sphere(count: int, radius: float, offset: int = 0) -> np.ndarray:
    """Равномерные точки на сфере: золотой угол, y — равномерно по высоте."""
    if count <= 0:
        return np.zeros((0, 3))
    idx = np.arange(count, dtype=np.float64)
    y = 1.0 - 2.0 * (idx + 0.5) / count
    ring = np.sqrt(np.maximum(0.0, 1.0 - y * y))
    theta = np.pi * (3.0 - np.sqrt(5.0)) * (idx + offset)
    return np.column_stack((ring * np.cos(theta), y, ring * np.sin(theta))) * radius


def spherical_layout(cluster_of: np.ndarray, half: float) -> np.ndarray:
    """Кластеры → центры на внутренней сфере, члены — орбитой вокруг центра,
    одиночки — внешняя сфера. Без физики, детерминированно от состава
    кластеров (индексы отсортированных id): один и тот же корпус → те же
    координаты. Она же fallback M1 для узлов без координат в map_layout."""
    cluster_of = np.asarray(cluster_of, dtype=np.int64)
    coords = np.zeros((len(cluster_of), 3))
    uniq = np.unique(cluster_of[cluster_of >= 0])
    centers = _fibonacci_sphere(len(uniq), half * 0.72)
    member_radius = half * 0.1
    for pos, cluster in enumerate(uniq):
        members = np.where(cluster_of == cluster)[0]
        # offset от индекса кластера: орбиты соседних кластеров не совпадают
        local = _fibonacci_sphere(len(members), member_radius, offset=int(cluster))
        coords[members] = centers[pos] + local
    singles = np.where(cluster_of < 0)[0]
    if singles.size:
        coords[singles] = _fibonacci_sphere(len(singles), half * 0.95)
    return coords


def seed_positions(
    node_count: int,
    edge_indices: np.ndarray,
    old_coords: np.ndarray,
    half: float,
) -> np.ndarray | None:
    """Стартовые позиции DrL: старые узлы — прежние координаты (карта дышит),
    новые — среднее координат соседей со старыми (вливается в свою область).

    None, если размещённых узлов нет вовсе (первый прогон): сеять нечего,
    DrL стартует собственным RNG — ИДЕАЛЬНАЯ сфера сидов провоцирует
    density-grid краши C-core (фикс F1.1 приёмки).

    Новые без размещённых соседей — детерминированный джиттер-облако
    (случайные направления, радиусы 0.15..0.6·half), не идеальная сфера:
    ровные сферические ряды — тот же провокатор density grid.
    """
    seed = np.array(old_coords, dtype=np.float64, copy=True)
    if node_count == 0:
        return seed
    placed = ~np.isnan(seed[:, 0])
    if not placed.any():
        return None
    if edge_indices is not None and len(edge_indices):
        sums = np.zeros_like(seed)
        neighbor_count = np.zeros(node_count)
        edges = np.asarray(edge_indices, dtype=np.int64)
        # np.add.at аккумулирует по дублирующимся индексам (seed[a] += b не работает)
        forward = ~placed[edges[:, 0]] & placed[edges[:, 1]]
        np.add.at(sums, edges[forward, 0], seed[edges[forward, 1]])
        np.add.at(neighbor_count, edges[forward, 0], 1.0)
        backward = placed[edges[:, 0]] & ~placed[edges[:, 1]]
        np.add.at(sums, edges[backward, 1], seed[edges[backward, 0]])
        np.add.at(neighbor_count, edges[backward, 1], 1.0)
        seeded = neighbor_count > 0
        seed[seeded] = sums[seeded] / neighbor_count[seeded, None]
    unseeded = np.where(np.isnan(seed[:, 0]))[0]
    if unseeded.size:
        rng = np.random.default_rng(_RNG_SEED)
        directions = rng.normal(size=(unseeded.size, 3))
        norms = np.linalg.norm(directions, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        radii = half * rng.uniform(0.15, 0.6, size=(unseeded.size, 1))
        seed[unseeded] = directions / norms * radii
    return seed
