// Edge visibility selection for the full map (фидбек Мастера, итерация 2):
// рёбра — тонкая графика между звёздами, не паутина. Видимых рёбер максимум
// EDGE_VISIBLE_CAP; приоритет «weight × важность концов», contradicts и
// supersedes пробивают всегда; служебный co_occurrence-слой (related_to с
// weight < 1) скрыт по умолчанию. Чистая функция — unit-testable.

/** Жёсткий кап видимых рёбер полного графа (не опция). */
export const EDGE_VISIBLE_CAP = 700;

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

export interface VisibleNodesResult {
  visible: Uint8Array;
  count: number;
}

/**
 * Связный отбор узлов (финал): экран собирается «молекулами». Сначала
 * seeds — самые яркие/близкие кандидаты; вокруг каждого seed волна
 * собирает clusterSize узлов, приоритет «связей с молекулой × 3 +
 * importance». Внутримолекулярные стержни дают плотные связные группы —
 * рёбер both-ends становятся сотни, обрубков нет.
 */
export function selectVisibleNodes(
  candidateIndices: Int32Array,
  candidateScores: Float32Array,
  adjOffsets: Int32Array,
  adjList: Int32Array,
  nodeImportance: Float32Array,
  options: { seedCount: number; clusterSize: number; cap: number },
): VisibleNodesResult {
  const total = candidateIndices.length;
  const visible = new Uint8Array(adjOffsets.length - 1);
  const order = Array.from({ length: total }, (_, k) => k).sort(
    (a, b) => candidateScores[b] - candidateScores[a],
  );

  const scoreOf = (node: number, molecule: Uint8Array) => {
    let links = 0;
    for (let j = adjOffsets[node]; j < adjOffsets[node + 1]; j++) {
      if (molecule[adjList[j]]) links++;
    }
    return links * 3 + nodeImportance[node];
  };

  let taken = 0;
  const seedCount = Math.max(1, options.seedCount);
  const clusterSize = Math.max(1, options.clusterSize);

  for (let s = 0; s < seedCount && taken < options.cap; s++) {
    const seed = candidateIndices[order[s]];
    if (!seed || visible[seed]) continue;
    visible[seed] = 1;
    taken++;

    // волна вокруг seed: добираем clusterSize-1 узлов по связям с молекулой
    const molecule = new Uint8Array(visible.length);
    molecule[seed] = 1;
    let size = 1;
    let frontier = [seed];
    while (size < clusterSize && frontier.length > 0) {
      const next = new Set<number>();
      for (const node of frontier) {
        for (let j = adjOffsets[node]; j < adjOffsets[node + 1]; j++) {
          const nb = adjList[j];
          if (!visible[nb] && !molecule[nb]) next.add(nb);
        }
      }
      if (next.size === 0) break;
      const ranked = [...next].sort((a, b2) => scoreOf(b2, molecule) - scoreOf(a, molecule));
      const take = Math.min(ranked.length, clusterSize - size);
      for (let k = 0; k < take; k++) {
        const node = ranked[k];
        molecule[node] = 1;
        visible[node] = 1;
        taken++;
        size++;
      }
      frontier = ranked.slice(0, take);
    }
  }

  // добор остатка капа одиночными яркими звёздами (без связей — не мешает)
  for (let k = 0; k < total && taken < options.cap; k++) {
    const node = candidateIndices[order[k]];
    if (!visible[node]) {
      visible[node] = 1;
      taken++;
    }
  }

  return { visible, count: taken };
}

/**
 * Выбирает рисуемые рёбра: ОБА конца в culled draw-списке (фикса
 * «обрубков»), служебные фильтруются тумблером, остальные сортируются по
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
    if (!nodeVisible[src] || !nodeVisible[tgt]) continue;
    if (src === tgt) continue;
    bothVisible++;
    const weight = edgeWeights[e];
    const typeName = edgeTypeNames[edgeData[e * 3 + 2]] ?? "";
    if (!options.showAuxiliary && isAuxiliaryEdge(typeName, weight)) continue;

    let score = weight * (nodeImportance[src] + nodeImportance[tgt]);
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
