// Deterministic mock snapshot (PLAN_FULL_MAP_3D §3 contract) — lets the M3
// full-map UI run before Сона ships `GET /api/map/full`. Same shape as the
// real wire format, so the client code path is identical: the worker calls
// either fetchSnapshot() or buildMockSnapshot() and packs with packSnapshot().

import type { RawMapSnapshot } from "./types";

/** mulberry32 — the repo's canonical seeded PRNG. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const MOCK_NODE_COUNT = 15_000;
export const MOCK_EDGE_COUNT = 30_000;

const NAMESPACES = ["user_facts", "project_meta", "code_knowledge", "dialogue_insights", "infrastructure"];

const EDGE_TYPES = [
  "related_to",
  "references",
  "follows",
  "supersedes",
  "implements",
  "depends_on",
  "contradicts",
  "supports",
  "derived_from",
  "runs_on",
];

const ENTITY_KINDS = [
  "adr",
  "module",
  "deploy",
  "migration",
  "insight",
  "fact",
  "config",
  "container",
  "endpoint",
  "cluster",
];

const LABEL_TOPICS = [
  "репозиторий selti",
  "веб-морда",
  "звёздная карта",
  "миграция 022",
  "кластеры Level 2",
  "reconciler связей",
  "Celery beat",
  "Redis кеш",
  "pgbouncer пул",
  "деплой albedo",
  "BFS глубина",
  "glass-градиент",
  "WebGL шейдеры",
  "тултип preview",
  "дедупликация",
  "supersession цепочка",
  "DrL раскладка",
  "бэкенд контракты",
  "поиск-сегмент",
  "оболочки регионов",
];

const PREVIEW_PARTS = [
  "Решение зафиксировано после ревью: считаем",
  "Гранула подтверждает, что",
  "Контракт требует, чтобы",
  "Архитектурный исход: принимаем",
  "Проверено на живом корпусе:",
  "Инсайт сессии — договорились, что",
  "Факт инфраструктуры:",
  "ADR утверждён Мастером:",
  "Тесты зелёные при условии, что",
  "Мониторинг показывает, что",
];

const PREVIEW_TAILS = [
  "иначе карта теряет глубину",
  "и это единственный согласованный путь",
  "пока reconciler не пометит связку резолвленной",
  "с повторной сборкой снапшота по dirty-флагу",
  "без изменений в существующих эндпоинтах",
  "с фиксированным разлётом и нормировкой bbox",
  "и обязательной приёмкой на интегрированной графике",
  "пока не истечёт срок кеша в Redis",
  "и только потом открывать режим для всех",
  "что подтверждено метриками за неделю",
];

// long-winded codas: with ~45% chance a preview grows past 180 chars, so the
// word-boundary truncation branch stays covered on every mock build
const PREVIEW_CODAS = [
  "Дополнительно зафиксировали, что повторная сборка не должна трогать координаты уже разложенных узлов, иначе карта будет перетасовываться при каждом проходе reconciler и терять привычные ориентиры.",
  "Приёмка включает прогон на интегрированной графике: если fill-rate глоу просядет ниже комфортных шестидесяти кадров, включаем LOD-размер спрайтов и distance-fade агрессивнее, не спрашивая лишний раз.",
  "Открытый вопрос к контракту: список типов связей едет в снапшоте отдельным columnar-массивом, чтобы клиент не хардкодил индексы и новые типы появлялись на карте без релиза веб-морды.",
];

const CLUSTER_COUNT = 42;

/** Simple hash for a stable fake version string. */
function fnv1a(text: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

/**
 * Word-boundary truncation mirroring the server contract (§3, решение
 * Мастера 1): cut at the last space before the limit, append "…". Texts
 * that fit pass through untouched.
 */
export function truncatePreview(text: string, limit = 180): string {
  if (text.length <= limit) return text;
  const cut = text.lastIndexOf(" ", limit - 1);
  return cut > 0 ? `${text.slice(0, cut)}…` : `${text.slice(0, limit)}…`;
}

/**
 * Build the mock universe: 42 "star systems" (clusters) seeded on a sphere
 * inside the [-1000, 1000]³ bbox cube, gaussian member clouds around each
 * centroid, intra-cluster + hub edges. Deterministic — same bytes each run.
 */
export function buildMockSnapshot(
  nodeCount = MOCK_NODE_COUNT,
  edgeCount = MOCK_EDGE_COUNT,
): RawMapSnapshot {
  const rnd = mulberry32(20260922);

  // gaussian pair via Box-Muller
  const gauss = () => {
    const u = Math.max(rnd(), 1e-9);
    const v = rnd();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  };

  // ── clusters: centers on a jittered sphere, radius inside the cube ──
  const centers: Array<[number, number, number]> = [];
  for (let c = 0; c < CLUSTER_COUNT; c++) {
    const theta = rnd() * Math.PI * 2;
    const phi = Math.acos(2 * rnd() - 1);
    const r = 420 + rnd() * 420;
    centers.push([r * Math.sin(phi) * Math.cos(theta), r * Math.cos(phi) * 0.72, r * Math.sin(phi) * Math.sin(theta)]);
  }

  // ── nodes ──
  const nodes: RawMapSnapshot["nodes"] = [];
  const nodeCluster: number[] = [];
  for (let i = 0; i < nodeCount; i++) {
    const clusterIdx = i % CLUSTER_COUNT;
    const [cx, cy, cz] = centers[clusterIdx];
    const spread = 60 + (clusterIdx % 5) * 22;
    const x = cx + gauss() * spread;
    const y = cy + gauss() * spread;
    const z = cz + gauss() * spread;
    const nsIdx = (clusterIdx + (i % 3)) % NAMESPACES.length;
    const importance = 1 + Math.floor(rnd() * 5);
    // final contract: bit0 = frozen only; superseded/retracted never ship
    const flags = rnd() < 0.04 ? 1 : 0;
    const kind = ENTITY_KINDS[Math.floor(rnd() * ENTITY_KINDS.length)];
    const topic = LABEL_TOPICS[Math.floor(rnd() * LABEL_TOPICS.length)];
    const name = `${kind}/${topic} · ${i.toString(36).padStart(4, "0")}`.slice(0, 80);
    const preview =
      `${PREVIEW_PARTS[Math.floor(rnd() * PREVIEW_PARTS.length)]} ` +
      `${topic} ${PREVIEW_TAILS[Math.floor(rnd() * PREVIEW_TAILS.length)]}.` +
      (rnd() < 0.45 ? ` ${PREVIEW_CODAS[Math.floor(rnd() * PREVIEW_CODAS.length)]}` : "");
    const clamped = truncatePreview(preview);
    // wire contract int-rounds every numeric field
    nodeCluster.push(clusterIdx);
    nodes.push([
      fnv1a(`node-${i}`) + fnv1a(`node2-${i}`),
      name,
      clamped,
      nsIdx,
      clusterIdx,
      importance,
      flags,
      Math.round(x),
      Math.round(y),
      Math.round(z),
    ]);
  }

  // ── edges: intra-cluster gravity + inter-cluster hubs ──
  // bucket nodes per cluster for cheap local sampling
  const byCluster: number[][] = Array.from({ length: CLUSTER_COUNT }, () => []);
  for (let i = 0; i < nodeCount; i++) byCluster[nodeCluster[i]].push(i);

  const edges: RawMapSnapshot["edges"] = [];
  const seen = new Set<number>();
  const pushEdge = (src: number, tgt: number, typeIdx: number, weight: number) => {
    if (src === tgt) return;
    const key = src < tgt ? src * nodeCount + tgt : tgt * nodeCount + src;
    if (seen.has(key)) return;
    seen.add(key);
    edges.push([src, tgt, typeIdx, weight]);
  };

  const typeOf = (name: string) => EDGE_TYPES.indexOf(name);
  // float weights like the real reconciler emits (1.0–3.0, two decimals)
  const floatWeight = () => Math.round((1 + rnd() * 2) * 100) / 100;
  let guard = 0;
  while (edges.length < Math.floor(edgeCount * 0.82) && guard < edgeCount * 30) {
    guard++;
    const clusterIdx = Math.floor(rnd() * CLUSTER_COUNT);
    const bucket = byCluster[clusterIdx];
    const src = bucket[Math.floor(rnd() * bucket.length)];
    const tgt = bucket[Math.floor(rnd() * bucket.length)];
    const roll = rnd();
    const typeIdx = roll < 0.08 ? typeOf("supersedes") : roll < 0.11 ? typeOf("contradicts") : Math.floor(rnd() * EDGE_TYPES.length);
    pushEdge(src, tgt, typeIdx, floatWeight());
  }
  while (edges.length < edgeCount && guard < edgeCount * 40) {
    guard++;
    // hub edges: pick two systems, connect their brightest members
    const a = Math.floor(rnd() * CLUSTER_COUNT);
    let b = Math.floor(rnd() * CLUSTER_COUNT);
    if (b === a) b = (b + 1) % CLUSTER_COUNT;
    const bucketA = byCluster[a];
    const bucketB = byCluster[b];
    if (bucketA.length === 0 || bucketB.length === 0) continue;
    const pickBright = (bucket: number[]) => {
      let best = bucket[0];
      for (let k = 0; k < 6; k++) {
        const candidate = bucket[Math.floor(rnd() * bucket.length)];
        if (nodes[candidate][5] > nodes[best][5]) best = candidate;
      }
      return best;
    };
    pushEdge(pickBright(bucketA), pickBright(bucketB), typeOf("related_to"), floatWeight());
  }

  // final contract shape: {id, ns, size, ...} — label не входит, ns для цвета
  const clusters = centers.map((_, idx) => ({
    id: idx,
    ns: idx % NAMESPACES.length,
    size: byCluster[idx].length,
  }));

  return {
    v: `mock-${fnv1a(`${nodeCount}:${edgeCount}:${CLUSTER_COUNT}`)}`,
    ns: NAMESPACES,
    et: EDGE_TYPES,
    clusters,
    nodes,
    edges,
  };
}
