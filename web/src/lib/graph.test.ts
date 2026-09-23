import { describe, expect, it } from "vitest";
import type { RelationsPayload, SearchHit } from "../api/types";
import type { RawMapSnapshot } from "./fullmap/types";
import {
  buildGraphModel,
  edgeKind,
  edgeThickness,
  filterSnapshotLayers,
  graphNodeFromRecord,
  nodeLabel,
  nodeSize,
  starGlow,
} from "./graph";
import { resolveCssColor, toRgba } from "./colors";

const hit = (id: string, score = 0.5, importance = 3, ns = "code_knowledge"): SearchHit =>
  ({
    id,
    content: `контент гранулы ${id} с достаточно длинным текстом для обрезки подписи узла`,
    metadata: {},
    importance,
    score,
    project_id: null,
    status: "asserted",
    namespace: ns,
    created_at: null,
    last_accessed_at: null,
    frozen: false,
    score_rrf: null,
    score_decay: null,
    score_importance: null,
  });

const relations = (outgoing: Array<[string, string]>, incoming: Array<[string, string]> = []): RelationsPayload => ({
  outgoing: outgoing.map(([target, linkType], i) => ({
    id: `o${i}`,
    source_id: "seed",
    target_id: target,
    target_name: null,
    link_type: linkType,
    description: null,
    weight: 1,
    metadata: {},
    created_at: null,
  })),
  incoming: incoming.map(([source, linkType], i) => ({
    id: `i${i}`,
    source_id: source,
    target_id: "seed",
    target_name: null,
    link_type: linkType,
    description: null,
    weight: 1,
    metadata: {},
    created_at: null,
  })),
});

describe("buildGraphModel", () => {
  it("seeds nodes from hits and expands relation neighbors", () => {
    const model = buildGraphModel(
      [hit("seed"), hit("other")],
      new Map([["seed", relations([["n1", "related_to"], ["n2", "references"]], [["n3", "supports"]])]]),
    );
    expect(model.nodes.map((n) => n.id)).toEqual(["seed", "other", "n1", "n2", "n3"]);
    expect(model.edges.map((e) => `${e.source}>${e.target}`)).toEqual(["seed>n1", "seed>n2", "n3>seed"]);
  });

  it("marks neighbors as non-seed satellites with no importance", () => {
    const model = buildGraphModel([hit("seed")], new Map([["seed", relations([["n1", "related_to"]])]]));
    const neighbor = model.nodes.find((n) => n.id === "n1");
    expect(neighbor?.seed).toBe(false);
    expect(neighbor?.importance).toBeNull();
    expect(neighbor?.namespace).toBeNull();
    expect(neighbor?.status).toBeNull();
  });

  it("colors relation neighbors that are present in the search results", () => {
    // a neighbor known to the search rides in as a full (seed) star
    const model = buildGraphModel(
      [hit("seed"), hit("n1", 0.5, 4, "user_facts")],
      new Map([["seed", relations([["n1", "related_to"]])]]),
    );
    const neighbor = model.nodes.find((n) => n.id === "n1");
    expect(neighbor).toMatchObject({
      seed: true,
      namespace: "user_facts",
      importance: 4,
      status: "asserted",
    });
  });

  it("dedups identical (source, target, linkType) triples", () => {
    const once = relations([["n1", "related_to"]]).outgoing;
    const payload: RelationsPayload = {
      outgoing: [...once, ...once.map((r, i) => ({ ...r, id: `dup${i}` }))],
      incoming: [],
    };
    const model = buildGraphModel([hit("seed")], new Map([["seed", payload]]));
    expect(model.edges.length).toBe(1);
  });

  it("keeps opposite directions of the same pair as separate edges", () => {
    const payload: RelationsPayload = {
      outgoing: relations([["n1", "related_to"]]).outgoing,
      incoming: relations([], [["n1", "related_to"]]).incoming,
    };
    const model = buildGraphModel([hit("seed")], new Map([["seed", payload]]));
    expect(model.edges.length).toBe(2);
  });

  it("drops self-loops and edges with no target", () => {
    const payload = relations([["seed", "self_link"], ["", "broken"], ["n1", "ok"]]);
    const model = buildGraphModel([hit("seed")], new Map([["seed", payload]]));
    expect(model.edges.length).toBe(1);
    expect(model.edges[0].target).toBe("n1");
  });

  it("respects the node cap, dropping edges outside the survivor set", () => {
    const many = Array.from({ length: 10 }, (_, i) => hit(`h${i}`));
    const payload = relations(Array.from({ length: 20 }, (_, i) => [`n${i}`, "related_to"]));
    const model = buildGraphModel(many, new Map([["h0", payload]]), { maxNodes: 15 });
    expect(model.nodes.length).toBe(15);
    expect(model.edges.every((e) => model.nodes.some((n) => n.id === e.source) && model.nodes.some((n) => n.id === e.target))).toBe(true);
  });

  it("expands only the first seedExpansion seeds", () => {
    const two = [hit("a"), hit("b")];
    const map = new Map([
      ["a", relations([["na", "related_to"]])],
      ["b", relations([["nb", "related_to"]])],
    ]);
    const model = buildGraphModel(two, map, { seedExpansion: 1 });
    expect(model.nodes.map((n) => n.id)).toContain("na");
    expect(model.nodes.map((n) => n.id)).not.toContain("nb");
  });

  it("returns empty model for empty input", () => {
    expect(buildGraphModel([], new Map())).toEqual({ nodes: [], edges: [] });
  });
});

