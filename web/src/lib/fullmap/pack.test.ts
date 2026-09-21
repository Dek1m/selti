import { describe, expect, it } from "vitest";
import { packSnapshot, unpackNodeString } from "./pack";
import type { RawMapSnapshot } from "./types";

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
    expect([...packed.nodeMeta.slice(0, 4)]).toEqual([0, 0, 5, 0]);
    // flags bit0 = frozen (узел 1 — frozen)
    expect([...packed.nodeMeta.slice(4, 8)]).toEqual([1, 1, 2, 1]);
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
    // node 0 ↔ 1 once, 1 ↔ 2 once; self-loop 2→2 ignored
    const neighborsOf = (i: number) => [...packed.adjList.slice(packed.adjOffsets[i], packed.adjOffsets[i + 1])].sort();
    expect(neighborsOf(0)).toEqual([1]);
    expect(neighborsOf(1)).toEqual([0, 2]);
    expect(neighborsOf(2)).toEqual([1]);
    expect(packed.adjOffsets[3]).toBe(packed.adjList.length);
  });

  it("maps clusters {id, ns, size, label?} sorted by size, label optional", () => {
    expect(packed.clusters.map((c) => c.index)).toEqual([1, 0]); // size 2 before size 1
    expect(packed.clusters.map((c) => c.members)).toEqual([2, 1]);
    expect(packed.clusters[0].label).toBe("второй");
    expect(packed.clusters[1].label).toBeNull(); // no label on the wire — fine
    // cluster 0: only node 0 at (10, -20, 30)
    expect(packed.clusters.find((c) => c.index === 0)!.centroid).toEqual([10, -20, 30]);
    // cluster 1: nodes 1 (-5, 8, 2) and 2 (1, 2, 3) → mean (-2, 5, 2.5)
    const centroid = packed.clusters.find((c) => c.index === 1)!.centroid;
    expect(centroid[0]).toBeCloseTo(-2);
    expect(centroid[1]).toBeCloseTo(5);
    expect(centroid[2]).toBeCloseTo(2.5);
  });

  it("votes the dominant namespace per cluster", () => {
    // cluster 1 has ns 1 and ns 0 → tie → first index with max votes wins deterministically
    const clusterNs = packed.clusterNs;
    expect([0, 1]).toContain(clusterNs[1]);
    expect(clusterNs[0]).toBe(0);
  });

  it("rejects an empty snapshot", () => {
    expect(() => packSnapshot({ ...raw, nodes: [], edges: [] }, false)).toThrow(/empty/);
  });
});
