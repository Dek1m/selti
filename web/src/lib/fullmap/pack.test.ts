import { describe, expect, it } from "vitest";
import { packSnapshot, unpackNodeString } from "./pack";
import type { RawMapSnapshot } from "./types";

// ids on the wire are DB ids — deliberately gapped (1, 0 → and 40907 below)
const raw: RawMapSnapshot = {
  v: "snap-1",
  ns: ["code_knowledge", "infrastructure"],
  et: ["related_to", "supersedes"],
  clusters: [
    { id: 1, ns: 0, size: 2, label: "второй" },
    { id: 0, ns: 1, size: 1 },
  ],
  nodes: [
    ["uuid-alpha", "Альфа — первая гранула", "превью альфы про деплой и шейдеры", 0, 0, 5, 0, 10, -20, 30],
    ["uuid-beta", "Бета", null, 1, 1, 2, 1, -5, 8, 2],
    ["uuid-gamma", "Гамма · длинное имя для упаковки строк", "превью гаммы", 0, 1, 1, 0, 1, 2, 3],
  ],
  edges: [
    [0, 1, 0, 2.5],
    [1, 2, 1, 1],
    [0, 1, 0, 3], // duplicate pair for adjacency dedup
    [2, 2, 0, 1.25], // self-loop — excluded from adjacency, kept for render
  ],
};

describe("packSnapshot", () => {
  const packed = packSnapshot(raw, true);

  it("keeps dimensions and metadata", () => {
    expect(packed.version).toBe("snap-1");
    expect(packed.nodeCount).toBe(3);
    expect(packed.edgeCount).toBe(4);
    expect(packed.namespaces).toEqual(["code_knowledge", "infrastructure"]);
    expect(packed.edgeTypes).toEqual(["related_to", "supersedes"]);
  });

  it("round-trips node strings through the UTF-8 blob", () => {
    expect(unpackNodeString(packed, 0, 0)).toBe("uuid-alpha");
    expect(unpackNodeString(packed, 0, 1)).toBe("Альфа — первая гранула");
    expect(unpackNodeString(packed, 0, 2)).toBe("превью альфы про деплой и шейдеры");
    expect(unpackNodeString(packed, 1, 1)).toBe("Бета");
    expect(unpackNodeString(packed, 2, 0)).toBe("uuid-gamma");
  });

  it("drops previews when packed withPreview=false", () => {
    const lean = packSnapshot(raw, false);
    expect(lean.withPreview).toBe(false);
    expect(unpackNodeString(lean, 0, 2)).toBe("");
    expect(unpackNodeString(lean, 0, 1)).toBe("Альфа — первая гранула");
  });

  it("carries node meta and positions", () => {
    // wire clusterIdx 0 → slot 1 (id 0 is the second cluster entry)
    expect([...packed.nodeMeta.slice(0, 4)]).toEqual([0, 1, 5, 0]);
    // узел 1: ns 1, wire clusterIdx 1 → slot 0, flags bit0 = frozen
    expect([...packed.nodeMeta.slice(4, 8)]).toEqual([1, 0, 2, 1]);
    expect([...packed.nodePositions.slice(0, 3)]).toEqual([10, -20, 30]);
  });

  it("keeps edge endpoints int and weights float", () => {
    expect([...packed.edgeData.slice(0, 3)]).toEqual([0, 1, 0]);
    expect(packed.edgeWeights[0]).toBeCloseTo(2.5);
    expect(packed.edgeWeights[3]).toBeCloseTo(1.25);
    expect(packed.edgeWeights).toHaveLength(4);
    expect(packed.edgeData).toHaveLength(4 * 3); // stride 3: src, tgt, type
  });

  it("builds deduped CSR adjacency (self-loops and multi-edges collapse)", () => {
    const neighborsOf = (i: number) => [...packed.adjList.slice(packed.adjOffsets[i], packed.adjOffsets[i + 1])].sort();
    expect(neighborsOf(0)).toEqual([1]);
    expect(neighborsOf(1)).toEqual([0, 2]);
    expect(neighborsOf(2)).toEqual([1]);
    expect(packed.adjOffsets[3]).toBe(packed.adjList.length);
  });

  it("normalizes gapped wire ids to compact slots, keeps raw id", () => {
    // sorted by size desc: slot 0 = wire id 1, slot 1 = wire id 0
    expect(packed.clusters.map((c) => c.index)).toEqual([0, 1]);
    expect(packed.clusters.map((c) => c.id)).toEqual([1, 0]);
    expect(packed.clusters.map((c) => c.members)).toEqual([2, 1]);
    expect(packed.clusters[0].label).toBe("второй");
    expect(packed.clusters[1].label).toBeNull();
    // every nodeMeta clusterIdx is a valid slot or -1
    for (let i = 0; i < packed.nodeCount; i++) {
      const slot = packed.nodeMeta[i * 4 + 1] | 0;
      expect(slot === -1 || (slot >= 0 && slot < packed.clusters.length)).toBe(true);
    }
  });

  it("computes centroids in slot space (not silently zeroed)", () => {
    // slot 1 (wire id 0): node 0 at (10, -20, 30)
    expect(packed.clusters[1].centroid).toEqual([10, -20, 30]);
    // slot 0 (wire id 1): nodes 1 (-5, 8, 2) and 2 (1, 2, 3) → mean (-2, 5, 2.5)
    const centroid = packed.clusters[0].centroid;
    expect(centroid[0]).toBeCloseTo(-2);
    expect(centroid[1]).toBeCloseTo(5);
    expect(centroid[2]).toBeCloseTo(2.5);
  });

  it("votes the dominant namespace per cluster", () => {
    // slot 0 mixes ns 1 and ns 0 → tie → deterministic first max
    expect([0, 1]).toContain(packed.clusterNs[0]);
    expect(packed.clusterNs[1]).toBe(0);
  });

  it("rejects an empty snapshot", () => {
    expect(() => packSnapshot({ ...raw, nodes: [], edges: [] }, false)).toThrow(/empty/);
  });

  it("unknown wire cluster ids degrade to loose stars (-1), never OOB", () => {
    const weird: RawMapSnapshot = {
      ...raw,
      nodes: raw.nodes.map((node, i) => (i === 0 ? ([...node.slice(0, 4), 40907, ...node.slice(5)] as typeof node) : node)),
    };
    const packedWeird = packSnapshot(weird, false);
    expect(packedWeird.nodeMeta[1] | 0).toBe(-1); // 40907 unknown → loose star
    for (let i = 1; i < packedWeird.nodeCount; i++) {
      const slot = packedWeird.nodeMeta[i * 4 + 1] | 0;
      expect(slot >= -1 && slot < packedWeird.clusters.length).toBe(true);
    }
  });
});

