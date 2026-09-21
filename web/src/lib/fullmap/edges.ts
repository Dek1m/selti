// Edge visibility selection for the full map (фидбек Мастера, итерация 2):
// рёбра — тонкая графика между звёздами, не паутина. Видимых рёбер максимум
// EDGE_VISIBLE_CAP; приоритет «weight × важность концов», contradicts и
// supersedes пробивают всегда; служебный co_occurrence-слой (related_to с
// weight < 1) скрыт по умолчанию. Чистая функция — unit-testable.

/** Жёсткий кап видимых рёбер полного графа (не опция). */
export const EDGE_VISIBLE_CAP = 1200;

/** Служебные связи: co_occurrence-слой related_to с весом ниже единицы. */
export function isAuxiliaryEdge(typeName: string, weight: number): boolean {
  return typeName === "related_to" && weight < 1;
}

export interface EdgeSelectOptions {
  /** тумблер «служебные связи» — off по умолчанию */
  showAuxiliary: boolean;
  cap: number;
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

  for (let e = 0; e < m; e++) {
    const src = edgeData[e * 3];
    const tgt = edgeData[e * 3 + 1];
    if (!nodeVisible[src] && !nodeVisible[tgt]) continue;
    const weight = edgeWeights[e];
    const typeName = edgeTypeNames[edgeData[e * 3 + 2]] ?? "";
    if (!options.showAuxiliary && isAuxiliaryEdge(typeName, weight)) continue;

    // спец-рёбра всегда пробивают кап; остальное — weight × важность концов
    let score = weight * (nodeImportance[src] + nodeImportance[tgt]);
    if (typeName === "contradicts" || typeName === "supersedes") score += 1e6;

    candIdx.push(e);
    candScore.push(score);
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
