import { describe, expect, it } from "vitest";
import {
  collectLabelCandidates,
  lodModeFor,
  selectLabeledNodes,
  type LabelCandidate,
} from "./lod";
import { buildMockSnapshot } from "./mock";
import { packSnapshot } from "./pack";

describe("lodModeFor — hysteresis switch", () => {
  it("starts in clusters when the camera opens far away", () => {
    expect(lodModeFor(5000, null)).toBe("clusters");
    expect(lodModeFor(1000, null)).toBe("full");
  });

  it("crosses into clusters only at the enter threshold", () => {
    expect(lodModeFor(1900, "full")).toBe("full");
    expect(lodModeFor(2600, "full")).toBe("clusters");
  });

  it("unfolds the full graph only under the exit threshold", () => {
    expect(lodModeFor(2500, "clusters")).toBe("clusters");
    expect(lodModeFor(1900, "clusters")).toBe("full");
  });

  it("never flickers inside the hysteresis band", () => {
    let mode: "full" | "clusters" | null = "full";
    // 2600 crosses in; 2100/2400 stay inside the band (no flip); 1850 unfolds
    for (const distance of [2000, 2600, 2400, 2100, 2400, 1850]) {
      mode = lodModeFor(distance, mode);
    }
    expect(mode).toBe("full");
  });
});

const candidate = (over: Partial<LabelCandidate>): LabelCandidate => ({
  index: 0,
  x: 100,
  y: 100,
  radiusPx: 4,
  depth: 500,
  importance: 3,
  behind: false,
  ...over,
});

describe("selectLabeledNodes — top-K DOM label culling (§7)", () => {
  it("drops behind-camera and off-screen stars", () => {
    const picks = selectLabeledNodes(
      [
        candidate({ index: 1, behind: true }),
        candidate({ index: 2, x: -200 }),
        candidate({ index: 3, y: 5000 }),
        candidate({ index: 4 }),
      ],
      800,
      600,
      10,
    );
    expect(picks.map((p) => p.index)).toEqual([4]);
  });

  it("respects the projected-size threshold", () => {
    const picks = selectLabeledNodes(
      [candidate({ index: 1, radiusPx: 1 }), candidate({ index: 2, radiusPx: 2.5 })],
      800,
      600,
      10,
    );
    expect(picks.map((p) => p.index)).toEqual([2]);
  });

  it("nearest first, importance as tiebreak, min pixel gap enforced", () => {
    const picks = selectLabeledNodes(
      [
        candidate({ index: 1, depth: 100, x: 100, y: 100, importance: 1 }),
        candidate({ index: 2, depth: 110, x: 130, y: 120, importance: 5 }), // too close to #1
        candidate({ index: 3, depth: 120, x: 400, y: 400, importance: 2 }),
      ],
      800,
      600,
      10,
    );
    expect(picks.map((p) => p.index)).toEqual([1, 3]);
  });

  it("caps the count at maxLabels", () => {
    const many = Array.from({ length: 50 }, (_, i) =>
      candidate({ index: i, x: (i % 10) * 130, y: Math.floor(i / 10) * 130, depth: 100 + i }),
    );
    expect(selectLabeledNodes(many, 2000, 2000, 5)).toHaveLength(5);
  });
});

describe("collectLabelCandidates", () => {
  it("projects every node through the callback", () => {
    const packed = packSnapshot(buildMockSnapshot(40, 60), false);
    const seen: number[] = [];
    const candidates = collectLabelCandidates(packed, (x, y, z) => {
      seen.push(1);
      expect(typeof x).toBe("number");
      void y;
      void z;
      return { x: 0, y: 0, depth: 100, behind: false, radiusPx: 5 };
    });
    expect(candidates).toHaveLength(40);
    expect(seen).toHaveLength(40);
  });
});
