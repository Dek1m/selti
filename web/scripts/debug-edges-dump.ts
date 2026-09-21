// Debug на реальном дампе (не прод-код): packSnapshot → камера →
// nodeVisible → selectVisibleEdges. Отвечает: candidates/drawn/bothVisible,
// и эмулирует математику ribbon-вершин (экранные координаты квадов).

import { readFileSync } from "node:fs";
import { packSnapshot } from "../src/lib/fullmap/pack";
import { selectVisibleEdges, selectVisibleNodes } from "../src/lib/fullmap/edges";
import type { RawMapSnapshot } from "../src/lib/fullmap/types";

const raw = JSON.parse(readFileSync("E:/tmp/selti-map-dump.json", "utf8")) as RawMapSnapshot;
const packed = packSnapshot(raw, true);
console.log(`nodes ${packed.nodeCount}, edges ${packed.edgeCount}, clusters ${packed.clusters.length}`);
const boundOk = packed.clusters.every((c) => c.index >= 0 && c.index < packed.clusters.length);
console.log("cluster slots compact:", boundOk);

// камера как в сцене: (0, 900, 2600) → target (0,0,0), fov 55
const camera = { pos: [0, 900, 2600] };
const fovY = (55 * Math.PI) / 180;
const aspect = 16 / 9;
const margin = 1.15;
const maxDistSq = 2200 * 2200;

// view-матрица: camera смотрит в origin (упрощённый lookAt)
import { Matrix4, Vector3 } from "three";
const eye = new Vector3(...camera.pos);
const target = new Vector3(0, 0, 0);
const view = new Matrix4().lookAt(eye, target, new Vector3(0, 1, 0)).setPosition(eye).invert();
const proj = new Matrix4().makePerspective(-aspect * Math.tan(fovY / 2), aspect * Math.tan(fovY / 2), Math.tan(fovY / 2), -Math.tan(fovY / 2), 2, 20000);
const vp = new Matrix4().multiplyMatrices(proj, view);
const e = vp.elements;

// 1) фрустум-проход как rebuildVisibleNodes (без score-сортировки — берём
// честно топ-280 по importance*3 + nearness, как в сцене)
const n = packed.nodeCount;
const cand: Array<{ i: number; score: number }> = [];
const positions = packed.nodePositions;
for (let i = 0; i < n; i++) {
  const x = positions[i * 3];
  const y = positions[i * 3 + 1];
  const z = positions[i * 3 + 2];
  const dx = x - eye.x;
  const dy = y - eye.y;
  const dz = z - eye.z;
  if (dx * dx + dy * dy + dz * dz > maxDistSq) continue;
  const w = e[3] * x + e[7] * y + e[11] * z + e[15];
  if (w <= 0) continue;
  const nx = (e[0] * x + e[4] * y + e[8] * z + e[12]) / w;
  const ny = (e[1] * x + e[5] * y + e[9] * z + e[13]) / w;
  if (nx < -margin || nx > margin || ny < -margin || ny > margin) continue;
  const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
  cand.push({ i, score: packed.nodeMeta[i * 4 + 2] * 3 + (1 - dist / 2200) * 2 });
}
console.log("фрустум-кандидатов узлов:", cand.length);
const importance = new Float32Array(n);
for (let i = 0; i < n; i++) importance[i] = packed.nodeMeta[i * 4 + 2];

cand.sort((a, b) => b.score - a.score);
const candIdx = Int32Array.from(cand.map((c) => c.i));
const candScore = Float32Array.from(cand.map((c) => c.score));
const selection = selectVisibleNodes(
  candIdx,
  candScore,
  packed.adjOffsets,
  packed.adjList,
  importance,
  140,
  280,
);
const visible = selection.visible;
console.log("nodeVisible (связный greedy):", selection.count);

// 2) отбор рёбер как в сцене (aux off)
const stats = { candidates: 0, bothVisible: 0, drawn: 0 };
const picked = selectVisibleEdges(packed.edgeData, packed.edgeWeights, packed.edgeTypes, importance, visible, {
  showAuxiliary: false,
  cap: 1200,
  stats,
});
console.log("edges stats:", stats);
console.log("picked len:", picked.length, "первый десяток:", [...picked.slice(0, 10)]);

// 3) эмуляция ribbon-вершин: экранные квадраты первых 5 рёбер
const W = 1920;
const H = 1080;
const halfWidth = 1.0; // px, полутолщина
let bad = 0;
for (let k = 0; k < Math.min(5, picked.length); k++) {
  const edge = picked[k];
  const srcNode = packed.edgeData[edge * 3];
  const tgtNode = packed.edgeData[edge * 3 + 1];
  const projPoint = (i: number) => {
    const x = positions[i * 3];
    const y = positions[i * 3 + 1];
    const z = positions[i * 3 + 2];
    const w = e[3] * x + e[7] * y + e[11] * z + e[15];
    return {
      x: ((e[0] * x + e[4] * y + e[8] * z + e[12]) / w + 1) / 2 * W,
      y: (1 - (e[1] * x + e[5] * y + e[9] * z + e[13]) / w) / 2 * H,
      w,
    };
  };
  const pa = projPoint(srcNode);
  const pb = projPoint(tgtNode);
  const len = Math.hypot(pb.x - pa.x, pb.y - pa.y);
  const onScreen =
    Number.isFinite(pa.x) && pa.x > 0 && pa.x < W && pa.y > 0 && pa.y < H;
  if (!onScreen) bad++;
  console.log(
    `edge ${edge}: A(${pa.x.toFixed(0)},${pa.y.toFixed(0)},w=${pa.w.toFixed(0)}) B(${pb.x.toFixed(0)},${pb.y.toFixed(0)},w=${pb.w.toFixed(0)}) len ${len.toFixed(0)}px visibleA=${visible[srcNode]} visibleB=${visible[tgtNode]} ${onScreen ? "в экране" : "ВНЕ ЭКРАНА"}`,
  );
}
console.log(bad > 0 ? `⚠ ${bad} из 5 вне экрана` : "все проверенные квады в экране");
