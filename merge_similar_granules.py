"""Merge similar memory granules in selti.

Thin wrapper: Qdrant ANN → PostgreSQL clustering → JSON plan.

Архитектура v2:
  1. Qdrant search_points — ANN поиск кандидатов (HNSW, cosine)
  2. PostgreSQL merge_similar_granules() — кластеризация через recursive CTE
  3. Python — только оркестрация, без вычислений

Было (v1):
  - Все векторы в Python → O(N²) по памяти
  - Similarity матрица в numpy → медленно
  - Union-Find в Python → нормально

Стало (v2):
  - ANN в Qdrant → O(N * K) где K ≈ 10-50
  - Кластеризация в PostgreSQL → recursive CTE
  - Python = thin wrapper (~100 строк)

Использование:
    python merge_similar_granules.py --threshold 0.9 --limit 50 --dry-run
    python merge_similar_granules.py --threshold 0.9 --limit 50 --execute
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import asyncpg
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

# ════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════

DEFAULT_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://svc_athene_ai:changeme@localhost:5432/memory",
).replace("postgresql+asyncpg://", "postgresql://")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "memories")
OUTPUT_PATH = Path("/tmp/merge_plan.json")

# ANN параметры
ANN_LIMIT = 20  # сколько ближайших соседей искать для каждой гранулы
BATCH_SIZE = 100  # размер батча для поиска в Qdrant


# ════════════════════════════════════════════════════════════════
# 1. Загрузка кандидатов из PostgreSQL
# ════════════════════════════════════════════════════════════════

async def fetch_candidates(db_url: str, limit: int) -> list[dict]:
    """Получить гранулы с importance < 5 и не в архиве."""
    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch("""
            SELECT id::text, content, namespace, importance,
                   COALESCE(metadata->>'project_id', '') AS project_id
            FROM memories
            WHERE importance < 5
              AND is_archived = false
            ORDER BY importance DESC, created_at DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ════════════════════════════════════════════════════════════════
# 2. ANN поиск кандидатов в Qdrant
# ════════════════════════════════════════════════════════════════

def find_similar_pairs(
    qdrant: QdrantClient,
    granules: list[dict],
    threshold: float,
    ann_limit: int = ANN_LIMIT,
) -> list[tuple[str, str, float]]:
    """Для каждой гранулы находим ближайших соседей через Qdrant ANN.

    Returns: [(source_id, target_id, similarity), ...]
    """
    pairs: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()

    # Группируем по namespace для фильтрации
    by_namespace: dict[str, list[dict]] = {}
    for g in granules:
        by_namespace.setdefault(g["namespace"], []).append(g)

    for namespace, ns_granules in by_namespace.items():
        # Берём векторы из Qdrant для этого namespace
        ids = [g["id"] for g in ns_granules]
        if not ids:
            continue

        # Batch получение векторов
        result = qdrant.retrieve(
            collection_name=QDRANT_COLLECTION,
            ids=ids,
            with_vectors=True,
        )

        vectors = {str(p.id): p.vector for p in result if p.vector}

        # Для каждого вектора ищем ближайших соседей
        for granule in ns_granules:
            gid = granule["id"]
            if gid not in vectors:
                continue

            vector = vectors[gid]

            # ANN поиск через Qdrant HNSW
            search_result = qdrant.search(
                collection_name=QDRANT_COLLECTION,
                query_vector=vector,
                limit=ann_limit + 1,  # +1 чтобы исключить саму себя
                score_threshold=threshold,
                query_filter=Filter(
                    must=[
                        FieldCondition(
                            key="namespace",
                            match=MatchValue(value=namespace)
                        )
                    ]
                ) if namespace != "default" else None,
            )

            for hit in search_result:
                hit_id = str(hit.id)
                if hit_id == gid:
                    continue  # исключаем себя
                if hit_id not in {g["id"] for g in ns_granules}:
                    continue  # только внутри кандидатов

                pair = tuple(sorted([gid, hit_id]))
                if pair not in seen:
                    seen.add(pair)
                    pairs.append((gid, hit_id, hit.score))

    return pairs


# ════════════════════════════════════════════════════════════════
# 3. Загрузка пар в PostgreSQL + вызов хранимки
# ════════════════════════════════════════════════════════════════

async def run_clustering(
    db_url: str,
    pairs: list[tuple[str, str, float]],
    threshold: float,
    max_groups: int,
) -> dict:
    """Загружает пары в PG, вызывает merge_similar_granules()."""
    conn = await asyncpg.connect(db_url)
    try:
        # Очищаем временную таблицу
        await conn.execute("TRUNCATE _similarity_pairs")

        # Загружаем пары батчами
        if pairs:
            records = [(s, t, sim) for s, t, sim in pairs]
            await conn.executemany(
                "INSERT INTO _similarity_pairs (source_id, target_id, similarity) VALUES ($1, $2, $3)",
                records,
            )

        # Вызываем хранимку
        row = await conn.fetchrow(
            "SELECT merge_similar_granules($1, $2) AS result",
            threshold,
            max_groups,
        )

        return json.loads(row["result"]) if row and row["result"] else {}
    finally:
        await conn.close()