describe("packSnapshot at prod scale (15 071 nodes / 84 742 edges)", () => {
  // Regression budget: the pack must stay far under the 2s HUD budget —
  // the mini fixtures never caught the flat-out hang class of bugs.
  it("packs in well under 2s and yields in-range cluster slots", () => {
    const n = 15_071;
    const m = 84_742;
    let seed = 20260922;
    const rnd = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    const nodes = Array.from({ length: n }, (_, i): RawMapSnapshot["nodes"][number] => [
      `uuid-${i}`,
      `гранула ${i} про деплой и шейдеры звёздной карты памяти`.slice(0, 80),
      "превью на русском языке около ста восьмидесяти символов с границей слова и многоточием в конце ".repeat(2).slice(0, 180),
      Math.floor(rnd() * 5),
      // gapped DB-style ids: slot base 37 → far beyond clusters.length
      Math.floor(rnd() * 1753) * 37 + 11,
      1 + Math.floor(rnd() * 5),
      rnd() < 0.03 ? 1 : 0,
      Math.round((rnd() - 0.5) * 2000),
      Math.round((rnd() - 0.5) * 2000),
      Math.round((rnd() - 0.5) * 2000),
    ]);
    const edges = Array.from({ length: m }, (): RawMapSnapshot["edges"][number] => [
      Math.floor(rnd() * n),
      Math.floor(rnd() * n),
      Math.floor(rnd() * 28),
      Math.round((1 + rnd() * 2) * 100) / 100,
    ]);
    const rawBig: RawMapSnapshot = {
      v: "prod-scale",
      ns: ["a", "b", "c", "d", "e"],
      et: Array.from({ length: 28 }, (_, i) => `link_${i}`),
      clusters: Array.from({ length: 1753 }, (_, slot) => ({ id: slot * 37 + 11, ns: slot % 5, size: 8 })),
      nodes,
      edges,
    };

    const t0 = performance.now();
    const big = packSnapshot(rawBig, true);
    const elapsed = performance.now() - t0;

    expect(big.nodeCount).toBe(n);
    expect(big.edgeCount).toBe(m);
    expect(elapsed).toBeLessThan(2000);
    // the silent-rot guard: gapped ids must still land in valid slots with
    // real centroids (the prod hang came from exactly this path)
    for (let i = 0; i < big.nodeCount; i++) {
      const slot = big.nodeMeta[i * 4 + 1] | 0;
      expect(slot === -1 || slot < big.clusters.length).toBe(true);
    }
    const nonZero = big.clusters.filter((c) => c.centroid[0] !== 0 || c.centroid[1] !== 0 || c.centroid[2] !== 0);
    expect(nonZero.length).toBeGreaterThan(1700); // ≥97% clusters with a real centroid
  });
});
