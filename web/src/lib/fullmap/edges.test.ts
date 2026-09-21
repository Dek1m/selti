import { describe, expect, it } from "vitest";
import { EDGE_VISIBLE_CAP, isAuxiliaryEdge, selectVisibleEdges } from "./edges";

const TYPES = ["related_to", "supersedes", "contradicts"];
const typeOf = (name: string) => TYPES.indexOf(name);

interface EdgeSpec {
  src: number;
  tgt: number;
  type: string;
  weight: number;
}

function build(specs: EdgeSpec[]) {
  const edgeData = new Int32Array(specs.length * 3);
  const edgeWeights = new Float32Array(specs.length);
  specs.forEach((spec, e) => {
    edgeData[e * 3] = spec.src;
    edgeData[e * 3 + 1] = spec.tgt;
    edgeData[e * 3 + 2] = typeOf(spec.type);
    edgeWeights[e] = spec.weight;
  });
  return { edgeData, edgeWeights };
}

describe("isAuxiliaryEdge — co_occurrence-слой", () => {
  it("related_to с weight < 1 служебная, остальное — нет", () => {
    expect(isAuxiliaryEdge("related_to", 0.5)).toBe(true);
    expect(isAuxiliaryEdge("related_to", 1)).toBe(false);
    expect(isAuxiliaryEdge("supersedes", 0.3)).toBe(false);
    expect(isAuxiliaryEdge("contradicts", 0.1)).toBe(false);
  });
});

describe("selectVisibleEdges — кап рёбер (итерация 2)", () => {
  it("caps the draw set at EDGE_VISIBLE_CAP", () => {
    const n = 3000;
    const specs: EdgeSpec[] = Array.from({ length: 4000 }, (_, e) => ({
      src: e % n,
      tgt: (e * 7 + 1) % n,
      type: "related_to",
      weight: 1 + (e % 10) / 10,
    }));
    const { edgeData, edgeWeights } = build(specs);
    const importance = new Float32Array(n).fill(3);
    const visible = new Uint8Array(n).fill(1);

    const picked = selectVisibleEdges(edgeData, edgeWeights, TYPES, importance, visible, {
      showAuxiliary: false,
      cap: EDGE_VISIBLE_CAP,
    });
    expect(picked).toHaveLength(EDGE_VISIBLE_CAP);
    expect(new Set([...picked]).size).toBe(picked.length); // no duplicates
  });

  it("keeps the strongest weight × endpoint-importance first", () => {
    const specs: EdgeSpec[] = [
      { src: 0, tgt: 1, type: "related_to", weight: 1 },   // weak ends
      { src: 2, tgt: 3, type: "related_to", weight: 2.5 }, // strong ends
      { src: 4, tgt: 5, type: "related_to", weight: 1.5 },
    ];
    const { edgeData, edgeWeights } = build(specs);
    const importance = new Float32Array([1, 1, 5, 5, 3, 3]);
    const visible = new Uint8Array(6).fill(1);

    const picked = selectVisibleEdges(edgeData, edgeWeights, TYPES, importance, visible, {
      showAuxiliary: false,
      cap: 2,
    });
    // edge 1 (2.5 × 10) and edge 2 (1.5 × 6) beat edge 0 (1 × 2)
    expect([...picked].sort()).toEqual([1, 2]);
  });

  it("contradicts and supersedes always punch through the cap", () => {
    const specs: EdgeSpec[] = Array.from({ length: 1500 }, (_, e) => ({
      src: e,
      tgt: e + 1,
      type: "related_to",
      weight: 3, // максимально жирные обычные связи
    }));
    specs.push({ src: 0, tgt: 5, type: "contradicts", weight: 0.2 });
    specs.push({ src: 1, tgt: 6, type: "supersedes", weight: 0.3 });
    const { edgeData, edgeWeights } = build(specs);
    const importance = new Float32Array(1600).fill(3);
    const visible = new Uint8Array(1600).fill(1);

    const picked = selectVisibleEdges(edgeData, edgeWeights, TYPES, importance, visible, {
      showAuxiliary: false,
      cap: EDGE_VISIBLE_CAP, // 1200 < 1502 кандидатов
    });
    expect(picked).toHaveLength(EDGE_VISIBLE_CAP);
    expect(picked).toContain(1500); // contradicts
    expect(picked).toContain(1501); // supersedes
  });

  it("hides the co_occurrence layer by default, shows with the toggle", () => {
    const specs: EdgeSpec[] = [
      { src: 0, tgt: 1, type: "related_to", weight: 0.5 }, // co_occurrence
      { src: 2, tgt: 3, type: "related_to", weight: 1.5 },
    ];
    const { edgeData, edgeWeights } = build(specs);
    const importance = new Float32Array(4).fill(3);
    const visible = new Uint8Array(4).fill(1);

    const hidden = selectVisibleEdges(edgeData, edgeWeights, TYPES, importance, visible, {
      showAuxiliary: false,
      cap: EDGE_VISIBLE_CAP,
    });
    expect([...hidden]).toEqual([1]);

    const shown = selectVisibleEdges(edgeData, edgeWeights, TYPES, importance, visible, {
      showAuxiliary: true,
      cap: EDGE_VISIBLE_CAP,
    });
    expect([...shown].sort()).toEqual([0, 1]);
  });

  it("drops edges whose both endpoints are outside the culled draw set", () => {
    const specs: EdgeSpec[] = [
      { src: 0, tgt: 1, type: "related_to", weight: 3 }, // оба невидимы
      { src: 1, tgt: 2, type: "related_to", weight: 1 }, // один видим
      { src: 2, tgt: 3, type: "supersedes", weight: 2 }, // видим
    ];
    const { edgeData, edgeWeights } = build(specs);
    const importance = new Float32Array(4).fill(3);
    const visible = new Uint8Array([0, 0, 1, 1]);

    const picked = selectVisibleEdges(edgeData, edgeWeights, TYPES, importance, visible, {
      showAuxiliary: false,
      cap: EDGE_VISIBLE_CAP,
    });
    expect([...picked].sort()).toEqual([1, 2]);
  });
});
