"""Юнит-тесты search_fusion (Фаза 1.1/1.2): RRF, MMR, decay, importance.

Чистые функции на фиксированном корпусе — без IO и моков репозиториев.
"""
import pytest

from memory_server.memory.search_fusion import (
    final_score,
    importance_weight,
    mmr_rerank,
    recency_decay,
    rrf_fuse,
)


# ---------------------------------------------------------------------------
# RRF (score = Σ 1/(k + rank))
# ---------------------------------------------------------------------------

class TestRRF:
    def test_single_channel_top_rank(self):
        scores = rrf_fuse([["a", "b", "c"]])
        assert scores["a"] == 1 / 60
        assert scores["b"] == 1 / 61
        assert scores["c"] == 1 / 62

    def test_two_channels_sum(self):
        """Документ в обоих каналах — вклады суммируются (стандартный RRF)."""
        scores = rrf_fuse([["a", "b"], ["b", "a"]])
        assert scores["a"] == 1 / 60 + 1 / 61
        assert scores["b"] == 1 / 61 + 1 / 60

    def test_doc_from_single_channel_only(self):
        scores = rrf_fuse([["a"], ["b"]])
        assert scores["a"] == 1 / 60
        assert scores["b"] == 1 / 60

    def test_custom_k(self):
        scores = rrf_fuse([["a"]], k=100)
        assert scores["a"] == 1 / 100

    def test_empty_rankings(self):
        assert rrf_fuse([]) == {}
        assert rrf_fuse([[], []]) == {}

    def test_overlap_outranks_single_channel(self):
        """Пересечение каналов должно опережать документ из одного канала."""
        scores = rrf_fuse([["x", "a"], ["a"]])
        assert scores["a"] > scores["x"]


# ---------------------------------------------------------------------------
# MMR (λ·rel − (1−λ)·max_sim)
# ---------------------------------------------------------------------------

class TestMMR:
    def test_lambda_one_is_pure_relevance(self):
        """λ=1: diversity выключен — порядок чистой релевантности."""
        rel = {"a": 0.9, "b": 0.8, "c": 0.7}
        order = mmr_rerank(rel, vectors={}, top_k=3, lambda_=1.0)
        assert order == ["a", "b", "c"]

    def test_lambda_zero_maximizes_diversity(self):
        """λ=0: релевантность выключена — каждый выбор максимально непохож на выбранные."""
        # a и b — коллинеарны (sim=1), c — ортогонален
        vectors = {
            "a": [1.0, 0.0],
            "b": [1.0, 0.0],
            "c": [0.0, 1.0],
        }
        order = mmr_rerank({"a": 0.9, "b": 0.85, "c": 0.1}, vectors, top_k=2, lambda_=0.0)
        assert order[0] == "a"  # ties: максимум rel на пустом selected
        assert order[1] == "c"  # sim(a,c)=0 < sim(a,b)=1

    def test_diverse_beats_similar_at_default_lambda(self):
        """λ=0.7: почти дубль (sim≈1) уступает разнообразному кандидату
        с чуть меньшей релевантностью — ради разнообразия топ-K."""
        vectors = {
            "q1": [1.0, 0.0],
            "q2": [0.999, 0.01],   # почти копия q1
            "div": [0.0, 1.0],     # ортогонален
        }
        rel = {"q1": 1.0, "q2": 0.95, "div": 0.8}
        order = mmr_rerank(rel, vectors, top_k=2, lambda_=0.7)
        assert order == ["q1", "div"]

    def test_missing_vectors_not_penalized(self):
        """Кандидат без вектора (FTS-only) не штрафуется: sim=0."""
        rel = {"a": 1.0, "b": 0.9, "c": 0.8}
        order = mmr_rerank(rel, vectors={}, top_k=3, lambda_=0.5)
        assert order == ["a", "b", "c"]

    def test_top_k_truncates(self):
        rel = {"a": 1.0, "b": 0.9}
        assert mmr_rerank(rel, {}, top_k=1) == ["a"]

    def test_top_k_zero_returns_empty(self):
        assert mmr_rerank({"a": 1.0}, {}, top_k=0) == []

    def test_empty_relevance_returns_empty(self):
        assert mmr_rerank({}, {}, top_k=5) == []

    def test_deterministic_on_ties(self):
        """Одинаковая релевантность — стабильный лексикографический порядок."""
        rel = {"z": 1.0, "a": 1.0, "m": 1.0}
        assert mmr_rerank(rel, {}, top_k=3) == ["a", "m", "z"]

    def test_zero_vector_does_not_crash(self):
        vectors = {"a": [0.0, 0.0], "b": [1.0, 0.0]}
        order = mmr_rerank({"a": 1.0, "b": 0.9}, vectors, top_k=2)
        assert sorted(order) == ["a", "b"]


# ---------------------------------------------------------------------------
# Ранжирование D4: decay / importance / final
# ---------------------------------------------------------------------------

class TestRecencyDecay:
    def test_zero_days_no_decay(self):
        assert recency_decay(0, 0.995) == 1.0

    def test_negative_days_no_decay(self):
        assert recency_decay(-3, 0.995) == 1.0

    def test_hundred_days(self):
        assert recency_decay(100, 0.995) == pytest.approx(0.995**100)

    def test_older_decays_more(self):
        assert recency_decay(10, 0.995) > recency_decay(100, 0.995)

    def test_year_of_dialogue_insights(self):
        """Год без доступа при rate=0.99: заметное, но не фатальное затухание."""
        assert recency_decay(365, 0.99) == pytest.approx(0.99**365)


class TestImportanceWeight:
    def test_neutral_importance_is_one(self):
        assert importance_weight(3) == 1.0

    def test_max_importance_with_multiplier(self):
        assert importance_weight(5, 1.2) == pytest.approx((5 / 3) * 1.2)

    def test_low_importance(self):
        assert importance_weight(1, 1.0) == pytest.approx(1 / 3)


class TestFinalScore:
    def test_multiplication(self):
        assert final_score(0.5, 0.9, 1.2) == pytest.approx(0.5 * 0.9 * 1.2)

    def test_frozen_equivalent(self):
        """frozen: decay=1.0 — финальный score не затухает."""
        fresh = final_score(0.02, recency_decay(0, 0.995), 1.0)
        frozen = final_score(0.02, 1.0, 1.0)
        assert fresh == frozen
