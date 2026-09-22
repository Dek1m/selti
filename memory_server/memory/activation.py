"""Activation spreading — traverse(strategy="activation"), V3.5 Ф2.

Персонализированный PageRank на CSR-матрице эффективного графа:
  r_{k+1} = d · M·r_k + (1−d) · e,
M[tgt, src] = w_eff(src→tgt) / Σ исходящих w_eff(src) — directed-переходы
(эталон 5.1 тест-плана Катерины), веса — ленивая w_eff на момент вызова
(вычислена SQL при выборке, здесь только числа). Dead-end узлы (без
исходящих) отдают массу в никуда — стандартный PPR без редистрибуции.

Граф передаётся списком рёбер: модуль — чистая математика без I/O,
юнит-тестируется вручную собранными графами (graph6). 25 итераций на
106k рёбер — миллисекунды (O(iterations × nnz) на scipy-sparse).
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from scipy.sparse import coo_matrix


class ActivationSpreader:
    """PPR-движок: рёбра → CSR один раз, seed-запросы — дёшево."""

    def __init__(self, edges: list[tuple[str, str, float]], damping: float = 0.85):
        if not 0.0 < damping < 1.0:
            raise ValueError(f"damping must be in (0, 1), got {damping}")
        self.damping = damping
        # индексация узлов — единственный Python-проход по рёбрам;
        # фильтрация весов и нормировка — векторно (numpy)
        index: dict[str, int] = {}
        for source, target, _ in edges:
            if source not in index:
                index[source] = len(index)
            if target not in index:
                index[target] = len(index)
        self._index = index
        n = len(index)
        cols = np.fromiter((index[s] for s, _, _ in edges), dtype=np.int64)
        rows = np.fromiter((index[t] for _, t, _ in edges), dtype=np.int64)
        data = np.fromiter((w for _, _, w in edges), dtype=np.float64)
        positive = data > 0.0  # нулевые/отрицательные рёбра не переносят активацию
        cols, rows, data = cols[positive], rows[positive], data[positive]
        out_weight = np.zeros(n, dtype=np.float64)
        np.add.at(out_weight, cols, data)
        valid = out_weight[cols] > 0.0  # защита от вырожденного деления (dangling)
        cols, rows, data = cols[valid], rows[valid], data[valid]
        data = data / out_weight[cols]
        # Дубликаты пар (встречные дуги зеркал симметричных типов, явные
        # двойные связи) суммируем ДО CSR — COO→tocsr делает то же, но
        # edge_flows должен работать по M-значениям, не по сырым дугам:
        # двойная связь A↔B течёт сильнее (осознанно, PPR нормирует по
        # исходящей сумме)
        pair_key = rows * n + cols
        uniq_key, inverse = np.unique(pair_key, return_inverse=True)
        summed = np.zeros(uniq_key.size, dtype=np.float64)
        np.add.at(summed, inverse, data)
        data = summed
        rows = uniq_key // n
        cols = uniq_key % n
        self._matrix = coo_matrix(
            (data, (rows, cols)), shape=(n, n), dtype=np.float64
        ).tocsr()
        self._ids: list[str] = [""] * n
        for node_id, idx in index.items():
            self._ids[idx] = node_id
        # нормированные рёбра (M-значения) для edge_flows: поток ребра
        # считается поверх посчитанного ранга, без повторных итераций
        self._src_idx = cols
        self._tgt_idx = rows
        self._m_vals = data
        self._rank: np.ndarray | None = None

    @property
    def node_count(self) -> int:
        return len(self._index)

    def spread(
        self,
        seed_ids: list[str],
        iterations: int = 25,
        top_k: int | None = None,
        ensure_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Активация от seed-узлов: топ-K [(granule_id, score)] по убыванию.

        e равномерно по seed (мульти-старт, Ф2-PPR-04). ensure_ids — узлы,
        гарантированно присутствующие в выдаче (контракт Ф2-PPR-05: стартовый
        узел включён): не попавший в топ-K ensure-узел заменяет последний
        элемент выдачи, длина остаётся ровно K. Несуществующий seed — пустой
        результат (тихий no-op, не исключение).
        """
        seeds = [self._index[s] for s in seed_ids if s in self._index]
        if not seeds:
            return []
        e = np.zeros(self.node_count, dtype=np.float64)
        e[seeds] = 1.0 / len(seeds)
        rank = e.copy()
        for _ in range(iterations):
            rank = self.damping * (self._matrix @ rank) + (1.0 - self.damping) * e
        self._rank = rank  # финальный ранг — база edge_flows (вердикт Эны 23.09)
        k = self.node_count if top_k is None else min(top_k, self.node_count)
        # частичная сортировка: топ-K за O(n), полный порядок не нужен
        top = np.argpartition(rank, -k)[-k:] if k < self.node_count else np.arange(self.node_count)
        top = list(top[np.argsort(-rank[top], kind="stable")])
        for node_id in ensure_ids or ():
            idx = self._index.get(node_id)
            if idx is not None and idx not in top:
                top[-1] = idx  # старт вытесняет хвост выдачи, длина = K
        top = sorted(top, key=lambda i: -rank[i])
        return [(self._ids[i], round(float(rank[i]), 6)) for i in top]

    def edge_flows(self, node_ids: Iterable[str]) -> list[tuple[str, str, float]]:
        """Потоки рёбер на финальном ранге последнего spread() (вердикт
        Эны 23.09): flow(src→tgt) = r[src] × M[tgt,src] — сколько активации
        ребро перенесло, «проводимость» выдачи.

        Одна поэлементная операция поверх сохранённого ранга — итерации не
        пересчитываются. Возвращаются только рёбра с обоими концами в
        node_ids (проводники внутри выдачи); ранг не извлекался — ValueError.
        """
        if self._rank is None:
            raise ValueError("edge_flows requires a prior spread() call")
        in_top = np.zeros(self.node_count, dtype=bool)
        for node_id in node_ids:
            idx = self._index.get(node_id)
            if idx is not None:
                in_top[idx] = True
        picked = np.nonzero(in_top[self._src_idx] & in_top[self._tgt_idx])[0]
        flows = self._rank[self._src_idx[picked]] * self._m_vals[picked]
        return [
            (self._ids[self._src_idx[i]], self._ids[self._tgt_idx[i]], float(flow))
            for i, flow in zip(picked, flows)
        ]
