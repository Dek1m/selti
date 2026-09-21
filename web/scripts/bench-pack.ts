// Debug bench (не прод-код, запускать вручную: npx tsx scripts/bench-pack.ts)
// Воспроизводит прод-объём снапшота (15 071 узлов / 84 742 рёбра / 1753
// кластеров) и замеряет каждую фазу цепочки после «98%» в HUD:
// JSON.parse → packSnapshot (внутри: CSR, кластеры) → buildUuidIndex →
// эмуляция scene.load (buildFullEdges / buildClusterLevel / gate-агрегация).
// Флаг --gapped-id запускает вариант с НЕплотными cluster id (реальная
// таблица кластеров может иметь id с дырами — мок так не умеет).

import { packSnapshot, unpackNodeString } from "../src/lib/fullmap/pack";
import { bfsLevels } from "../src/lib/fullmap/bfs";
import type { RawMapSnapshot } from "../src/lib/fullmap/types";

const N = 15_071;
const M = 84_742;
const ET_COUNT = 28;
const C_COUNT = 1753;

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

const WORDS = "решение миграция деплой кластер гранула связь карта шейдер поиск память сервер кеш лок ключ контракт тест сборка релиз".split(" ");

function fakeText(rnd: () => number, minWords: number, maxWords: number): string {
  const count = minWords + Math.floor(rnd() * (maxWords - minWords));
  const parts: string[] = [];
  for (let i = 0; i < count; i++) parts.push(WORDS[Math.floor(rnd() * WORDS.length)]);
  return parts.join(" ");
}

function buildReal(order: "dense" | "gapped"): RawMapSnapshot {
  const rnd = mulberry32(42);
  // gapped: id-шники как из БД — с дырами, до 40× больше count
  const clusterId = (slot: number) => (order === "dense" ? slot : slot * 37 + 11);

  const nodes: RawMapSnapshot["nodes"] = [];
  for (let i = 0; i < N; i++) {
    const slot = Math.floor(rnd() * C_COUNT);
    nodes.push([
      crypto.randomUUID(),
      `${fakeText(rnd, 2, 6)} #${i}`.slice(0, 80),
      fakeText(rnd, 20, 30).slice(0, 180),
      Math.floor(rnd() * 5),
      clusterId(slot),
      1 + Math.floor(rnd() * 5),
      rnd() < 0.03 ? 1 : 0,
      Math.round((rnd() - 0.5) * 2000),
      Math.round((rnd() - 0.5) * 2000),
      Math.round((rnd() - 0.5) * 2000),
    ]);
  }

  const edges: RawMapSnapshot["edges"] = [];
  for (let e = 0; e < M; e++) {
    const src = Math.floor(rnd() * N);
    const tgt = Math.floor(rnd() * N);
    edges.push([src, tgt, Math.floor(rnd() * ET_COUNT), Math.round((1 + rnd() * 2) * 100) / 100]);
  }

  const clusters = Array.from({ length: C_COUNT }, (_, slot) => ({
    id: clusterId(slot),
    ns: Math.floor(rnd() * 5),
    size: 1 + Math.floor(rnd() * 40),
  }));

  return {
    v: "bench",
    ns: ["user_facts", "project_meta", "code_knowledge", "dialogue_insights", "infrastructure"],
    et: Array.from({ length: ET_COUNT }, (_, i) => `link_${i}`),
    clusters,
    nodes,
    edges,
  };
}

const fmt = (ms: number) => `${ms.toFixed(0)}ms`;
const step = (label: string, fn: () => void): void => {
  const t0 = performance.now();
  fn();
  const ms = performance.now() - t0;
  console.log(`  ${label.padEnd(46)} ${fmt(ms)}${ms > 2000 ? "  ← ЗАВИСАНИЕ" : ""}`);
};

const order = process.argv.includes("--gapped-id") ? "gapped" : "dense";
console.log(`\n=== bench: прод-объём (${order} cluster ids) ===`);

const raw = buildReal(order);

// network+parse прокси: stringify как передача, parse как на воркере
let parsed: RawMapSnapshot | null = null;
step("JSON stringify (прокси передачи)", () => JSON.stringify(raw));
step("JSON.parse (воркер)", () => {
  parsed = JSON.parse(JSON.stringify(raw)) as RawMapSnapshot;
});
const snapshot = parsed!;

let packed: ReturnType<typeof packSnapshot> | null = null;
step("packSnapshot (typed arrays + CSR + кластеры)", () => {
  packed = packSnapshot(snapshot, true);
});
const pack = packed!;

step("bfsLevels от узла 0", () => bfsLevels(pack, [0]));

