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
import tempfile
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


# ── Бинарный numpy-транспорт DrL (Мастер 27.09: полный точный прогон) ──
# Формат графа (little-endian, C-order, секции подряд):
#   [edges (m,2) int64][weights m float64]?[seed (n,3) float64]?[trailer 4×int64]
# Трейлер В КОНЦЕ: родитель пишет потоково (батчи курсора БД) и не знает
# m заранее; воркер (map_drl_worker.read_graph) читает секции от начала
# по счётчикам трейлера и сверяет общий размер — битый файл = "error".
_TRAILER_COUNT = 4  # node_count, edge_count, has_weights, has_seed


class GraphFileWriter:
    """Потоковая запись бинарного графа для DrL-воркера.

    Рёбра доходят до igraph int64/float64-массивами напрямую из БД —
    без JSON и питоновских списков пар (сотни МБ на 1.5M рёбер убили
    полный DrL в 512M-контейнере, прод-замер 27.09). Память писателя
    O(батч), не O(рёбра).

    Формат секционный (edges → weights → seed → трейлер), батчи пишутся
    строго по фазам: ВСЕ батчи рёбер, затем ВСЕ батчи весов — перемежовка
    E1 W1 E2 W2 сделала бы файл нечитаемым, поэтому переход фаз защищён
    исключением.
    """

    _PHASE_EDGES, _PHASE_WEIGHTS, _PHASE_SEED, _PHASE_DONE = 0, 1, 2, 3

    def __init__(self, directory: str) -> None:
        fd, self.path = tempfile.mkstemp(prefix="drl-graph-", suffix=".bin", dir=directory)
        self._file = os.fdopen(fd, "wb")
        self._phase = self._PHASE_EDGES
        self.edge_count = 0
        self._weight_count = 0
        self._has_weights = False
        self._has_seed = False

    def write_edges(self, pairs: np.ndarray) -> None:
        """Батч рёбер (k,2); все батчи рёбер — до первого батча весов."""
        if self._phase != self._PHASE_EDGES:
            raise RuntimeError("all edge batches must precede weight batches")
        if len(pairs):
            self._file.write(np.ascontiguousarray(pairs, dtype="<i8").tobytes())
            self.edge_count += len(pairs)

    def write_weights(self, values: np.ndarray) -> None:
        """Батч весов; суммарно — ровно столько, сколько рёбер."""
        if self._phase >= self._PHASE_SEED:
            raise RuntimeError("weight batches must precede seed")
        if len(values):
            self._file.write(np.ascontiguousarray(values, dtype="<f8").tobytes())
            self._weight_count += len(values)
            self._has_weights = True
        self._phase = self._PHASE_WEIGHTS

    def write_seed(self, seed: np.ndarray) -> None:
        """Стартовые позиции (n,3) float64 — целиком, секция последняя."""
        if self._phase == self._PHASE_DONE:
            raise RuntimeError("graph file already finished")
        if seed is not None and len(seed):
            self._file.write(np.ascontiguousarray(seed, dtype="<f8").tobytes())
            self._has_seed = True
        self._phase = self._PHASE_SEED

    def finish(self, node_count: int) -> int:
        """Дописать трейлер, закрыть; возвращает записанное число рёбер."""
        if self._has_weights and self._weight_count != self.edge_count:
            raise RuntimeError(
                f"weights/edges count mismatch: {self._weight_count} vs {self.edge_count}"
            )
        trailer = np.array(
            [node_count, self.edge_count, int(self._has_weights), int(self._has_seed)],
            dtype="<i8",
        )
        self._file.write(trailer.tobytes())
        self._file.close()
        self._phase = self._PHASE_DONE
        return self.edge_count


def _run_isolated(input_bytes: bytes, timeout: float) -> dict | None:
    """Выполнить DrL-воркер отдельным интерпретатором с таймаутом.

    multiprocessing.Process из daemonic prefork-чайлда celery запрещён
    (AssertionError «daemonic processes are not allowed to have children»,
    прод-инцидент 27.09) — subprocess.Popen daemon-флаг не наследует,
    работает из любого процесса. stdin — крошечный JSON с ПУТЯМИ
    numpy-файлов (граф/результат); тяжёлые массивы через pipe не идут.

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
            input=input_bytes,
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


def _read_coords(result_path: str, node_count: int) -> np.ndarray | None:
    """Result-файл → (n,3) float64; битый/обрезанный → None.

    Инвариант полноты: DrL-выход обязан содержать ровно node_count узлов —
    недостающие строки означали бы молчаливую потерю узлов на карте.
    """
    try:
        flat = np.fromfile(result_path, dtype="<f8")
    except OSError:
        return None
    return flat.reshape(-1, 3) if flat.size == node_count * 3 else None


def _run_drl_file(graph_path: str, node_count: int, timeout: float) -> tuple[np.ndarray | None, str]:
    """Готовый граф-файл → изолированный DrL-воркер → координаты.

    Общий путь массивного (drl_layout) и потокового (MapService.rebuild_layout)
    транспорта: файл один, контракт статусов прежний JSON.
    """
    result_path = graph_path + ".result"
    answer = _run_isolated(
        _json_dumps({"graph": graph_path, "result": result_path, "rng_seed": _RNG_SEED}),
        timeout,
    )
    if answer is not None:
        status, value = answer.get("status"), answer.get("value")
        if status == "ok":
            coords = _read_coords(result_path, node_count)
            if coords is not None:
                return coords, DRL_OK
            # файл недописан (потомок умер после ответа?) — честнее failed
            logger.warning(
                "map_layout: DrL result file missing or truncated",
                extra={"nodes": node_count, "declared": value},
            )
            return None, DRL_FAILED
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

    with tempfile.TemporaryDirectory(prefix="drl-") as tmp:
        writer = GraphFileWriter(tmp)
        writer.write_edges(np.asarray(edge_indices))
        if weights is not None and len(weights):
            writer.write_weights(np.asarray(weights, dtype=np.float64))
        if seed is not None:
            writer.write_seed(np.asarray(seed, dtype=np.float64))
        writer.finish(node_count)
        return _run_drl_file(writer.path, node_count, timeout)


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
