import { describe, expect, it } from "vitest";
import { buildMockSnapshot, MOCK_EDGE_COUNT, MOCK_NODE_COUNT, truncatePreview } from "./mock";
import { packSnapshot, unpackNodeString } from "./pack";

describe("buildMockSnapshot — контракт снапшота (§3)", () => {
  const mock = buildMockSnapshot(600, 1200);

  it("is deterministic — identical bytes on every call", () => {
    const twin = buildMockSnapshot(600, 1200);
    expect(JSON.stringify(twin)).toBe(JSON.stringify(mock));
  });

  it("carries the columnar contract fields", () => {
    expect(Array.isArray(mock.ns)).toBe(true);
    expect(mock.ns.length).toBeGreaterThan(0);
    expect(mock.et.length).toBeGreaterThan(0);
    expect(mock.clusters.length).toBeGreaterThan(0);
    expect(mock.v).toMatch(/^mock-/);
  });

  it("matches the requested scale by default", () => {
    const full = buildMockSnapshot();
    expect(full.nodes).toHaveLength(MOCK_NODE_COUNT);
    expect(full.edges).toHaveLength(MOCK_EDGE_COUNT);
  });

  it("node tuples respect the positional contract", () => {
    for (const node of mock.nodes.slice(0, 50)) {
      expect(node).toHaveLength(10);
      const [uuid, name, preview, nsIdx, clusterIdx, size, flags, x, y, z] = node;
      expect(uuid.length).toBeGreaterThan(0);
      expect(name.length).toBeGreaterThan(0);
      expect(name.length).toBeLessThanOrEqual(80);
      expect(preview === null || preview.length <= 180).toBe(true);
      expect(nsIdx).toBeGreaterThanOrEqual(0);
      expect(nsIdx).toBeLessThan(mock.ns.length);
      if (clusterIdx >= 0) expect(clusterIdx).toBeLessThan(mock.clusters.length);
      expect(size).toBeGreaterThanOrEqual(1);
      expect(size).toBeLessThanOrEqual(5);
      // final contract: flags bit0 = frozen only, values are 0|1
      expect([0, 1]).toContain(flags);
      // int-rounded coordinates in the bbox cube (§2.2)
      expect(Number.isInteger(x)).toBe(true);
      expect(Number.isInteger(y)).toBe(true);
      expect(Number.isInteger(z)).toBe(true);
      for (const axis of [x, y, z]) {
        expect(Math.abs(axis)).toBeLessThanOrEqual(1100); // gaussian cloud slack
      }
    }
  });

  it("clusters follow the {id, ns, size} shape", () => {
    for (const cluster of mock.clusters) {
      expect(cluster.id).toBeGreaterThanOrEqual(0);
      expect(cluster.id).toBeLessThan(mock.clusters.length);
      expect(cluster.ns).toBeGreaterThanOrEqual(0);
      expect(cluster.ns).toBeLessThan(mock.ns.length);
      expect(cluster.size).toBeGreaterThan(0);
    }
  });

  it("truncates previews on a word boundary with an ellipsis", () => {
    const previews = mock.nodes.map((node) => node[2] ?? "");
    expect(previews.every((p) => p.length <= 180)).toBe(true);
    const truncated = previews.filter((p) => p.endsWith("…"));
    expect(truncated.length).toBeGreaterThan(0);
    // untouched previews keep their sentence end
    const intact = previews.filter((p) => p && !p.endsWith("…"));
    expect(intact.length).toBeGreaterThan(0);
    expect(intact.some((p) => p.endsWith("."))).toBe(true);
  });

  describe("truncatePreview (контракт §3: усечение по границе слова)", () => {
    it("passes short texts through untouched", () => {
      expect(truncatePreview("короткое превью", 180)).toBe("короткое превью");
    });

    it("cuts at the last space before the limit and appends …", () => {
      const text = `${"слово ".repeat(60)}конец.`; // 360+ chars
      const cut = truncatePreview(text, 180);
      expect(cut.length).toBeLessThanOrEqual(181); // ≤ limit + ellipsis
      expect(cut.endsWith("…")).toBe(true);
      expect(cut.endsWith(" …")).toBe(false);
      // whole words only: the kept part must be a prefix ending on a word edge
      const kept = cut.slice(0, -1);
      expect(text.startsWith(kept)).toBe(true);
      expect(text[kept.length]).toBe(" ");
    });

    it("falls back to a hard cut when there is no space at all", () => {
      const wall = "я".repeat(300);
      const cut = truncatePreview(wall, 180);
      expect(cut).toBe(`${wall.slice(0, 180)}…`);
    });
  });

  it("edges reference valid node indices, float weights, known types", () => {
    for (const [src, tgt, typeIdx, weight] of mock.edges.slice(0, 100)) {
      expect(src).toBeGreaterThanOrEqual(0);
      expect(src).toBeLessThan(mock.nodes.length);
      expect(tgt).toBeGreaterThanOrEqual(0);
      expect(tgt).toBeLessThan(mock.nodes.length);
      expect(typeIdx).toBeGreaterThanOrEqual(0);
      expect(typeIdx).toBeLessThan(mock.et.length);
      expect(weight).toBeGreaterThanOrEqual(1);
      expect(weight).toBeLessThanOrEqual(3);
      expect(typeof weight).toBe("number");
    }
  });

  it("packs into typed arrays and strings survive the round-trip", () => {
    const packed = packSnapshot(mock, true);
    expect(packed.nodeCount).toBe(600);
    expect(packed.edgeCount).toBe(1200);
    expect(packed.clusters.length).toBe(mock.clusters.length);
    // spot-check: the packed name of node 0 equals the raw name
    expect(unpackNodeString(packed, 0, 1)).toBe(mock.nodes[0][1]);
    expect(unpackNodeString(packed, 599, 0)).toBe(mock.nodes[599][0]);
    // clusters sorted by membership desc
    const members = packed.clusters.map((c) => c.members);
    expect([...members].sort((a, b) => b - a)).toEqual(members);
  });
});