# ════════════════════════════════════════════════════════════════
# 4. Вывод и сохранение
# ════════════════════════════════════════════════════════════════

def print_stats(plan: dict, elapsed: float):
    total_groups = plan.get("total_groups", 0)
    total_granules = plan.get("total_granules", 0)
    total_pairs = plan.get("total_pairs", 0)
    pg_ms = plan.get("elapsed_ms", 0)

    print("\n" + "=" * 60)
    print("MERGE PLAN STATISTICS (v2 — Qdrant ANN + PG clustering)")
    print("=" * 60)
    print(f"  Similarity pairs found:  {total_pairs}")
    print(f"  Merge groups:            {total_groups}")
    print(f"  Granules in groups:      {total_granules}")
    print(f"  Qdrant + Python time:    {elapsed:.2f}s")
    print(f"  PostgreSQL clustering:   {pg_ms:.1f}ms")
    print("=" * 60)

    groups = plan.get("groups") or []
    for g in groups[:10]:
        sim = g.get("similarity_score", 0)
        core = g.get("core_id", "?")[:8]
        members = g.get("member_count", 0)
        print(f"\n  Group {g.get('group_id')} (sim={sim:.4f}, members={members}):")
        print(f"    Core: {core}... imp={g.get('core_importance')}")

        for m in (g.get("members") or [])[:5]:
            marker = " *" if m.get("is_core") else "  "
            mid = m.get("id", "?")[:8]
            content = m.get("content", "")[:100].replace("\n", " ")
            print(f"    {marker} {mid}... imp={m.get('importance')} ns={m.get('namespace')}")
            print(f"       {content}")

    if len(groups) > 10:
        print(f"\n  ... and {len(groups) - 10} more groups")


def save_plan(plan: dict, output_path: Path):
    output_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    print(f"\nPlan saved to: {output_path}")


# ════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════

async def run(
    threshold: float,
    limit: int,
    dry_run: bool,
    db_url: str,
    output_path: Path = OUTPUT_PATH,
    ann_limit: int = ANN_LIMIT,
):
    start = time.monotonic()

    # 1. Загрузка кандидатов
    print(f"[1/4] Fetching candidates (importance < 5, limit={limit})...")
    granules = await fetch_candidates(db_url, limit)
    print(f"  Found: {len(granules)} granules")

    if len(granules) < 2:
        print("Not enough granules to compare.")
        return

    # 2. ANN поиск кандидатов в Qdrant
    print(f"[2/4] ANN search in Qdrant (threshold={threshold}, ann_limit={ann_limit})...")
    qdrant = QdrantClient(url=QDRANT_URL)
    pairs = find_similar_pairs(qdrant, granules, threshold, ann_limit)
    print(f"  Similar pairs found: {len(pairs)}")

    if not pairs:
        print("No similar pairs found.")
        return

    # 3. Кластеризация в PostgreSQL
    print("[3/4] Clustering in PostgreSQL...")
    plan = await run_clustering(db_url, pairs, threshold, max_groups=50)

    # 4. Вывод и сохранение
    elapsed = time.monotonic() - start
    print("[4/4] Results:")
    print_stats(plan, elapsed)
    save_plan(plan, output_path)

    if not dry_run:
        print("\n[EXECUTE MODE] Plan ready. Pass to Tish via task tool.")


def main():
    parser = argparse.ArgumentParser(description="Merge similar memory granules (v2)")
    parser.add_argument("--threshold", type=float, default=0.9, help="Cosine similarity threshold (default: 0.9)")
    parser.add_argument("--limit", type=int, default=2000, help="Max granules to scan (default: 2000)")
    parser.add_argument("--ann-limit", type=int, default=ANN_LIMIT, help="ANN neighbors per granule (default: 20)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Only generate plan (default)")
    parser.add_argument("--execute", action="store_true", help="Generate plan + output task payload")
    parser.add_argument("--db-url", type=str, default=DEFAULT_DB_URL, help="PostgreSQL connection URL")
    parser.add_argument("--output", type=str, default=str(OUTPUT_PATH), help="Output JSON path")
    args = parser.parse_args()

    output = Path(args.output)
    dry_run = not args.execute

    asyncio.run(run(
        threshold=args.threshold,
        limit=args.limit,
        dry_run=dry_run,
        db_url=args.db_url,
        output_path=output,
        ann_limit=args.ann_limit,
    ))


if __name__ == "__main__":
    main()
