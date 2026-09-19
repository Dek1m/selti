import { describe, expect, it } from "vitest";
import { decomposeScore, formatScore, scoreAriaLabel } from "./score";

describe("decomposeScore", () => {
  it("normalizes rrf against the RRF anchor (0.033 → 1.0)", () => {
    const [rrf] = decomposeScore({ rrf: 0.033, decay: 1, importance: 1 });
    expect(rrf.norm).toBe(1);
  });

  it("keeps decay and importance as-is within 0..1", () => {
    const parts = decomposeScore({ rrf: 0.0165, decay: 0.5, importance: 0.8 });
    expect(parts.map((p) => p.norm)).toEqual([0.5, 0.5, 0.8]);
  });

  it("clamps overshooting factors to 1", () => {
    const parts = decomposeScore({ rrf: 0.9, decay: 1.2, importance: 5 });
    expect(parts.every((p) => p.norm === 1)).toBe(true);
  });

  it("maps null factors to norm 0 and preserves raw null", () => {
    const parts = decomposeScore({ rrf: null, decay: 0.4, importance: null });
    expect(parts[0].raw).toBeNull();
    expect(parts[0].norm).toBe(0);
    expect(parts[2].norm).toBe(0);
    expect(parts[1].norm).toBe(0.4);
  });
});

describe("formatScore", () => {
  it("uses 2 decimals from 0.1 up", () => {
    expect(formatScore(0.8712)).toBe("0.87");
  });
  it("uses 3 decimals below 0.01", () => {
    expect(formatScore(0.00296)).toBe("0.003");
  });
});

describe("scoreAriaLabel", () => {
  it("spells out raw factors, nulls as em-dash", () => {
    expect(scoreAriaLabel(0.87, { rrf: 0.03, decay: 0.96, importance: 1 })).toBe(
      "Релевантность 0.87: rrf 0.030, decay 0.960, importance 1.000",
    );
    expect(scoreAriaLabel(0.1, { rrf: null, decay: null, importance: null })).toContain("rrf —");
  });
});
