"""Hybrid search fusion: RRF + MMR + ранжирование D4 (Фаза 1.1/1.2).

Чистые функции без IO — тестируются юнитами на фиксированном корпусе.
Оркестрация каналов (Qdrant dense + PG FTS) — MemoryRepository.search_hybrid,
сборка финального score — MemoryService.search.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence
from uuid import UUID

import numpy as np

RRF_K_DEFAULT = 60
MMR_LAMBDA_DEFAULT = 0.7

# Нейтральная важность шкалы 1..5: importance=3 → вес 1.0
_NEUTRAL_IMPORTANCE = 3.0


@dataclass
class HybridCandidate:
    """Кандидат гибридного поиска после двухканального сбора (до fusion).

    Канонические поля наполняет PG-догрузка (fetch_by_ids); ранги каналов
    и вектор (для MMR) приходят из сбора. rrf_score/final_score — результат
    fusion, заполняет MemoryService.
    """

    id: str
    content: str
    metadata: dict
    namespace: str
    importance: int
    project_id: UUID | None
    status: str
    created_at: datetime | None
    last_accessed_at: datetime | None
    frozen: bool
    rank_dense: int | None = None
    rank_fts: int | None = None
    vector: Sequence[float] | None = None
    rrf_score: float = 0.0
    final_score: float = 0.0


def rrf_fuse(
    rankings: Sequence[Sequence[str]], k: int = RRF_K_DEFAULT
) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(d) = Σ_i 1/(k + rank_i(d)).

    rank — 0-based позиция документа в ранжировании канала i. Документ,
    найденный обоими каналами, получает сумму вкладов — стандартный RRF.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def mmr_rerank(
    relevance: dict[str, float],
    vectors: dict[str, Sequence[float]],
    top_k: int,
    lambda_: float = MMR_LAMBDA_DEFAULT,
) -> list[str]:
    """Maximal Marginal Relevance: разнообразие топ-K без потери релевантности.

    mmr(c) = λ·norm_rel(c) − (1−λ)·max_{s∈S} cos(c, s).

    relevance нормируется к [0,1]: rrf-очки ~1/60, и без нормировки
    diversity-член (косинус 0..1) задавил бы релевантность полностью.
    Вектора есть не у всех кандидатов (FTS-only канал): схожесть пары без
    данных = 0 — кандидат не пенализуется за отсутствие вектора.
    Детерминизм: ties разрываются лексикографическим порядком id.
    """
    if top_k <= 0 or not relevance:
        return []
    max_rel = max(relevance.values())
    if max_rel <= 0:
        return []

    ids = sorted(relevance)
    n = len(ids)
    pos = {doc_id: p for p, doc_id in enumerate(ids)}
    rel = np.array([relevance[i] / max_rel for i in ids])

    # Косинусная матрица (симметричная, диагональ не используется)
    sim = np.zeros((n, n))
    with_vec = [i for i in ids if vectors.get(i)]
    if len(with_vec) >= 2:
        mat = np.asarray([vectors[i] for i in with_vec], dtype=np.float64)
        norms = np.linalg.norm(mat, axis=1)
        np.maximum(norms, 1e-12, out=norms)  # нулевой вектор → без деления на 0
        unit = mat / norms[:, None]
        rows = [pos[i] for i in with_vec]
        sim[np.ix_(rows, rows)] = unit @ unit.T

    selected: list[int] = []
    remaining = list(range(n))  # позиции по возрастанию — детерминизм на ties
    while len(selected) < top_k and remaining:
        best_p, best_val = remaining[0], -np.inf
        for p in remaining:
            penalty = float(sim[p, selected].max()) if selected else 0.0
            value = lambda_ * rel[p] - (1.0 - lambda_) * penalty
            if value > best_val:
                best_p, best_val = p, value
        selected.append(best_p)
        remaining.remove(best_p)
    return [ids[p] for p in selected]


def recency_decay(days_since_access: float, rate: float) -> float:
    """Экспоненциальное затухание свежести: rate^days (D4).

    Отрицательное/нулевое число дней (доступ «сейчас») → без затухания.
    """
    if days_since_access <= 0:
        return 1.0
    return rate**days_since_access


def importance_weight(importance: int, namespace_multiplier: float = 1.0) -> float:
    """Вес важности: importance/3 (нейтраль при 3) × per-namespace множитель."""
    return (importance / _NEUTRAL_IMPORTANCE) * namespace_multiplier


def final_score(rrf_score: float, decay: float, importance: float) -> float:
    """D4: score = rrf × recency_decay × importance_weight."""
    return rrf_score * decay * importance
