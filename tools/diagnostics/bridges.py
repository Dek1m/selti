"""bridges.py — T0.3 «Жизнь графа знаний» (V3.5).

READ ONLY-диагностика межкластерных рёбер-мостов (src.cluster_id <>
tgt.cluster_id, кластеры миграции 022): JSON-список мостиков —
иммунитет-список для decay-воркера (мостики сшивают тематические
кластеры — их затухание рвёт карту знаний).

Опция --betweenness: центровость подграфа top-degree (<= --top-k, по
умолчанию 5000) узлов, python-igraph, невзвешенно/неориентированно
(мост по смыслу симметричен; веса рёбер разнородны по слоям линкера и
взвешивание смешало бы семантики). Мягкий таймаут --timeout-s (60):
SIGALRM-таймауты POSIX-only, а скрипт обязан работать и на проде
(Linux), и в локальной проверке (Windows) — поэтому вычисление в
daemon-потоке с join(timeout): по истечении результат отбрасывается,
процесс завершается, не дожидаясь зависшего потока.

Никаких секретов в коде: подключение — только env (см. README.md).
Прод-окно: после 05:30 UTC. Против прода запускает Рэй.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:  # импорт как пакет: tools.diagnostics.bridges (pytest)
    from .cosine_histogram import (
        DEFAULT_STATEMENT_TIMEOUT_S,
        DiagConfig,
        ENV_PG_DSN,
        open_read_only_connection,
    )
except ImportError:  # запуск файлом с прода: python bridges.py
    from cosine_histogram import (
        DEFAULT_STATEMENT_TIMEOUT_S,
        DiagConfig,
        ENV_PG_DSN,
        open_read_only_connection,
    )

DEFAULT_TOP_K = 5000
DEFAULT_BETWEENNESS_TIMEOUT_S = 60.0

# Межкластерные рёбра. Висячие (target_id IS NULL) исключены: у ребра без
# адресата нет второй стороны кластера. Порядок weight DESC — при ручном
# просмотре сверху самые уверенные мосты.
BRIDGES_SQL = """
    SELECT r.source_id::text, r.target_id::text, r.link_type, r.weight,
           r.metadata->>'source' AS rel_source,
           r.metadata->>'layer'  AS layer,
           src.cluster_id::text AS src_cluster,
           tgt.cluster_id::text AS tgt_cluster,
           src.metadata->>'entity_name' AS src_name,
           tgt.metadata->>'entity_name' AS tgt_name,
           nsrc.uid AS src_namespace,
           ntgt.uid AS tgt_namespace,
           r.created_at
    FROM relations r
    JOIN memories src   ON src.id = r.source_id
    JOIN memories tgt   ON tgt.id = r.target_id
    JOIN namespaces nsrc ON nsrc.id = src.namespace_id
    JOIN namespaces ntgt ON ntgt.id = tgt.namespace_id
    WHERE r.target_id IS NOT NULL
      AND src.cluster_id IS NOT NULL
      AND tgt.cluster_id IS NOT NULL
      AND src.cluster_id <> tgt.cluster_id
    ORDER BY r.weight DESC, r.created_at
"""

# Степени узлов (in + out) + имя: топ-k по степени задаёт подграф
# betweenness. Индексы idx_relations_source/target покрывают оба плеча.
DEGREE_SQL = """
    SELECT d.node_id::text AS node_id,
           sum(d.deg)::int AS degree,
           m.metadata->>'entity_name' AS entity_name
    FROM (
        SELECT source_id AS node_id, count(*) AS deg
        FROM relations
        GROUP BY 1
        UNION ALL
        SELECT target_id, count(*)
        FROM relations
        WHERE target_id IS NOT NULL
        GROUP BY 1
    ) d
    JOIN memories m ON m.id = d.node_id
    GROUP BY 1, 3
"""

# Рёбра подграфа: оба конца в списке топ-узлов (in-memory фильтр 5k×5k
# по 106k строк был бы то же самое, но дороже по сети и памяти клиента).
SUBGRAPH_SQL = """
    SELECT r.source_id::text, r.target_id::text, r.weight
    FROM relations r
    WHERE r.target_id IS NOT NULL
      AND r.source_id = ANY($1::uuid[])
      AND r.target_id = ANY($1::uuid[])
