"""Тесты T0.2: косинус-гистограмма — чистая математика + UAT in-memory.

Асинхронные прогонки через asyncio.run внутри sync-тестов: нулевые
зависимости от pytest-asyncio, тести跑了 гонки event-loop-ов нет.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

import numpy as np
import pytest

from tools.diagnostics.cosine_histogram import (
    DiagConfig,
    cosine_matrix,
    histogram_counts,
    render_markdown,
    retrieve_vectors_batched,
    shares_above,
    summarize_namespace,
    upper_triangle_values,
)

# Пограничные значения на порогах бакетов/шэров в float32 гуляют на ulp
# (~1e-7): в тестах держим значения отдалённо от границ, строгость
# сравнения проверяем на float64-массиве отдельно.


# ── cosine_matrix ────────────────────────────────────────────────────


def test_cosine_matrix_identical_orthogonal_axis() -> None:
    vectors = np.array([[1, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    similarity = cosine_matrix(vectors)
    assert similarity[0, 1] == pytest.approx(1.0)      # идентичные
    assert similarity[0, 2] == pytest.approx(0.0)      # ортогональные
    assert similarity[1, 2] == pytest.approx(0.0)
    np.testing.assert_allclose(similarity, similarity.T, atol=1e-6)


def test_cosine_matrix_antiparallel() -> None:
    vectors = np.array([[1, 0], [-1, 0]], dtype=np.float32)
    assert cosine_matrix(vectors)[0, 1] == pytest.approx(-1.0)


def test_cosine_matrix_zero_row_is_zero_not_nan() -> None:
    # Нулевой вектор не даёт NaN: строка зануляется, косинус = 0
    vectors = np.array([[1, 0, 0], [0, 0, 0], [0, 2, 0]], dtype=np.float32)
    similarity = cosine_matrix(vectors)
    assert np.isfinite(similarity).all()
    assert similarity[0, 1] == pytest.approx(0.0)
    assert similarity[1, 2] == pytest.approx(0.0)


def test_cosine_matrix_float32_and_scale_invariant() -> None:
    vectors = np.array([[3, 4], [30, 40]], dtype=np.float64)  # масштаб не важен
    similarity = cosine_matrix(vectors)
    assert similarity.dtype == np.float32
    assert similarity[0, 1] == pytest.approx(1.0, abs=1e-6)


# ── upper_triangle_values ────────────────────────────────────────────


def test_upper_triangle_excludes_diagonal_and_mirror() -> None:
    matrix = np.array([[1.0, 0.5, 0.2], [0.5, 1.0, 0.9], [0.2, 0.9, 1.0]], dtype=np.float32)
    values = upper_triangle_values(matrix)
    np.testing.assert_allclose(sorted(values.tolist()), [0.2, 0.5, 0.9], atol=1e-6)


def test_upper_triangle_degenerate_inputs() -> None:
    assert upper_triangle_values(np.empty((0, 0), dtype=np.float32)).size == 0
    assert upper_triangle_values(np.ones((1, 1), dtype=np.float32)).size == 0


# ── histogram / shares ───────────────────────────────────────────────


def test_histogram_counts_bucket_edges() -> None:
    # Семантика границ: значение принадлежит бакету, который оно ОТКРЫВАЕТ
    # (0.05 → [0.05, 0.10)); 0.0 → [0.00, 0.05); 1.0 клипнут в последний
    # бакет [0.95, 1.00] — правая граница включающе.
    values = np.array([-1.0, -0.999, 0.0, 0.049, 0.05, 1.0], dtype=np.float32)
    counts = histogram_counts(values)
    assert counts["-1.00--0.95"] == 2   # -1.0 и -0.999
    assert counts["0.00-0.05"] == 2     # 0.0 и 0.049
    assert counts["0.05-0.10"] == 1     # 0.05 — открывает следующий бакет
    assert counts["0.95-1.00"] == 1     # 1.0 клипнут в последний бакет
    assert sum(counts.values()) == len(values)


def test_histogram_counts_empty() -> None:
    assert histogram_counts(np.empty(0, dtype=np.float32)) == {}


def test_shares_above_strict_comparison() -> None:
    # Значения отдалённо от порогов (float32-пограничники гуляют на ulp)
    values = np.array([0.7999, 0.86, 0.9001, 0.9601, 0.10], dtype=np.float32)
    shares = shares_above(values, thresholds=(0.80, 0.85, 0.90, 0.95))
    assert shares[">0.80"] == pytest.approx(3 / 5)
    assert shares[">0.85"] == pytest.approx(3 / 5)
    assert shares[">0.90"] == pytest.approx(2 / 5)
    assert shares[">0.95"] == pytest.approx(1 / 5)


def test_shares_above_strict_on_exact_double() -> None:
    # Строгость «>» без конверсионных сюрпризов: float64-массив
    values = np.array([0.80, 0.81, 0.95], dtype=np.float64)
    shares = shares_above(values, thresholds=(0.80, 0.95))
    assert shares[">0.80"] == pytest.approx(2 / 3)   # ровно 0.80 НЕ входит
    assert shares[">0.95"] == pytest.approx(0.0)


def test_summarize_namespace_empty_and_full() -> None:
    empty = summarize_namespace(np.empty(0, dtype=np.float32))
    assert empty["pairs"] == 0 and empty["histogram"] == {}
    values = np.array([0.1, 0.2, 0.91, 0.97], dtype=np.float32)
    stats = summarize_namespace(values)
    assert stats["pairs"] == 4
    assert stats["max"] == pytest.approx(0.97, abs=1e-6)
    assert stats["shares"][">0.90"] == pytest.approx(0.5)


# ── UAT: пайплайн на in-memory Qdrant + подсунутом сэмпле ───────────


@dataclass
class _Row:
    """Мок asyncpg.Record из SAMPLE_SQL."""
    id: str
    namespace: str

    def __getitem__(self, key: str) -> str:
        return getattr(self, key)


def test_pipeline_end_to_end_inmemory_qdrant() -> None:
    from qdrant_client import QdrantClient
    from qdrant_client import models as qm

    client = QdrantClient(":memory:")
    client.create_collection(
        "diag",
        vectors_config=qm.VectorParams(size=8, distance=qm.Distance.COSINE),
    )
    # Два «тематических» направления + наклонный шум: ожидаем заметную
    # долю пар > 0.80 внутри направлений и почти нулевую между ними.
    rng = np.random.default_rng(42)
    ids: list[str] = []
    vectors: list[list[float]] = []
    for i in range(12):
        granule_id = f"00000000-0000-0000-0000-{i:012d}"
        base = [1.0, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] if i < 6 else [0.0, 0.0, 0.0, 0.9, 0.2, 0.0, 0.0, 0.0]
        noise = rng.normal(0.0, 0.01, size=8)
        vector = (np.array(base) + noise).tolist()
        ids.append(granule_id)
        vectors.append(vector)
        client.upsert(
            "diag",
            points=[qm.PointStruct(id=granule_id, vector=vector, payload={})],
        )

    rows = [_Row(id=gid, namespace="code_knowledge" if i < 6 else "dialogue_insights") for i, gid in enumerate(ids)]
    cfg = DiagConfig(pg_dsn="unused", qdrant_url=":memory:", sample_per_ns=6, seed="test")

    async def fake_fetch(_: DiagConfig) -> list[_Row]:
        return rows

    def fake_retrieve(batch_ids: list[str]) -> dict[str, list[float]]:
        # batch=3: три последовательных батча по 256 не нужны — проверяем
        # саму механику дробления
        return retrieve_vectors_batched(client, "diag", batch_ids, 3)

    report = asyncio.run(run_with(fake_fetch, fake_retrieve, cfg))

    code = report["namespaces"]["code_knowledge"]
    assert code["sampled"] == 6 and code["vectors"] == 6
    assert code["missing_sync_pct"] == 0.0
    assert code["stats"]["pairs"] == math.comb(6, 2)
    # Однонаправленный кластер с шумом 0.01: все пары должны быть > 0.80
    assert code["stats"]["shares"][">0.80"] > 0.99
    markdown = render_markdown(report)
    assert "code_knowledge" in markdown and "dialogue_insights" in markdown
    client.close()


async def run_with(fetch: object, retrieve: object, cfg: DiagConfig) -> dict:
    from tools.diagnostics.cosine_histogram import run

    return await run(cfg, fetch_sample=fetch, retrieve=retrieve)  # type: ignore[arg-type]


def test_pipeline_counts_missing_vectors_as_sync_gap() -> None:
    # Половина сэмпла без вектора в Qdrant → missing_sync_pct ≈ 50,
    # отчёт не падает, pairs считается по найденным
    from qdrant_client import QdrantClient
    from qdrant_client import models as qm

    client = QdrantClient(":memory:")
    client.create_collection("diag", vectors_config=qm.VectorParams(size=4, distance=qm.Distance.COSINE))
    ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(4)]
    for granule_id in ids[:2]:
        client.upsert("diag", points=[qm.PointStruct(id=granule_id, vector=[1.0, 0.0, 0.0, 0.0], payload={})])
    rows = [_Row(id=gid, namespace="infrastructure") for gid in ids]
    cfg = DiagConfig(pg_dsn="unused", qdrant_url=":memory:")

    async def fake_fetch(_: DiagConfig) -> list[_Row]:
        return rows

    report = asyncio.run(run_with(fake_fetch, lambda batch_ids: retrieve_vectors_batched(client, "diag", batch_ids, 256), cfg))
    infra = report["namespaces"]["infrastructure"]
    assert infra["sampled"] == 4 and infra["vectors"] == 2
    assert infra["missing_in_qdrant"] == 2
    assert infra["missing_sync_pct"] == 50.0
    client.close()
