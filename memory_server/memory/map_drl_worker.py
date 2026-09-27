"""Дочерний процесс изоляции DrL (PLAN_FULL_MAP_3D M2, фикс F1 приёмки).

igraph 1.0 на отдельных платформах/графах валит процесс СЕГФОЛТОМ прямо
в C-core layout_drl(dim=3) — try/except бесполезен, умирает воркер Celery.
Единственная защита — адресная изоляция: расчёт в отдельном интерпретаторе
(запуск `python -m memory_server.memory.map_drl_worker` из map_layout);
смерть/зависание потомка для родителя — просто отсутствие ответа на
stdout → прежний layout (fallback).

Почему subprocess, а не multiprocessing.Process: задача исполняется в
daemonic prefork-чайлде celery, где Process.start() запрещён
(AssertionError «daemonic processes are not allowed to have children»,
прод-инцидент 27.09); subprocess.Popen daemon-флаг не наследует. Изоляция
при этом сильнее: крах C-core умирает в потомке, адресные пространства
не пересекаются вовсе.

Транспорт — numpy-бинарник, НЕ JSON (Мастер 27.09: полный точный DrL на
всех узлах; JSON-списки пар на 1.5M рёбер ели сотни МБ и в писателе, и в
читателе). Граф приходит файлом (int64-рёбра, float64-веса/сиды — формат
map_layout.GraphFileWriter), igraph строится напрямую из 2D edge-array —
ни одного промежуточного питоновского списка (Graph.DictList/TupleList
и tolist() — запрещены). Координаты уходят обратно бинарным файлом.

Модуль НАМЕРЕННО лёгкий: только stdlib + numpy + igraph. Никаких
импортов memory_server (логгер, config) — потомок не должен тянуть мир
воркера, его задача умереть тихо и недорого.

Контракт: stdin — JSON {"graph": путь, "result": путь, "rng_seed": int};
stdout — {"status": "ok"|"no_igraph"|"error", "value": node_count|строка}
(статусный JSON прежний; координаты — result-файл float64 (n,3), пишется
ДО ответа). Пустой stdout/ненулевой exitcode для родителя неотличимы от
смерти — обрабатываются одинаково.
"""

from __future__ import annotations

import os
import random
import sys

import numpy as np

# Радиус нормировки seed для DrL: density grid 3D не терпит крупных
# стартовых разбросов («Exceeded density grid»); относительная структура
# seed сохраняется, абсолютный масштаб снимает нормировка в родителе.
SEED_RADIUS = 10.0

# Трейлер графа: 4×int64 — node_count, edge_count, has_weights, has_seed
_TRAILER_BYTES = 32


def read_graph(path: str) -> tuple[int, np.ndarray, np.ndarray | None, np.ndarray | None]:
    """Бинарный формат GraphFileWriter → (node_count, edges, weights, seed).

    Размер файла сверяется с трейлером: обрезанный/битый транспорт —
    ValueError (воркер ответит "error", родитель уйдёт в fallback), а не
    мусорная раскладка на живой карте.
    """
    size = os.path.getsize(path)
    if size < _TRAILER_BYTES:
        raise ValueError(f"graph file too small: {size}B")
    node_count, edge_count, has_weights, has_seed = (
        int(v) for v in np.fromfile(path, dtype="<i8", count=4, offset=size - _TRAILER_BYTES)
    )
    expected = (
        16 * edge_count
        + (8 * edge_count if has_weights else 0)
        + (24 * node_count if has_seed else 0)
        + _TRAILER_BYTES
    )
    if size != expected:
        raise ValueError(f"graph file size {size} != trailer expectation {expected}")
    edges = np.fromfile(path, dtype="<i8", count=2 * edge_count).reshape(-1, 2)
    weights = seed = None
    offset = 16 * edge_count
    if has_weights:
        weights = np.fromfile(path, dtype="<f8", count=edge_count, offset=offset)
        offset += 8 * edge_count
    if has_seed:
        seed = np.fromfile(path, dtype="<f8", count=3 * node_count, offset=offset).reshape(-1, 3)
    return node_count, edges, weights, seed


def run(graph_path: str, result_path: str, rng_seed: int) -> tuple[str, object]:
    """Посчитать DrL dim=3.

    Контракт ответа: ("ok", node_count) | ("no_igraph", None) |
    ("error", str). Координаты — в result_path, не в ответе. Юнит-тестируется
    напрямую (read_graph/run отдельно).
    """
    try:
        random.seed(rng_seed)
        import igraph

        # Воспроизводимость прогонов: DrL стартует со случайного
        # состояния, дефолтный RNG платформозависим (фикс F1.2)
        igraph.set_random_number_generator(random.Random(rng_seed))

        node_count, edges, weights, seed = read_graph(graph_path)
        graph = igraph.Graph(n=node_count, edges=edges, directed=False)
        # C-core скопировал рёбра — транспортный массив освобождаем ДО
        # расчёта: density grid DrL на сотнях тысяч узлов сам съест
        # сотни МБ (прод-OOM 27.09), каждый МБ на счету
        del edges
        kwargs: dict = {"dim": 3}
        if weights is not None:
            # |w|: DrL не терпит неположительных весов
            kwargs["weights"] = np.abs(weights).clip(min=1e-6)
            del weights
        if seed is not None and len(seed):
            scale = float(np.abs(seed).max())
            if scale > SEED_RADIUS:
                seed = seed * (SEED_RADIUS / scale)
            kwargs["seed"] = seed
        layout = np.asarray(graph.layout_drl(**kwargs), dtype=np.float64)
        layout.tofile(result_path)
        return "ok", node_count
    except ImportError:
        return "no_igraph", None
    except BaseException as exc:  # потомку нечем логировать — только доложить
        return "error", f"{type(exc).__name__}: {exc}"


def main() -> None:
    """Точка входа `python -m`: stdin JSON → run() → stdout JSON."""
    try:
        import orjson

        payload = orjson.loads(sys.stdin.buffer.read())
    except ImportError:
        import json

        payload = json.loads(sys.stdin.buffer.read())
    status, value = run(payload["graph"], payload["result"], payload["rng_seed"])
    try:
        import orjson

        sys.stdout.buffer.write(orjson.dumps({"status": status, "value": value}))
    except ImportError:
        import json

        sys.stdout.buffer.write(
            json.dumps({"status": status, "value": value}, separators=(",", ":")).encode()
        )


if __name__ == "__main__":
    main()
