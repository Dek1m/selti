"""Дочерний процесс изоляции DrL (PLAN_FULL_MAP_3D M2, фикс F1 приёмки).

igraph 1.0.0 на отдельных платформах/графах валит процесс СЕГФОЛТОМ прямо
в C-core layout_drl(dim=3) — try/except бесполезен, умирает воркер Celery.
Единственная защита — адресная изоляция: расчёт в spawn-субпроцессе с
таймаутом; смерть/зависание ребёнка для родителя — просто отсутствие
ответа в pipe → сферический fallback.

Модуль НАМЕРЕННО лёгкий: только stdlib + numpy + igraph. Никаких
импортов memory_server (логгер, config) — spawn-ребёнок не должен
тянуть мир воркера, его задача умереть тихо и недорого.
"""

from __future__ import annotations

import random

import numpy as np

# Радиус нормировки seed для DrL: density grid 3D не терпит крупных
# стартовых разбросов («Exceeded density grid»); относительная структура
# seed сохраняется, абсолютный масштаб снимает нормировка в родителе.
SEED_RADIUS = 10.0


def run(
    conn,
    node_count: int,
    edge_pairs: list,
    weights: list | None,
    seed: list | None,
    rng_seed: int,
) -> None:
    """Посчитать DrL dim=3 и отправить результат в pipe.

    Контракт ответа: ("ok", np.ndarray) | ("no_igraph", None) |
    ("error", str). Письмо в pipe БЕЗ unsafe-pickle сюрпризов; соединение
    закрывается всегда — родитель ловит EOF как отсутствие ответа.
    """
    try:
        random.seed(rng_seed)
        import igraph

        # Воспроизводимость ночных прогонов: DrL стартует со случайного
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
        conn.send(("ok", layout))
    except ImportError:
        conn.send(("no_igraph", None))
    except BaseException as exc:  # ребёнку нечем логировать — только доложить
        try:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass  # pipe уже мёртв — родитель увидит EOF
    finally:
        conn.close()
