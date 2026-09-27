"""Дочерний процесс изоляции DrL (PLAN_FULL_MAP_3D M2, фикс F1 приёмки).

igraph 1.0.0 на отдельных платформах/графах валит процесс СЕГФОЛТОМ прямо
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

Модуль НАМЕРЕННО лёгкий: только stdlib + numpy + igraph. Никаких
импортов memory_server (логгер, config) — потомок не должен тянуть мир
воркера, его задача умереть тихо и недорого.

Контракт: stdin — orjson/json payload {node_count, edges, weights, seed,
rng_seed}; stdout — {"status": "ok"|"no_igraph"|"error", "value": ...}
(ok → список [x, y, z] по узлам). Пустой stdout/ненулевой exitcode для
родителя неотличимы от смерти — обрабатываются одинаково.
"""

from __future__ import annotations

import random
import sys

import numpy as np

# Радиус нормировки seed для DrL: density grid 3D не терпит крупных
# стартовых разбросов («Exceeded density grid»); относительная структура
# seed сохраняется, абсолютный масштаб снимает нормировка в родителе.
SEED_RADIUS = 10.0


def run(
    node_count: int,
    edge_pairs: list,
    weights: list | None,
    seed: list | None,
    rng_seed: int,
) -> tuple[str, object]:
    """Посчитать DrL dim=3.

    Контракт ответа: ("ok", np.ndarray) | ("no_igraph", None) |
    ("error", str). Сериализацию в JSON делает main() — здесь чистый
    расчёт, юнит-тестируется напрямую.
    """
    try:
        random.seed(rng_seed)
        import igraph

        # Воспроизводимость прогонов: DrL стартует со случайного
        # состояния, дефолтный RNG платформозависим (фикс F1.2)
        igraph.set_random_number_generator(random.Random(rng_seed))

        graph = igraph.Graph(n=node_count, edges=edge_pairs, directed=False)
        kwargs: dict = {"dim": 3}
        if weights:
            # |w|: DrL не терпит неположительных весов
            kwargs["weights"] = [max(abs(float(w)), 1e-6) for w in weights]
        if seed is not None:
            arr = np.asarray(seed, dtype=np.float64)
            scale = float(np.abs(arr).max())
            if scale > SEED_RADIUS:
                arr = arr * (SEED_RADIUS / scale)
            kwargs["seed"] = arr.tolist()
        layout = np.asarray(graph.layout_drl(**kwargs), dtype=np.float64)
        return "ok", layout
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

    status, value = run(
        payload["node_count"],
        payload["edges"],
        payload.get("weights"),
        payload.get("seed"),
        payload["rng_seed"],
    )
    if isinstance(value, np.ndarray):
        value = value.tolist()

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