// FullMapLayer "done"-обработчик: индекс uuid → узел
step("buildUuidIndex (15k×unpackNodeString)", () => {
  const index = new Map<string, number>();
  for (let i = 0; i < pack.nodeCount; i++) index.set(unpackNodeString(pack, i, 0), i);
  if (index.size !== pack.nodeCount) throw new Error("uuid collision");
});

// ── эмуляция scene.load(): те же циклы без WebGL ──
step("scene.load: node attrs (15k)", () => {
  const colors = new Float32Array(pack.nodeCount * 3);
  for (let i = 0; i < pack.nodeCount; i++) {
    const nsIdx = pack.nodeMeta[i * 4] | 0;
    colors[i * 3] = (nsIdx % 5) / 5;
  }
});

step("buildFullEdges цикл (85k, литералы как в scene)", () => {
  const positions = new Float32Array(pack.edgeCount * 6);
  const colors = new Float32Array(pack.edgeCount * 6);
  for (let e = 0; e < pack.edgeCount; e++) {
    const src = pack.edgeData[e * 3];
    const tgt = pack.edgeData[e * 3 + 1];
    positions.set([pack.nodePositions[src * 3], pack.nodePositions[src * 3 + 1], pack.nodePositions[src * 3 + 2]], e * 6);
    positions.set([pack.nodePositions[tgt * 3], pack.nodePositions[tgt * 3 + 1], pack.nodePositions[tgt * 3 + 2]], e * 6 + 3);
    const rgb = [0.3, 0.5, 0.7];
    colors.set(rgb, e * 6);
    colors.set(rgb, e * 6 + 3);
  }
});

step("buildClusterLevel: радиусы (15k + Map.get)", () => {
  const c = pack.clusters.length;
  const byIndex = new Map(pack.clusters.map((cl) => [cl.index, cl]));
  const accDist = new Float64Array(c);
  const accCnt = new Float64Array(c);
  let skipped = 0;
  for (let i = 0; i < pack.nodeCount; i++) {
    const clusterIdx = pack.nodeMeta[i * 4 + 1] | 0;
    const centroid = byIndex.get(clusterIdx)?.centroid;
    if (!centroid) {
      skipped++;
      continue;
    }
    const dx = pack.nodePositions[i * 3] - centroid[0];
    const dy = pack.nodePositions[i * 3 + 1] - centroid[1];
    const dz = pack.nodePositions[i * 3 + 2] - centroid[2];
    accDist[clusterIdx] += Math.sqrt(dx * dx + dy * dy + dz * dz);
    accCnt[clusterIdx] += 1;
  }
  if (skipped > 0) console.log(`    ⚠ узлов вне карты кластеров (byIndex.get → undefined): ${skipped}`);
});

step("gate-агрегация (85k) + РАЗБОР ЦЕНТРОИДОВ как в scene", () => {
  const c = pack.clusters.length;
  const byIndex = new Map(pack.clusters.map((cl) => [cl.index, cl]));
  const gateWeight = new Map<number, number>();
  for (let e = 0; e < pack.edgeCount; e++) {
    const srcCluster = pack.nodeMeta[pack.edgeData[e * 3] * 4 + 1] | 0;
    const tgtCluster = pack.nodeMeta[pack.edgeData[e * 3 + 1] * 4 + 1] | 0;
    if (srcCluster < 0 || tgtCluster < 0 || srcCluster === tgtCluster) continue;
    const a = Math.min(srcCluster, tgtCluster);
    const b = Math.max(srcCluster, tgtCluster);
    const key = a * c + b;
    gateWeight.set(key, (gateWeight.get(key) ?? 0) + pack.edgeWeights[e]);
  }
  // тот же код, что в buildClusterLevel: this.clusterByIndex.get(a)!.centroid
  let missing = 0;
  for (const key of gateWeight.keys()) {
    const a = Math.floor(key / c);
    const b = key % c;
    const ca = byIndex.get(a);
    const cb = byIndex.get(b);
    if (!ca || !cb) {
      missing++;
      continue;
    }
    void ca.centroid[0];
    void cb.centroid[2];
  }
  if (missing > 0) {
    console.log(`    ✖ ГЕЙТОВ С ОТСУТСТВУЮЩИМ КЛАСТЕРОМ: ${missing} из ${gateWeight.size}`);
    console.log("    ✖ в scene.ts это this.clusterByIndex.get(a)!.centroid → TypeError →");
    console.log("    ✖ onmessage main падает, setProgress(null) не вызывается → «98%» навсегда");
    process.exitCode = 2;
  } else {
    console.log(`    гейтов: ${gateWeight.size}, все кластеры найдены`);
  }
});

console.log("");