"""


@dataclass(frozen=True)
class BridgesConfig(DiagConfig):
    """DiagConfig + параметры betweenness (наследует env-слой T0.2)."""

    top_k: int = DEFAULT_TOP_K
    betweenness_timeout_s: float = DEFAULT_BETWEENNESS_TIMEOUT_S
    with_betweenness: bool = False


# ── Чистые функции (тестируются pytest без PG) ───────────────────────


def extract_bridges(rows: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """asyncpg-строки → (список мостиков, сводка).

    Сводка: total, разрезы по link_type и парам namespace'ов — их
    достаточно, чтобы оценить масштаб иммунитета не читая весь список.
    """
    bridges: list[dict[str, Any]] = []
    by_link_type: Counter[str] = Counter()
    by_ns_pair: Counter[str] = Counter()
    for row in rows:
        bridge = {
            "source_id": row["source_id"],
            "target_id": row["target_id"],
            "link_type": row["link_type"],
            "weight": round(float(row["weight"]), 6),
            "src_cluster": row["src_cluster"],
            "tgt_cluster": row["tgt_cluster"],
            "src_name": row["src_name"],
            "tgt_name": row["tgt_name"],
            "src_namespace": row["src_namespace"],
            "tgt_namespace": row["tgt_namespace"],
            "rel_source": row["rel_source"],
            "layer": row["layer"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        bridges.append(bridge)
        by_link_type[bridge["link_type"]] += 1
        by_ns_pair[f"{bridge['src_namespace']}→{bridge['tgt_namespace']}"] += 1
    summary = {
        "total_bridges": len(bridges),
        "by_link_type": dict(by_link_type),
        "by_namespace_pair": dict(by_ns_pair),
    }
    return bridges, summary


def build_igraph(nodes: list[str], edges: list[tuple[str, str]]) -> Any:
    """Неориентированный граф подграфа; self-loop'ы и дубли схлопываются
    (igraph делает это сам, фильтр self-loop'ов — явный: петля не мост)."""
    import igraph as ig

    graph = ig.Graph(n=0, directed=False)
    graph.add_vertices(sorted(set(nodes)))
    clean = sorted({(min(a, b), max(a, b)) for a, b in edges if a != b})
    graph.add_edges(clean)
    return graph


def top_betweenness(
    nodes: list[str],
    edges: list[tuple[str, str]],
    top: int = 20,
    timeout_s: float = DEFAULT_BETWEENNESS_TIMEOUT_S,
) -> dict[str, Any]:
    """Топ-узлов по betweenness подграфа; мягкий таймаут потоком.

    Возвращает {"computed": True, "top": [...]} либо
    {"computed": False, "timeout": True} — таймаут это отказ, не ошибка:
    воркер-decay не должен зависеть от успехов центровости.
    """
    import threading

    graph = build_igraph(nodes, edges)
    result: dict[str, Any] = {"computed": False, "timeout": False, "nodes": graph.vcount(), "edges": graph.ecount()}
    scores: list[list[float]] = []

    def _job() -> None:
        scores.append(graph.betweenness(directed=False))

    worker = threading.Thread(target=_job, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if not scores:
        result["timeout"] = True
        return result
    ranking = sorted(zip(graph.vs["name"], scores[0]), key=lambda pair: pair[1], reverse=True)
    result["computed"] = True
    result["top"] = [{"node_id": name, "betweenness": round(score, 3)} for name, score in ranking[:top]]
    return result


# ── Пайплайн ─────────────────────────────────────────────────────────


async def run(
    cfg: BridgesConfig,
    fetch_bridges: Any | None = None,
    fetch_degrees: Any | None = None,
    fetch_subgraph: Any | None = None,
) -> dict[str, Any]:
    """Прогон целиком. fetch_* — точки подмены для тестов."""

    async def _default_conn() -> Any:
        return await open_read_only_connection(cfg.pg_dsn, cfg.statement_timeout_s)

    if fetch_bridges is None:
        async def fetch_bridges() -> list[Any]:  # type: ignore[misc]
            conn = await _default_conn()
            try:
                return await conn.fetch(BRIDGES_SQL)
            finally:
                await conn.close()

    if fetch_degrees is None:
        async def fetch_degrees() -> list[Any]:  # type: ignore[misc]
            conn = await _default_conn()
            try:
                return await conn.fetch(DEGREE_SQL)
            finally:
                await conn.close()

    if fetch_subgraph is None:
        async def fetch_subgraph(top_ids: list[str]) -> list[Any]:  # type: ignore[misc]
            conn = await _default_conn()
            try:
                return await conn.fetch(SUBGRAPH_SQL, top_ids)
            finally:
                await conn.close()

    bridges_rows = await fetch_bridges()
    bridges, summary = extract_bridges(bridges_rows)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": "cross-cluster bridge edges — immunity list for decay",
        **summary,
        "bridges": bridges,
    }

    if cfg.with_betweenness:
        degree_rows = await fetch_degrees()
        # top-k по степени: стабильный порядок (degree DESC, node_id) —
        # воспроизводимость подграфа между прогонами
        ranked = sorted(degree_rows, key=lambda r: (-r["degree"], r["node_id"]))
        top_ids = [r["node_id"] for r in ranked[: cfg.top_k]]
        node_names = {r["node_id"]: r["entity_name"] for r in ranked[: cfg.top_k]}
        sub_rows = await fetch_subgraph(top_ids)
        sub_edges = [(r["source_id"], r["target_id"]) for r in sub_rows]
        betweenness = top_betweenness(top_ids, sub_edges, timeout_s=cfg.betweenness_timeout_s)
        for item in betweenness.get("top", ()):  # имена поверх голых id
            item["entity_name"] = node_names.get(item["node_id"])
        report["betweenness"] = {
            "top_k": cfg.top_k,
            "subgraph_nodes": betweenness.get("nodes"),
            "subgraph_edges": betweenness.get("edges"),
            **{k: v for k, v in betweenness.items() if k not in ("nodes", "edges")},
        }
    return report


def _print_dry_run_plan(cfg: BridgesConfig) -> None:
    print("DRY RUN — план прогона bridges (подключений нет):")
    print(f"  PG DSN:          {'<set>' if cfg.pg_dsn else '<MISSING ' + ENV_PG_DSN + '>'}")
    print(f"  PG session:      default_transaction_read_only=on, "
          f"statement_timeout={cfg.statement_timeout_s:.0f}s")
    print("  1) BRIDGES_SQL:  cross-cluster рёбра (src.cluster_id <> tgt.cluster_id)")
    print("     → JSON: список мостиков + разрезы by_link_type / by_namespace_pair")
    if cfg.with_betweenness:
        print(f"  2) DEGREE_SQL:   степени узлов → топ-{cfg.top_k} подграф")
        print("  3) SUBGRAPH_SQL: рёбра между топ-узлами (ANY($1::uuid[]))")
        print(f"  4) igraph betweenness (undirected), таймаут {cfg.betweenness_timeout_s:.0f}s")
    else:
        print("  betweenness:     выключен (--betweenness чтобы включить)")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="напечатать план и выйти")
    parser.add_argument("--json", dest="json_path", help="путь JSON-выхода (default: stdout)")
    parser.add_argument("--betweenness", action="store_true", help="betweenness топ-degree подграфа")
    parser.add_argument("--top-k", type=int, help=f"узлов подграфа betweenness (default: {DEFAULT_TOP_K})")
    parser.add_argument("--timeout-s", type=float, help="PG statement_timeout, с (default: 30)")
    parser.add_argument(
        "--btw-timeout-s", type=float, dest="btw_timeout_s",
        help=f"таймаут betweenness, с (default: {DEFAULT_BETWEENNESS_TIMEOUT_S})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = BridgesConfig.from_env(
        statement_timeout_s=args.timeout_s,
        top_k=args.top_k,
        betweenness_timeout_s=args.btw_timeout_s,
        with_betweenness=args.betweenness,
    )
    if args.dry_run:
        _print_dry_run_plan(cfg)
        return 0
    if not cfg.pg_dsn:
        print(f"error: задай {ENV_PG_DSN} (см. README.md)", file=sys.stderr)
        return 2

    report = asyncio.run(run(cfg))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