describe("nodeLabel", () => {
  it("prefers metadata.entity_name", () => {
    const named = { ...hit("x"), metadata: { entity_name: "ADR-018" } };
    expect(nodeLabel("x", named)).toBe("ADR-018");
  });

  it("truncates long content to 60 chars with an ellipsis", () => {
    const label = nodeLabel("x", hit("x"));
    expect(label.length).toBe(61);
    expect(label.endsWith("…")).toBe(true);
  });

  it("falls back to id head when no hit is given", () => {
    expect(nodeLabel("12345678-9abc")).toBe("12345678");
  });
});

describe("sigma attribute mapping", () => {
  it("maps importance 1–5 onto a 4–13px radius", () => {
    const node = (importance: number | null) => ({
      id: "x",
      label: "x",
      namespace: null,
      importance,
      seed: true,
      status: null,
      position: null,
    });
    expect(nodeSize(node(1))).toBe(4);
    expect(nodeSize(node(5))).toBeCloseTo(13);
    expect(nodeSize({ ...node(null), seed: false })).toBe(4);
  });

  it("clamps edge weight onto a 1–3px thickness", () => {
    expect(edgeThickness(1)).toBe(1);
    expect(edgeThickness(2.5)).toBe(2.5);
    expect(edgeThickness(9)).toBe(3);
    expect(edgeThickness(0)).toBe(1);
  });
});

describe("star map mapping (EVE art direction)", () => {
  it("classifies link types into visual families", () => {
    expect(edgeKind("supersedes")).toBe("supersedes");
    expect(edgeKind("superseded_by")).toBe("supersedes");
    expect(edgeKind("contradicts")).toBe("contradicts");
    expect(edgeKind("related_to")).toBe("route");
  });

  it("scales halo intensity with importance", () => {
    expect(starGlow({ id: "x", label: "x", namespace: null, importance: 5, seed: true, status: "asserted", position: null })).toBe(1);
    expect(starGlow({ id: "x", label: "x", namespace: null, importance: 1, seed: true, status: "asserted", position: null })).toBeCloseTo(0.2);
    expect(starGlow({ id: "x", label: "x", namespace: null, importance: null, seed: false, status: null, position: null })).toBeCloseTo(0.3);
  });

  it("marks superseded and retracted granules as extinguished", () => {
    expect(
      starGlow({ id: "x", label: "x", namespace: null, importance: 5, seed: true, status: "superseded", position: null }),
    ).toBeLessThan(0);
    expect(
      starGlow({ id: "x", label: "x", namespace: null, importance: 5, seed: true, status: "retracted", position: null }),
    ).toBeLessThan(0);
  });
});

describe("graphNodeFromRecord", () => {
  const record = {
    id: "abc-123",
    content: "контент гранулы про архитектуру",
    metadata: { entity_name: "ADR-018" },
    namespace: "project_meta",
    importance: 3,
    status: "asserted",
  };

  it("builds a full star from a fetched granule", () => {
    expect(graphNodeFromRecord(record)).toEqual({
      id: "abc-123",
      label: "ADR-018",
      namespace: "project_meta",
      importance: 3,
      seed: false,
      status: "asserted",
      position: null,
    });
  });

  it("keeps a superseded granule extinguished after enrichment", () => {
    const node = graphNodeFromRecord({ ...record, status: "superseded" });
    expect(starGlow(node)).toBeLessThan(0);
  });

  it("carries map_layout position through enrichment", () => {
    const placed = graphNodeFromRecord({ ...record, position: [-240.5, 90.0, 812.25] });
    expect(placed.position).toEqual([-240.5, 90.0, 812.25]);
  });
});

