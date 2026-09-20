// Pure graph assembly for the constellation screen (§6): search hits seed
// the graph, first-level relations grow it. No sigma/graphology imports —
// the mapping to a renderable Graph happens in the screen; this module is
// deterministic and unit-testable.

import type { RelationsPayload, SearchHit } from "../api/types";

export interface GraphNode {
  id: string;
  label: string;
  namespace: string | null;
  /** 1–5 for seeds; neighbors carry no importance — fixed satellite size */
  importance: number | null;
  seed: boolean;
  /** granule status for seeds; unknown satellites are null */
  status: string | null;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  linkType: string;
  weight: number;
}

/** Visual family of a link on the star map. */
export type EdgeKind = "route" | "supersedes" | "contradicts";

/** supersedes chains read as dashed jump routes, contradictions glow red. */
export function edgeKind(linkType: string): EdgeKind {
  if (linkType === "supersedes" || linkType === "superseded_by") return "supersedes";
  if (linkType === "contradicts") return "contradicts";
  return "route";
}

/**
 * Halo intensity for a star, 0..1. Extinguished granules (superseded /
 * retracted) return -1 — the shader paints them as hollow outlines.
 */
export function starGlow(node: GraphNode): number {
  if (node.status === "superseded" || node.status === "retracted") return -1;
  if (node.importance === null) return 0.3;
  return 0.2 + ((node.importance - 1) / 4) * 0.8;
}

export interface GraphModel {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface GraphOptions {
  /** how many top-scored seeds expand their relations */
  seedExpansion: number;
  /** hard cap on total nodes — the layout stays legible */
  maxNodes: number;
}

export const DEFAULT_GRAPH_OPTIONS: GraphOptions = { seedExpansion: 8, maxNodes: 120 };

/** Short node caption: entity name if granulated, else a content head. */
export function nodeLabel(id: string, hit?: SearchHit): string {
  const name = hit ? hit.metadata?.entity_name : undefined;
  if (typeof name === "string" && name.trim()) return name.trim();
  const content = hit?.content?.replace(/\s+/g, " ").trim();
  if (content) return content.length > 60 ? `${content.slice(0, 60)}…` : content;
  return id.slice(0, 8);
}

/**
 * Build the model: nodes = hits, then for the first `seedExpansion` hits
 * add relation neighbors and edges. Node cap `maxNodes` cuts the growth;
 * edges pointing outside the surviving node set are dropped, as are
 * self-loops and duplicate (source, target, linkType) triples.
 */
export function buildGraphModel(
  hits: SearchHit[],
  relationsById: Map<string, RelationsPayload>,
  options: Partial<GraphOptions> = {},
): GraphModel {
  const { seedExpansion, maxNodes } = { ...DEFAULT_GRAPH_OPTIONS, ...options };

  const nodes = new Map<string, GraphNode>();
  for (const hit of hits) {
    if (nodes.size >= maxNodes) break;
    nodes.set(hit.id, {
      id: hit.id,
      label: nodeLabel(hit.id, hit),
      namespace: hit.namespace,
      importance: hit.importance,
      seed: true,
      status: hit.status,
    });
  }

  const edges: GraphEdge[] = [];
  const seenEdges = new Set<string>();
  const edge = (source: string, target: string, linkType: string, weight: number) => {
    if (source === target) return;
    const id = `${source}|${target}|${linkType}`;
    if (seenEdges.has(id)) return;
    if (!nodes.has(source) || !nodes.has(target)) return;
    seenEdges.add(id);
    edges.push({ id, source, target, linkType, weight });
  };

  for (const hit of hits.slice(0, seedExpansion)) {
    if (nodes.size >= maxNodes) break;
    const relations = relationsById.get(hit.id);
    if (!relations) continue;
    // Outgoing: hit → target. Incoming: source → hit. Either endpoint may
    // be a stranger — both need a node before the edge can survive.
    type Pair = readonly [source: string, target: string, linkType: string, weight: number];
    const pairs: Pair[] = [
      ...relations.outgoing.map((r): Pair => [hit.id, r.target_id ?? "", r.link_type, r.weight]),
      ...relations.incoming.map((r): Pair => [r.source_id, hit.id, r.link_type, r.weight]),
    ];
    const ensureNode = (id: string) => {
      if (!nodes.has(id) && nodes.size < maxNodes) {
        // Neighbor granule: metadata unknown — a slate satellite node
        nodes.set(id, { id, label: id.slice(0, 8), namespace: null, importance: null, seed: false, status: null });
      }
    };
    for (const [source, target, linkType, weight] of pairs) {
      if (!target) continue;
      ensureNode(source);
      ensureNode(target);
      edge(source, target, linkType, weight);
    }
  }

  return { nodes: [...nodes.values()], edges };
}

/** Sigma node size: importance 1–5 maps onto a 4–13px radius. */
export function nodeSize(node: GraphNode): number {
  if (node.importance === null) return 4;
  return 4 + (node.importance - 1) * 2.25;
}

/** Sigma edge thickness: weight 1..3+ clamps to a 1–3px line. */
export function edgeThickness(weight: number): number {
  return 1 + Math.min(2, Math.max(0, weight - 1));
}
