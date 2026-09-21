import { describe, expect, it } from "vitest";
import { bfsLevels, glassAlpha, glassDesaturation, selectionBoost, GLASS_RADIUS } from "./bfs";
import { buildMockSnapshot } from "./mock";
import { packSnapshot } from "./pack";
import type { RawMapSnapshot } from "./types";

// tiny diamond graph: 0 — 1 — 3, 0 — 2 — 3, plus an isolated node 4
const tiny: RawMapSnapshot = {
  v: "test",
  ns: ["a"],
  et: ["related_to"],
  clusters: [],
  nodes: [
    ["0", "zero", null, 0, -1, 1, 0, 0, 0, 0],
    ["1", "one", null, 0, -1, 1, 0, 1, 0, 0],
    ["2", "two", null, 0, -1, 1, 0, 2, 0, 0],
    ["3", "three", null, 0, -1, 1, 0, 3, 0, 0],
    ["4", "iso", null, 0, -1, 1, 0, 4, 0, 0],
  ],
  edges: [
    [0, 1, 0, 1],
    [0, 2, 0, 1],
    [1, 3, 0, 1],
    [2, 3, 0, 1],
    [1, 3, 0, 2], // duplicate pair — must not duplicate adjacency
  ],
};

describe("bfsLevels", () => {
  const packed = packSnapshot(tiny, false);

  it("assigns 0 to the seed and grows by adjacency", () => {
    const levels = bfsLevels(packed, [0]);
    expect([...levels]).toEqual([0, 1, 1, 2, -1]);
  });

  it("supports multiple seeds without re-visiting", () => {
    const levels = bfsLevels(packed, [1, 2]);
    // node 0 neighbors both seeds → level 1; diamond tip 3 → level 1; iso → -1
    expect([...levels]).toEqual([1, 0, 0, 1, -1]);
  });

  it("ignores out-of-range and duplicate seeds", () => {
    const levels = bfsLevels(packed, [3, 3, -1, 99]);
    // seed 3 grows back to its neighbors 1, 2 then to 0
    expect([...levels]).toEqual([2, 1, 1, 0, -1]);
  });

  it("visits the whole 15k mock in one pass", () => {
    const packedMock = packSnapshot(buildMockSnapshot(2000, 4000), false);
    const levels = bfsLevels(packedMock, [0]);
    const reached = levels.reduce((acc, level) => (level >= 0 ? acc + 1 : acc), 0);
    expect(reached).toBeGreaterThan(1900); // a few stars may sit in tiny islands
  });
});

describe("glass curve (§4.2 — решение Мастера)", () => {
  it("is fully opaque without a selection", () => {
    expect(glassAlpha(-1)).toBe(1);
    expect(glassDesaturation(-1)).toBe(0);
  });

  it("mix(0.95, 0.12, smoothstep(0, 6, dist)): seed ≈ 0.95, far ≈ 0.12", () => {
    expect(glassAlpha(0)).toBeCloseTo(0.95, 5);
    expect(glassAlpha(GLASS_RADIUS)).toBeCloseTo(0.12, 5);
    expect(glassAlpha(GLASS_RADIUS + 3)).toBeCloseTo(0.12, 5);
  });

  it("monotonically fades with distance", () => {
    for (let dist = 0; dist < GLASS_RADIUS; dist += 0.5) {
      expect(glassAlpha(dist + 0.5)).toBeLessThan(glassAlpha(dist));
    }
  });

  it("neighbors stay almost solid, deep levels desaturate", () => {
    expect(glassAlpha(1)).toBeGreaterThan(0.85);
    expect(glassDesaturation(1)).toBeLessThan(0.1);
    expect(glassDesaturation(5)).toBeGreaterThan(0.5);
  });

  it("only the selected star gets the size boost", () => {
    expect(selectionBoost(0)).toBeGreaterThan(1);
    expect(selectionBoost(1)).toBe(1);
    expect(selectionBoost(-1)).toBe(1);
  });
});
