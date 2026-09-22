"""cosine_histogram.py — T0.2 «Жизнь графа знаний» (V3.5).

Диагностический READ ONLY-скрипт для прода: стратифицированный сэмпл
гранул per-namespace из PostgreSQL (детерминированный, md5-порядок с
фиксированным seed) -> векторы из Qdrant (retrieve батчами по 256,
последовательно — без параллельного давления) -> матричный pairwise-
косинус (numpy, float32) -> гистограмма (бакет 0.05) и доли пар выше
порогов 0.80/0.85/0.90/0.95 per-namespace. Выход: markdown-отчёт + JSON.

Память: 3000 векторов x 4096 dim float32 ~ 49 МБ + симметричная матрица
3000x3000 ~ 36 МБ — суммарно < 150 МБ на namespace (лимит задачи 1 ГБ
с запасом); матрица освобождается сразу после извлечения треугольника.

Никаких секретов в коде: подключение — только env (см. README.md).
Прод-окно: после 05:30 UTC. Против прода запускает Рэй.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

# ── Конфигурация (env, секреты только здесь) ─────────────────────────

ENV_PG_DSN = "SELTI_DIAG_PG_DSN"
ENV_QDRANT_URL = "SELTI_DIAG_QDRANT_URL"
ENV_QDRANT_COLLECTION = "SELTI_DIAG_QDRANT_COLLECTION"
ENV_QDRANT_API_KEY = "SELTI_DIAG_QDRANT_API_KEY"

DEFAULT_COLLECTION = "memories"
DEFAULT_SAMPLE_PER_NS = 3000
DEFAULT_BATCH = 256          # RETRIEVE_BATCH_SIZE из qdrant_store.py
DEFAULT_SEED = "selti-diag-v3.5"
DEFAULT_STATEMENT_TIMEOUT_S = 30.0

# Пороги долей пар (зоны линкера/дедупа, config.py: linker_synonym_threshold
# 0.80, linker_verdict_threshold 0.85, cluster_threshold 0.92, dedup 0.95).
SHARE_THRESHOLDS = (0.80, 0.85, 0.90, 0.95)
BUCKET_WIDTH = 0.05

# Стратифицированный сэмпл: row_number per namespace_id по md5(id || seed).
# md5 вместо ORDER BY random(): выборка воспроизводима между прогонами —
# два диагностических запуска с одним seed видят ОДНИ И ТЕ ЖЕ гранулы
# (random() в PG сеансово-случаен, воспроизвести прогон нельзя).
SAMPLE_SQL = """
    SELECT s.id, n.uid AS namespace
    FROM (
        SELECT id, namespace_id,
               row_number() OVER (
                   PARTITION BY namespace_id
                   ORDER BY md5(id::text || $2), id
               ) AS rn
        FROM memories
        WHERE status = 'asserted' AND valid_to IS NULL
    ) s
    JOIN namespaces n ON n.id = s.namespace_id
    WHERE s.rn <= $1::int
      AND ($3::text[] IS NULL OR n.uid = ANY($3::text[]))
