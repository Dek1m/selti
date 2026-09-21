// Регрессия на РЕАЛЬНОМ дампе прода (E:/tmp/selti-map-dump.json — кладёт Рэй;
// тест скиппается, если файла нет). Ловит класс багов, который мини-фикстуры
// не видели: gapped cluster ids, старый шейп {i, m}, связность капа 280.
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { Matrix4, Vector3 } from "three";
import { selectVisibleEdges, selectVisibleNodes } from "./edges";
import { packSnapshot } from "./pack";
import type { RawMapSnapshot } from "./types";

const DUMP = process.env.SELTI_MAP_DUMP ?? "E:/tmp/selti-map-dump.json";
const hasDump = existsSync(DUMP);

describe.skipIf(!hasDump)("real prod dump (15k nodes / 90k edges)", () => {
  const raw = JSON.parse(readFileSync(DUMP, "utf8")) as RawMapSnapshot;
  const packed = packSnapshot(raw, true);

  it("packs the prod shape: clusters normalize from {i, m} to slots", () => {
    expect(packed.nodeCount).toBeGreaterThan(10_000);
    expect(packed.edgeCount).toBeGreaterThan(80_000);
    expect(packed.clusters.length).toBeGreaterThan(1000);
    // узлы с кластером обязаны указывать в валидный слот (раньше утекали в -1)
    let clustered = 0;
    for (let i = 0; i < packed.nodeCount; i++) {
      const slot = packed.nodeMeta[i * 4 + 1] | 0;
      if (slot >= 0) {
        clustered++;
        expect(slot).toBeLessThan(packed.clusters.length);
      }
    }
    expect(clustered).toBeGreaterThan(5_000);
    // центроиды не сгнили в нули
    const real = packed.clusters.filter((c) => c.centroid.some((v) => v !== 0));
    expect(real.length).toBeGreaterThan(packed.clusters.length * 0.5);
  });

  it("connected node selection → edge cap filled with both-ends-visible rods", () => {
    // типовая камера: (0, 900, 2600) → центр куба, fov 55, margin 1.15
    const eye = new Vector3(0, 900, 2600);
    const view = new Matrix4().lookAt(eye, new Vector3(0, 0, 0), new Vector3(0, 1, 0)).setPosition(eye).invert();
    const t = Math.tan((55 * Math.PI) / 180 / 2);
    const aspect = 16 / 9;
    const proj = new Matrix4().makePerspective(-aspect * t, aspect * t, t, -t, 2, 20000);
    const vp = new Matrix4().multiplyMatrices(proj, view);
    const e = vp.elements;

    const n = packed.nodeCount;
    const positions = packed.nodePositions;
    const candIdx: number[] = [];
    const candScore: number[] = [];
    const margin = 1.15;
    const maxDistSq = 2200 * 2200;
    for (let i = 0; i < n; i++) {
      const x = positions[i * 3];
      const y = positions[i * 3 + 1];
      const z = positions[i * 3 + 2];
      const dx = x - eye.x;
      const dy = y - eye.y;
      const dz = z - eye.z;
      const distSq = dx * dx + dy * dy + dz * dz;
      if (distSq > maxDistSq) continue;
      const w = e[3] * x + e[7] * y + e[11] * z + e[15];
      if (w <= 0) continue;
      const nx = (e[0] * x + e[4] * y + e[8] * z + e[12]) / w;
      const ny = (e[1] * x + e[5] * y + e[9] * z + e[13]) / w;
      if (nx < -margin || nx > margin || ny < -margin || ny > margin) continue;
      candIdx.push(i);
      candScore.push(packed.nodeMeta[i * 4 + 2] * 3 + (1 - Math.sqrt(distSq) / 2200) * 2);
    }

    const importance = new Float32Array(n);
    for (let i = 0; i < n; i++) importance[i] = packed.nodeMeta[i * 4 + 2];
    const selection = selectVisibleNodes(
      Int32Array.from(candIdx),
      Float32Array.from(candScore),
      packed.adjOffsets,
      packed.adjList,
      importance,
      140,
      280,
    );
    expect(selection.count).toBe(280);

    const stats = { candidates: 0, bothVisible: 0, drawn: 0 };
    const picked = selectVisibleEdges(
      packed.edgeData,
      packed.edgeWeights,
      packed.edgeTypes,
      importance,
      selection.visible,
      { showAuxiliary: false, cap: 1200, stats },
    );
    // требование Мастера: >1000 рёбер в капе
    expect(picked.length).toBeGreaterThan(1000);
    // и большинство из них — стержни МЕЖДУ видимыми атомами (связный кадр)
    expect(stats.bothVisible).toBeGreaterThan(500);
  });
});
