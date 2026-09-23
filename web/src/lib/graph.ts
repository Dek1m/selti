// Pure graph assembly for the constellation screen (§6): search hits seed
// the graph, first-level relations grow it. No sigma/graphology imports —
// the mapping to a renderable Graph happens in the screen; this module is
// deterministic and unit-testable.

import type { RelationsPayload, SearchHit } from "../api/types";
import type { RawMapSnapshot } from "./fullmap/types";

export interface GraphNode {
  id: string;
  label: string;
  namespace: string | null;
  /** 1–5 for seeds; neighbors carry no importance — fixed satellite size */
  importance: number | null;
  seed: boolean;
  /** granule status for seeds; unknown satellites are null */
  status: string | null;
  /** server map_layout coordinates; null — granule has no map row (fallback layout) */
  position: [number, number, number] | null;
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

/** Minimal record shape needed to light a star (MemoryRecord satisfies it). */
export interface GraphNodeRecord {
  id: string;
  content: string;
  metadata: Record<string, unknown>;
  namespace: string | null;
  importance: number;
  status: string;
  /** map_layout coordinates (?with_positions=1; absent — no map row) */
  position?: [number, number, number];
}

/** Turn a fetched granule into a full star (used to enrich gray strangers). */
export function graphNodeFromRecord(record: GraphNodeRecord, seed = false): GraphNode {
  return {
    id: record.id,
    label: nodeLabel(record.id, { metadata: record.metadata, content: record.content } as SearchHit),
    namespace: record.namespace,
    importance: record.importance,
    seed,
    status: record.status,
    position: record.position ?? null,
  };
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

  // Full search index: neighbors pulled in by relations are often present
  // in the search results themselves — inherit their real layer, size and
  // status instead of rendering them as anonymous slate dots.
  const hitById = new Map(hits.map((hit) => [hit.id, hit]));

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
      position: hit.position ?? null,
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
      if (nodes.has(id) || nodes.size >= maxNodes) return;
      const hit = hitById.get(id);
      if (hit) {
        // Known granule from the search results: full star with its real layer
        nodes.set(id, {
          id,
          label: nodeLabel(id, hit),
          namespace: hit.namespace,
          importance: hit.importance,
          seed: false,
          status: hit.status,
          position: hit.position ?? null,
        });
      } else {
        // True stranger: no metadata known — a slate satellite node; its
        // map row (if any) already arrived with this relation's positions
        nodes.set(id, {
          id,
          label: id.slice(0, 8),
          namespace: null,
          importance: null,
          seed: false,
          status: null,
          position: relations.positions?.[id] ?? null,
        });
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

/**
 * Скрытые слои легенды: оставить в снапшоте созвездия только узлы keep-множества.
 * filter сжимает массив узлов — старые индексы рёбер перемапливаются
 * (oldIndex→newIndex), иначе рёбра уезжают на чужие узлы; рёбра с
 * отфильтрованным концом выбрасываются. keepIds пуст (все слои скрыты) —
 * пустые узлы/рёбра, как раньше.
 */
export function filterSnapshotLayers(snapshot: RawMapSnapshot, keepIds: ReadonlySet<string>): RawMapSnapshot {
  if (keepIds.size === 0) return { ...snapshot, nodes: [], edges: [] };
  const remap = new Map<number, number>();
  const nodes = snapshot.nodes.filter((node, index) => {
    if (!keepIds.has(node[0])) return false;
    remap.set(index, remap.size);
    return true;
  });
  const edges: RawMapSnapshot["edges"] = [];
  for (const [src, tgt, typeIdx, weight] of snapshot.edges) {
    const nextSrc = remap.get(src);
    const nextTgt = remap.get(tgt);
    if (nextSrc === undefined || nextTgt === undefined) continue;
    edges.push([nextSrc, nextTgt, typeIdx, weight]);
  }
  return { ...snapshot, nodes, edges };
}
