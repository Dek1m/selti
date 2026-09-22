import { describe, expect, it } from "vitest";
import { selectLabeledNodes, type LabelCandidate } from "./lod";
import { FULL_LAYOUT, CONSTELLATION_LAYOUT, FULL_SPIRAL, ellipseLayout, spiralLayout, hashUuid } from "./layout";

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

describe("ellipseLayout — детерминированный 3D-объём", () => {
  const uuids = Array.from({ length: 3000 }, (_, i) => `${i.toString(16).padStart(8, "0")}-granule`);

  it("is deterministic — identical positions on every call", () => {
    const a = ellipseLayout(uuids, new Float32Array(uuids.length * 3), FULL_LAYOUT);
    const b = ellipseLayout(uuids, new Float32Array(uuids.length * 3), FULL_LAYOUT);
    expect([...a]).toEqual([...b]);
  });

  it("keeps every point inside the bounds (full и созвездие)", () => {
    for (const bounds of [FULL_LAYOUT, CONSTELLATION_LAYOUT]) {
      const pos = ellipseLayout(uuids, new Float32Array(uuids.length * 3), bounds);
      for (let i = 0; i < uuids.length; i++) {
        expect(Math.abs(pos[i * 3])).toBeLessThanOrEqual(bounds.radius);
        expect(Math.abs(pos[i * 3 + 1])).toBeLessThanOrEqual(bounds.thickness);
        expect(Math.abs(pos[i * 3 + 2])).toBeLessThanOrEqual(bounds.radius);
      }
    }
  });

  it("объём объёмный: y-джиттер реально используется", () => {
    const pos = ellipseLayout(uuids, new Float32Array(uuids.length * 3), FULL_LAYOUT);
    const ys = new Set();
    for (let i = 0; i < uuids.length; i++) ys.add(pos[i * 3 + 1].toFixed(1));
    expect(ys.size).toBeGreaterThan(500); // не плоскость
  });

  it("no two points closer than the anti-clump distance (grid guarantee)", () => {
    const pos = ellipseLayout(uuids.slice(0, 800), new Float32Array(800 * 3), FULL_LAYOUT);
    const min2 = 6 * 6 * 0.9; // допуск на релаксацию
    for (let i = 0; i < 800; i++) {
      for (let j = i + 1; j < 800; j++) {
        const dx = pos[i * 3] - pos[j * 3];
        const dy = pos[i * 3 + 1] - pos[j * 3 + 1];
        const dz = pos[i * 3 + 2] - pos[j * 3 + 2];
        expect(dx * dx + dy * dy + dz * dz).toBeGreaterThan(min2);
      }
    }
  });

  it("hashUuid is stable", () => {
    expect(hashUuid("000e4e05-cbde-4e5a-aa08-cb2577bf1c15")).toBe(
      hashUuid("000e4e05-cbde-4e5a-aa08-cb2577bf1c15"),
    );
  });
});

describe("spiralLayout — спираль full-карты", () => {
  const uuids = Array.from({ length: 15_000 }, (_, i) => `${i.toString(16).padStart(8, "0")}-g`);

  it("is deterministic and stays inside the spiral bounds", () => {
    const a = spiralLayout(uuids, new Float32Array(uuids.length * 3), FULL_SPIRAL);
    const b = spiralLayout(uuids, new Float32Array(uuids.length * 3), FULL_SPIRAL);
    expect([...a]).toEqual([...b]);
    for (let i = 0; i < uuids.length; i++) {
      expect(Number.isNaN(a[i * 3])).toBe(false);
      expect(Math.abs(a[i * 3 + 1])).toBeLessThanOrEqual(FULL_SPIRAL.thickness);
      expect(Math.hypot(a[i * 3], a[i * 3 + 2])).toBeLessThan(FULL_SPIRAL.radius * 1.05);
    }
  });

  it("reads as a spiral: most points far from center, none clumped at origin", () => {
    const a = spiralLayout(uuids, new Float32Array(uuids.length * 3), FULL_SPIRAL);
    let nearCenter = 0;
    for (let i = 0; i < uuids.length; i++) {
      if (Math.hypot(a[i * 3], a[i * 3 + 2]) < 60) nearCenter++;
    }
    expect(nearCenter).toBeLessThan(300); // 2% — не «каша в центре»
  });
});