describe("filterSnapshotLayers — скрытые слои легенды созвездия", () => {
  // узел: [id, label, preview, nsIdx, clusterIdx, size, flags, x, y, z]
  const node = (id: string, nsIdx: number): RawMapSnapshot["nodes"][number] => [
    id, id, null, nsIdx, -1, 3, 0, 1.5, -2.0, 700.25,
  ];
  const snapshot = (): RawMapSnapshot => ({
    v: "constellation",
    ns: ["code_knowledge", "project_meta"],
    et: ["related_to"],
    clusters: [],
    nodes: [node("a", 0), node("b", 1), node("c", 0), node("d", 1)],
    // a—b (0—1), b—c (1—2), c—d (2—3), a—a self (0—0)
    edges: [
      [0, 1, 0, 1],
      [1, 2, 0, 1.5],
      [2, 3, 0, 2],
      [0, 0, 0, 1],
    ],
  });

  it("hidden layer: surviving edges still point at THEIR nodes (index remap)", () => {
    // скрываем code_knowledge: выживают b(1) и d(3) → новые индексы 0 и 1;
    // единственное выжившее ребро c—d имеет скрытый конец и уходит ЦЕЛИКОМ
    const filtered = filterSnapshotLayers(snapshot(), new Set(["b", "d"]));
    expect(filtered.nodes.map((n) => n[0])).toEqual(["b", "d"]);

    // каждое ребро — между двумя выжившими uuid (не чужими узлами)
    for (const [src, tgt] of filtered.edges) {
      expect(filtered.nodes[src]).toBeDefined();
      expect(filtered.nodes[tgt]).toBeDefined();
    }
    expect(filtered.edges).toEqual([]);
  });

  it("hidden middle nodes shift indices — edge endpoints keep their uuids", () => {
    // скрываем b(1) и c(2): выживают a(0), d(3) → новые 0, 1; ребро a—b
    // выброшено (b скрыт), c—d выброшено, a—a остаётся 0—0
    const filtered = filterSnapshotLayers(snapshot(), new Set(["a", "d"]));
    expect(filtered.nodes.map((n) => n[0])).toEqual(["a", "d"]);
    expect(filtered.edges).toEqual([[0, 0, 0, 1]]);

    // прямая проверка «не уехали на чужие»: концы по uuid
    const endpointIds = filtered.edges.map(([s, t]) => [filtered.nodes[s][0], filtered.nodes[t][0]]);
    expect(endpointIds).toEqual([["a", "a"]]);
  });

  it("keeps a cross-layer edge intact when both ends survive", () => {
    // скрываем только c: a—b остаётся, индексы a(0)→0, b(1)→1 без сдвига
    const filtered = filterSnapshotLayers(snapshot(), new Set(["a", "b", "d"]));
    expect(filtered.edges).toEqual([
      [0, 1, 0, 1],
      [0, 0, 0, 1],
    ]);
  });

  it("preserves weight and typeIdx through the remap", () => {
    const filtered = filterSnapshotLayers(snapshot(), new Set(["b", "c"]));
    // выживает только b—c (1—2 → 0—1) с weight 1.5
    expect(filtered.edges).toEqual([[0, 1, 0, 1.5]]);
  });

  it("all layers hidden — empty graph (same as before the fix)", () => {
    const filtered = filterSnapshotLayers(snapshot(), new Set());
    expect(filtered.nodes).toEqual([]);
    expect(filtered.edges).toEqual([]);
  });
});

describe("toRgba", () => {
  it("re-emits hex colors with the given alpha", () => {
    expect(toRgba("#4DC9FF", 0.25)).toBe("rgba(77, 201, 255, 0.25)");
  });

  it("converts ladder hsl() colors so WebGL can parse them", () => {
    expect(toRgba("hsl(120 70% 70%)", 0.5)).toMatch(/^rgba\(\d+, \d+, \d+, 0\.5\)$/);
  });

  it("clamps alpha into 0..1", () => {
    expect(toRgba("#4DC9FF", 5)).toBe("rgba(77, 201, 255, 1)");
  });
});

describe("resolveCssColor", () => {
  it("passes literal colors through", () => {
    expect(resolveCssColor("#FF8E7A")).toBe("#FF8E7A");
  });

  it("resolves var() tokens to the palette fallback when getComputedStyle is absent", () => {
    // node test env: no DOM — the slate default keeps WebGL painting sane
    expect(resolveCssColor("var(--sl-ns-code-knowledge)")).toBe("#8A97AC");
  });
});
