// Edge visibility selection for the full map (фидбек Мастера, итерация 2):
// рёбра — тонкая графика между звёздами, не паутина. Видимых рёбер максимум
// EDGE_VISIBLE_CAP; приоритет «weight × важность концов», contradicts и
// supersedes пробивают всегда; служебный co_occurrence-слой (related_to с
// weight < 1) скрыт по умолчанию. Чистая функция — unit-testable.

/** Жёсткий кап видимых рёбер полного графа (не опция). */
export const EDGE_VISIBLE_CAP = 1200;

export interface VisibleNodesResult {
  visible: Uint8Array;
  count: number;
}

/**
 * Связный отбор узлов (итерация 4): кап 280 «звёзд-одиночек» рвал сеть —
 * из 1837 видимых во фрустуме узлов обе концовки у рёбер сходились лишь
 * 71 раз (дамп 15k/90k). Теперь: 1) seeds = топ seedCount по score;
 * 2) добор до cap — соседи уже выбранных с приоритетом
 * «связей с выбранными × 3 + importance». Кадр собирается в связные
 * «молекулы»: у доборных узлов по построению есть стержень внутрь набора.
 * Чистая функция — unit-testable.
 */
export function selectVisibleNodes(
  candidateIndices: Int32Array,
  candidateScores: Float32Array,
  adjOffsets: Int32Array,
  adjList: Int32Array,
  nodeImportance: Float32Array,
  seedCount: number,
  cap: number,
): VisibleNodesResult {
  const total = candidateIndices.length;
  const drawCount = Math.min(total, cap);
  const order = Array.from({ length: total }, (_, k) => k).sort(
    (a, b) => candidateScores[b] - candidateScores[a],
  );

  const visible = new Uint8Array(adjOffsets.length - 1);
  const chosen: number[] = [];

  const pick = (node: number) => {
    if (visible[node]) return;
    visible[node] = 1;
    chosen.push(node);
  };

  // 1) seeds — самые яркие/близкие
  for (let k = 0; k < Math.min(seedCount, drawCount); k++) {
    pick(candidateIndices[order[k]]);
  }

  // 2) добор: сосед выбранных ценнее одинокой звезды — стержни внутрь набора
  const refillIdx: number[] = [];
  const refillScore: number[] = [];
  const seen = new Set<number>();
  for (const node of chosen) {
    for (let j = adjOffsets[node]; j < adjOffsets[node + 1]; j++) {
      const neighbor = adjList[j];
      if (visible[neighbor] || seen.has(neighbor)) continue;
      seen.add(neighbor);
      let links = 0;
      for (let q = adjOffsets[neighbor]; q < adjOffsets[neighbor + 1]; q++) {
        if (visible[adjList[q]]) links++;
      }
      refillIdx.push(neighbor);
      refillScore.push(links * 3 + nodeImportance[neighbor]);
    }
  }
  const refillOrder = refillIdx.map((_, k) => k).sort((a, b) => refillScore[b] - refillScore[a]);
  const remaining = drawCount - chosen.length;
  for (let k = 0; k < Math.min(remaining, refillOrder.length); k++) {
    pick(refillIdx[refillOrder[k]]);
  }

  // 3) если кап всё ещё не добран — самые яркие оставшиеся кандидаты
  if (chosen.length < drawCount) {
    for (let k = seedCount; k < total && chosen.length < drawCount; k++) {
      pick(candidateIndices[order[k]]);
    }
  }

  return { visible, count: chosen.length };
}

/** Служебные связи: co_occurrence-слой related_to с весом ниже единицы. */
export function isAuxiliaryEdge(typeName: string, weight: number): boolean {
  return typeName === "related_to" && weight < 1;
}

export interface EdgeSelectOptions {
  /** тумблер «служебные связи» — off по умолчанию */
  showAuxiliary: boolean;
  cap: number;
  /** debug-диагностика (?debug=1): заполняется вызывающим, если передан */
  stats?: { candidates: number; bothVisible: number; drawn: number };
}

/**
 * Выбирает рисуемые рёбра: один конец обязан быть в culled draw-списке
 * (nodeVisible), служебные фильтруются тумблером, остальные сортируются по
 * score = weight × (importance[src] + importance[tgt]) со спец-бустом для
 * contradicts/supersedes; возвращает плотный список индексов ≤ cap.
 */
export function selectVisibleEdges(
  edgeData: Int32Array,
  edgeWeights: Float32Array,
  edgeTypeNames: string[],
  nodeImportance: Float32Array,
  nodeVisible: Uint8Array,
  options: EdgeSelectOptions,
): Int32Array {
  const m = edgeWeights.length;
  const candIdx: number[] = [];
  const candScore: number[] = [];
  let bothVisible = 0;

  for (let e = 0; e < m; e++) {
    const src = edgeData[e * 3];
    const tgt = edgeData[e * 3 + 1];
    const srcVisible = !!nodeVisible[src];
    const tgtVisible = !!nodeVisible[tgt];
    if (!srcVisible && !tgtVisible) continue;
    if (srcVisible && tgtVisible) bothVisible++;
    const weight = edgeWeights[e];
    const typeName = edgeTypeNames[edgeData[e * 3 + 2]] ?? "";
    if (!options.showAuxiliary && isAuxiliaryEdge(typeName, weight)) continue;

    // спец-рёбра всегда пробивают кап; остальное — weight × важность концов
    let score = weight * (nodeImportance[src] + nodeImportance[tgt]);
    // нити МЕЖДУ двумя видимыми звёздами приоритетнее «хвостов» за кадром
    if (srcVisible && tgtVisible) score *= 3;
    if (typeName === "contradicts" || typeName === "supersedes") score += 1e6;

    candIdx.push(e);
    candScore.push(score);
  }
  if (options.stats) {
    options.stats.candidates = candIdx.length;
    options.stats.bothVisible = bothVisible;
    options.stats.drawn = Math.min(candIdx.length, options.cap);
  }

  let count = candIdx.length;
  const out = new Int32Array(Math.min(count, options.cap));
  if (count > options.cap) {
    const order = candIdx.map((_, k) => k).sort((a, b) => candScore[b] - candScore[a]);
    for (let k = 0; k < options.cap; k++) out[k] = candIdx[order[k]];
    count = options.cap;
  } else {
    for (let k = 0; k < count; k++) out[k] = candIdx[k];
  }
  return out;
}