"""


@dataclass(frozen=True)
class DiagConfig:
    """Всё, что влияет на прогон; собирается из env + CLI-переопределений."""

    pg_dsn: str
    qdrant_url: str = ""  # обязателен только для T0.2; bridges.py не ходит в Qdrant
    qdrant_collection: str = DEFAULT_COLLECTION
    qdrant_api_key: str = ""
    seed: str = DEFAULT_SEED
    sample_per_ns: int = DEFAULT_SAMPLE_PER_NS
    batch: int = DEFAULT_BATCH
    statement_timeout_s: float = DEFAULT_STATEMENT_TIMEOUT_S
    namespaces: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, **overrides: Any) -> "DiagConfig":
        """env-дефолты + явные переопределения (CLI). Пустые → дефолты."""
        merged: dict[str, Any] = {
            "pg_dsn": os.environ.get(ENV_PG_DSN, ""),
            "qdrant_url": os.environ.get(ENV_QDRANT_URL, ""),
            "qdrant_collection": os.environ.get(ENV_QDRANT_COLLECTION, DEFAULT_COLLECTION),
            "qdrant_api_key": os.environ.get(ENV_QDRANT_API_KEY, ""),
        }
        merged.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**merged)


# ── Чистая математика (тестируется pytest без PG/Qdrant) ─────────────


def cosine_matrix(vectors: "np.ndarray") -> "np.ndarray":
    """Симметричная матрица pairwise-косинусов строк.

    Вход приводится к float32 (4096-dim float64 удвоил бы память без
    пользы для диагностики). Нулевые/немые строки не делятся на ноль:
    норма 0 заменяется на 1 → строка зануляется, косинус с ней = 0.
    """
    import numpy as np

    matrix = np.ascontiguousarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    unit = matrix / np.where(norms == 0.0, np.float32(1.0), norms)
    return unit @ unit.T


def upper_triangle_values(matrix: "np.ndarray") -> "np.ndarray":
    """Значения строго верхнего треугольника (диагональ = самокосинус 1.0
    не участвует; нижний — зеркало верхнего). Fancy indexing возвращает
    копию — матрицу вызывающий может освобождать сразу после вызова."""
    import numpy as np

    n = matrix.shape[0]
    if n < 2:
        return np.empty(0, dtype=np.float32)
    return matrix[np.triu_indices(n, k=1)]


def histogram_counts(values: "np.ndarray", width: float = BUCKET_WIDTH) -> dict[str, int]:
    """Счётчики пар по бакетам [-1, 1]; ключ — метка «lo–hi».

    Границы: floor((v + 1) / width) даёт индекс бакета; v = 1.0 попадает
    в последний бакет клипом (правая граница включающе).
    """
    import numpy as np

    bucket_count = int(round(2.0 / width))
    if values.size == 0:
        return {}
    idx = np.floor((values.astype(np.float64) + 1.0) / width).astype(np.int64)
    idx = np.clip(idx, 0, bucket_count - 1)
    counts = np.bincount(idx, minlength=bucket_count)
    return {
        f"{-1.0 + i * width:.2f}-{(-1.0 + (i + 1) * width):.2f}": int(counts[i])
        for i in range(bucket_count)
        if counts[i] > 0
    }


def shares_above(values: "np.ndarray", thresholds: Sequence[float] = SHARE_THRESHOLDS) -> dict[str, float]:
    """Доля пар СТРОГО выше каждого порога (0..1). Пустые значения → 0.0."""
    if values.size == 0:
        return {f">{t:.2f}": 0.0 for t in thresholds}
    return {f">{t:.2f}": float((values > t).mean()) for t in thresholds}


def summarize_namespace(values: "np.ndarray") -> dict[str, Any]:
    """Полный разрез одного namespace: гистограмма + перцентили + доли."""
    import numpy as np

    if values.size == 0:
        return {"pairs": 0, "histogram": {}, "shares": shares_above(values)}
    return {
        "pairs": int(values.size),
        "mean": round(float(values.mean()), 4),
        "median": round(float(np.median(values)), 4),
        "p95": round(float(np.percentile(values, 95)), 4),
        "max": round(float(values.max()), 4),
        "histogram": histogram_counts(values),
        "shares": shares_above(values),
    }


# ── I/O-слой (подключения, батчи) ────────────────────────────────────


async def open_read_only_connection(dsn: str, statement_timeout_s: float) -> Any:
    """asyncpg-коннект с двумя сессионными предохранителями.

    server_settings применяются ДО первого стейтмента сессии (asyncpg
    игнорирует PGOPTIONS — замечание Рэя из RUNBOOK_PROD.md п. 3.3, броня
    должна жить в коде). default_transaction_read_only отклоняет любую
    мутацию («cannot execute INSERT in a read-only transaction» —
    подтверждено Рэем на проде 22.09), statement_timeout валит запрос,
    съевший прод-окно.
    """
    import asyncpg

    return await asyncpg.connect(
        dsn.replace("postgresql+asyncpg://", "postgresql://"),
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": str(int(statement_timeout_s * 1000)),
        },
    )


def retrieve_vectors_batched(
    client: Any,
    collection: str,
    ids: list[str],
    batch: int,
) -> dict[str, list[float]]:
    """{id: vector} из Qdrant, последовательными батчами (не параллельно —
    диагностика не должна конкурировать с прод-нагрузкой на HNSW)."""
    vectors: dict[str, list[float]] = {}
    for start in range(0, len(ids), batch):
        chunk = ids[start : start + batch]
        records = client.retrieve(
            collection_name=collection,
            ids=chunk,
            with_payload=False,
            with_vectors=True,
        )
        for record in records:
            if record.vector is not None:
                vectors[str(record.id)] = record.vector
    return vectors


def _ascii_bar(share: float, width: int = 40) -> str:
    """Markdown-бар: 1 символ = 2.5% доли пар, полный бар = 100%."""
    return "#" * min(width, int(round(share / 0.025)))


def render_markdown(report: dict[str, Any]) -> str:
    """Отчёт-целина: сводная таблица + гистограммы per-namespace."""
    lines: list[str] = [
        "# Cosine histogram — linker/dedup zone audit",
        "",
        f"generated_at: {report['generated_at']}  seed: `{report['seed']}`  "
        f"sample_per_ns: {report['sample_per_ns']}",
        "",
        "## Summary per namespace",
        "",
        "| namespace | sampled | vectors | missing_sync | pairs | mean | median | p95 | max |"
        + "".join(f" >{t:.2f} |" for t in SHARE_THRESHOLDS),
        "|---|---|---|---|---|---|---|---|---|" + "---|" * len(SHARE_THRESHOLDS),
    ]
    for uid, ns_report in sorted(report["namespaces"].items()):
        stats = ns_report["stats"]
        shares = stats["shares"]
        missing_pct = ns_report["missing_sync_pct"]
        lines.append(
            f"| {uid} | {ns_report['sampled']} | {ns_report['vectors']} "
            f"| {missing_pct:.1f}% | {stats['pairs']} | {stats.get('mean', '-')} "
            f"| {stats.get('median', '-')} | {stats.get('p95', '-')} "
            f"| {stats.get('max', '-')} |"
            + "".join(f" {shares[f'>{t:.2f}']:.4f} |" for t in SHARE_THRESHOLDS)
        )
    lines += ["", "## Histograms (bucket 0.05, non-zero buckets)", ""]
    for uid, ns_report in sorted(report["namespaces"].items()):
        total_pairs = ns_report["stats"]["pairs"]
        if not total_pairs:
            continue
        lines += [f"### {uid} ({total_pairs} pairs)", "", "| bucket | share | bar |", "|---|---|---|"]
        for bucket, count in ns_report["stats"]["histogram"].items():
            share = count / total_pairs
            lines.append(f"| {bucket} | {share:.4f} | {_ascii_bar(share)} |")
        lines.append("")
    return "\n".join(lines)


# ── Пайплайн ─────────────────────────────────────────────────────────


async def run(
    cfg: DiagConfig,
    fetch_sample: Callable[[DiagConfig], Any] | None = None,
    retrieve: Callable[[list[str]], dict[str, list[float]]] | None = None,
) -> dict[str, Any]:
    """Прогон целиком. fetch_sample/retrieve — точки подмены для тестов
    (инъекция зависимостей: UAT на in-memory Qdrant без прода)."""

    import numpy as np

    async def _default_fetch(cfg: DiagConfig) -> list[Any]:
        conn = await open_read_only_connection(cfg.pg_dsn, cfg.statement_timeout_s)
        try:
            ns_filter = list(cfg.namespaces) or None
            return await conn.fetch(SAMPLE_SQL, cfg.sample_per_ns, cfg.seed, ns_filter)
        finally:
            await conn.close()

    def _default_retrieve(ids: list[str]) -> dict[str, list[float]]:
        from qdrant_client import QdrantClient

        client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None)
        try:
            return retrieve_vectors_batched(client, cfg.qdrant_collection, ids, cfg.batch)
        finally:
            client.close()

    fetch = fetch_sample or _default_fetch
    get_vectors = retrieve or _default_retrieve

    rows = await fetch(cfg)
    by_ns: dict[str, list[str]] = {}
    for row in rows:
        by_ns.setdefault(row["namespace"], []).append(str(row["id"]))

    namespaces_report: dict[str, Any] = {}
    for uid, ids in sorted(by_ns.items()):
        # NaN/inf-строки отбрасываем до матрицы: один немой вектор
        # превратил бы в NaN весь отчёт namespace'а.
        vectors_map = get_vectors(ids)
        missing = [gid for gid in ids if gid not in vectors_map]
        clean = {
            gid: vec
            for gid, vec in vectors_map.items()
            if all(math.isfinite(x) for x in vec)
        }
        nan_rows = len(vectors_map) - len(clean)
        matrix = (
            cosine_matrix(np.array([clean[gid] for gid in sorted(clean)], dtype=np.float32))
            if len(clean) >= 2
            else None
        )
        values = upper_triangle_values(matrix) if matrix is not None else np.empty(0)
        matrix = None  # пиковая память: треугольник извлечён, матрицу дропаем
        namespaces_report[uid] = {
            "sampled": len(ids),
            "vectors": len(clean),
            "missing_in_qdrant": len(missing),
            "missing_sync_pct": round(100.0 * len(missing) / len(ids), 2) if ids else 0.0,
            "nan_rows_dropped": nan_rows,
            "stats": summarize_namespace(values),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": cfg.seed,
        "sample_per_ns": cfg.sample_per_ns,
        "bucket_width": BUCKET_WIDTH,
        "namespaces": namespaces_report,
    }


def _print_dry_run_plan(cfg: DiagConfig) -> None:
    """План без подключений: что/куда/сколько будет запрошено."""
    target = "in-memory Qdrant (local check)" if cfg.qdrant_url == ":memory:" else cfg.qdrant_url
    print("DRY RUN — план прогона cosine_histogram (подключений нет):")
    print(f"  PG DSN:            {'<set>' if cfg.pg_dsn else '<MISSING ' + ENV_PG_DSN + '>'}")
    print(f"  Qdrant:            {target or '<MISSING ' + ENV_QDRANT_URL + '>'}")
    print(f"  collection:        {cfg.qdrant_collection}")
    print(f"  seed:              {cfg.seed}")
    print(f"  sample per ns:     {cfg.sample_per_ns}")
    print(f"  retrieve batch:    {cfg.batch} (последовательно)")
    print(f"  PG session:        default_transaction_read_only=on, "
          f"statement_timeout={cfg.statement_timeout_s:.0f}s")
    if cfg.namespaces:
        print(f"  namespaces filter: {', '.join(cfg.namespaces)}")
    print("  шаги: 1) stratified sample SQL (row_number per namespace, md5-seed)")
    print("        2) Qdrant retrieve векторов батчами")
    print("        3) float32 pairwise-cosine, верхний треугольник")
    print("        4) histogram 0.05 + shares >" + "/>".join(f"{t:.2f}" for t in SHARE_THRESHOLDS))
    print("        5) markdown + JSON")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="напечатать план и выйти")
    parser.add_argument("--seed", help=f"seed сэмплирования (default: {DEFAULT_SEED})")
    parser.add_argument("--sample-per-ns", type=int, help="гранул на namespace (default: 3000)")
    parser.add_argument("--namespaces", help="CSV uid-фильтр (default: все)")
    parser.add_argument("--json", dest="json_path", help="путь JSON-отчёта (default: stdout)")
    parser.add_argument("--markdown", dest="md_path", help="путь markdown-отчёта (default: stdout)")
    parser.add_argument("--timeout-s", type=float, help="PG statement_timeout, с (default: 30)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = DiagConfig.from_env(
        seed=args.seed,
        sample_per_ns=args.sample_per_ns,
        statement_timeout_s=args.timeout_s,
        namespaces=tuple(n.strip() for n in args.namespaces.split(",")) if args.namespaces else (),
    )
    if args.dry_run:
        _print_dry_run_plan(cfg)
        return 0
    if not cfg.pg_dsn or not cfg.qdrant_url:
        print(f"error: задай {ENV_PG_DSN} и {ENV_QDRANT_URL} (см. README.md)", file=sys.stderr)
        return 2

    report = asyncio.run(run(cfg))
    markdown = render_markdown(report)
    payload = json.dumps(report, ensure_ascii=False, indent=2)

    if args.md_path:
        with open(args.md_path, "w", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    else:
        print(markdown)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        print("\n--- JSON ---\n" + payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
