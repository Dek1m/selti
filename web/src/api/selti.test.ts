import { describe, expect, it } from "vitest";
import type { SearchHit } from "./types";
import { mergeHits } from "./selti";

const hit = (id: string, score: number): SearchHit =>
  ({ id, content: `c-${id}`, metadata: {}, importance: 3, score, project_id: null, status: "asserted", namespace: "code_knowledge", created_at: null, last_accessed_at: null, frozen: false, score_rrf: null, score_decay: null, score_importance: null });

describe("mergeHits (namespace fan-out merge)", () => {
  it("dedups by id keeping the higher score", () => {
    const merged = mergeHits([
      [hit("a", 0.5), hit("b", 0.7)],
      [hit("a", 0.9), hit("c", 0.1)],
    ]);
    expect(merged.map((h) => h.id)).toEqual(["a", "b", "c"]);
    expect(merged[0].score).toBe(0.9);
  });

  it("sorts by score descending", () => {
    const merged = mergeHits([[hit("x", 0.2)], [hit("y", 0.8)]]);
    expect(merged.map((h) => h.id)).toEqual(["y", "x"]);
  });

  it("keeps the full merged ranking — pagination slices outside", () => {
    const many = Array.from({ length: 80 }, (_, i) => hit(`id${i}`, i / 100));
    expect(mergeHits([many, many]).length).toBe(80);
    expect(mergeHits([many, many])[0].id).toBe("id79");
  });

  it("returns empty for empty inputs", () => {
    expect(mergeHits([[], []])).toEqual([]);
  });
});
