"""Профилирование памяти полного точного DrL (Мастер 27.09).

Синтетика той же природы, что корпус знаний: Barabasi–Albert (хабы,
тяжёлый хвост степеней) + равномерный фон — средняя «рёбер на узел» как
на проде. Граф идёт в DrL-воркер РЕАЛЬНЫМ прод-путём: бинарный
numpy-файл (GraphFileWriter) → python -m map_drl_worker → result-файл.

Замеры:
    родитель     — RSS процесса-генератора (аналог питон-части celery-задачи)
    до DrL       — отдельный прогон воркера: read_graph + igraph.Graph, БЕЗ
                   layout_drl (импорты + транспорт + C-copy рёбер)
    пик DrL      — полный прогон с psutil-поллингом RSS потомка (50 мс)

Запуск (из корня репо):
    python scripts/bench_drl_memory.py [nodes] [edges] [--csv PATH]

По умолчанию 200000 узлов / 1460000 рёбер (7.3 рёбер на узел — корпус
17.5k/104k, прод 27.09). CSV — timeline поллинга для графика.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import psutil

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# Runner CI не имеет приватного argenta-logging (только прод-образ);
# bench'у из map_layout нужен один GraphFileWriter — logger не читаем,
# подставляем тихую заглушку ДО первого import memory_server.*
import types  # noqa: E402

_logging_stub = types.ModuleType("argenta_logging")
_logging_stub.get_logger = lambda name: types.SimpleNamespace(
    warning=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None
)
_logging_stub.measure_duration = lambda *args, **kwargs: (lambda func: func)
_logging_stub.request_id_var = None
sys.modules.setdefault("argenta_logging", _logging_stub)

from memory_server.memory import map_layout  # noqa: E402
from memory_server.memory.map_drl_worker import SEED_RADIUS  # noqa: E402

_WORKER = "memory_server.memory.map_drl_worker"


def synth_edges(nodes: int, edges_total: int, rng: np.random.Generator) -> np.ndarray:
    """BA(m=7) + равномерный фон до edges_total: хабы как в графе знаний."""
    import igraph

    ba_m = 7
    g = igraph.Graph.Barabasi(nodes, m=ba_m, directed=False)
    ba = np.array(g.get_edgelist(), dtype=np.int64)
    del g
    extra = edges_total - len(ba)
    if extra > 0:
        src = rng.integers(0, nodes, size=extra)
        dst = rng.integers(0, nodes, size=extra)
        ba = np.concatenate((ba, np.column_stack((src, dst))))
    return ba[:edges_total]


def spawn_worker(payload: dict, poll: bool) -> tuple[dict | None, list[tuple[float, int]], float]:
    """Запуск DrL-воркера как в проде; опциональный поллинг RSS.

    Возвращает (ответ stdout-JSON | None, timeline (t, rss_bytes), секунд).
    """
    import json

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_ROOT), env.get("PYTHONPATH")) if p
    )
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", _WORKER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    proc.stdin.write(json.dumps(payload).encode())
    proc.stdin.close()
    timeline: list[tuple[float, int]] = []
    if poll:
        watcher = psutil.Process(proc.pid)
        while proc.poll() is None:
            try:
                timeline.append((time.perf_counter() - started, watcher.memory_info().rss))
            except psutil.Error:
                break
            time.sleep(0.05)
    # stdin уже закрыт вручную — communicate() падает «flush of closed file»;
    # процесс мёртв (poll-цикл вышел) → в pipes только EOF, читаем напрямую
    out = proc.stdout.read() if proc.stdout else b""
    err = proc.stderr.read() if proc.stderr else b""
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        print(f"  worker died rc={proc.returncode}: {err.decode(errors='replace')[:300]}")
        return None, timeline, elapsed
    return json.loads(out), timeline, elapsed


def main() -> None:
    nodes = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    edges_total = int(sys.argv[2]) if len(sys.argv) > 2 else 1_460_000
    csv_path = None
    if "--csv" in sys.argv:
        csv_path = Path(sys.argv[sys.argv.index("--csv") + 1])

    rng = np.random.default_rng(20260927)
    t0 = time.perf_counter()
    edges = synth_edges(nodes, edges_total, rng)
    weights = np.abs(rng.normal(0.7, 0.2, size=len(edges))).clip(min=1e-3)
    # Сид ночного прогона: старая карта в кубе bbox (нормировку к SEED_RADIUS
    # делает воркер — как в проде)
    seed = rng.uniform(-1000.0, 1000.0, size=(nodes, 3))
    gen_seconds = time.perf_counter() - t0
    parent_rss = psutil.Process(os.getpid()).memory_info().rss

    with tempfile.TemporaryDirectory(prefix="drl-bench-") as tmp:
        writer = map_layout.GraphFileWriter(tmp)
        writer.write_edges(edges)
        writer.write_weights(weights)
        writer.write_seed(seed)
        written = writer.finish(nodes)
        graph_mb = os.path.getsize(writer.path) / 2**20
        payload = {
            "graph": writer.path, "result": writer.path + ".result",
            "rng_seed": 20260927,
        }

        # Прогон 1: «воркер до DrL» — транспорт + построение C-графа, без расчёта
        before = f"""
import sys, json
import numpy as np
sys.path.insert(0, {str(_ROOT)!r})
from memory_server.memory import map_drl_worker
import igraph
n, edges, weights, seed = map_drl_worker.read_graph({writer.path!r})
g = igraph.Graph(n=n, edges=edges, directed=False)
del edges
import psutil, os
print(json.dumps({{"rss": psutil.Process(os.getpid()).memory_info().rss}}))
"""
        started = time.perf_counter()
        probe = subprocess.run(
            [sys.executable, "-c", before], capture_output=True, env={**os.environ}
        )
        build_seconds = time.perf_counter() - started
        pre_drl_rss = None
        if probe.returncode == 0:
            import json as json_mod

            pre_drl_rss = json_mod.loads(probe.stdout)["rss"]

        # Прогон 2: полный DrL с поллингом пика
        answer, timeline, full_seconds = spawn_worker(payload, poll=True)
        peak_rss = max((rss for _, rss in timeline), default=0)
        if csv_path and timeline:
            csv_path.write_text(
                "t_seconds,rss_bytes\n"
                + "\n".join(f"{t:.3f},{rss}" for t, rss in timeline),
                encoding="utf-8",
            )

    status = (answer or {}).get("status")
    result_rows = (answer or {}).get("value") if status == "ok" else None

    def mb(x: int | None) -> str:
        return "n/a" if x is None else f"{x / 2**20:.1f}"

    print(f"nodes={nodes} edges={written} (target {edges_total}, deg={edges_total / nodes:.1f})")
    print(f"graph file: {graph_mb:.1f} MB")
    print(f"synth gen: {gen_seconds:.1f}s; parent RSS: {mb(parent_rss)} MB")
    print(f"worker before DrL (imports+transport+Graph): {mb(pre_drl_rss)} MB in {build_seconds:.1f}s")
    print(f"DRL status: {status}; full worker: {full_seconds:.1f}s; PEAK RSS: {mb(peak_rss)} MB")
    print(f"result nodes: {result_rows}; seed radius norm: {SEED_RADIUS}")


if __name__ == "__main__":
    main()
