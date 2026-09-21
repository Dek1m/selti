// Full map (§3 of PLAN_FULL_MAP_3D): snapshot contract types + the packed
// runtime representation. The wire format is columnar JSON served by
// `GET /api/map/full` (finalized contract): nodes carry name+preview inline,
// clusters are {id, ns, size}, edges are [srcIdx, tgtIdx, typeIdx, weight].

/** `GET /api/map/meta` — cheap freshness probe (<50ms, ETag-friendly). */
export interface MapMeta {
  version: string;
  node_count: number;
  edge_count: number;
  cluster_count: number;
  layout_at: string | null;
  layout_stale: boolean;
}

/**
 * Wire snapshot (columnar). Node tuples are positional:
 * `[uuid, name(≤80), preview(≤180 | null), nsIdx, clusterIdx|-1,
 *   size=importance int 1..5 (default 3), flags int (bit0 frozen),
 *   x, y, z]` — everything numeric is int-rounded by the server.
 * Only asserted granules ship (no superseded/retracted in the map at all).
 * Edge tuples: `[srcIdx, tgtIdx, typeIdx, weight(float)]`.
 * Clusters: `{id, ns, size, ...}` (id = clusterIdx used by nodes; the
 * server may add extra fields — label is picked up when present).
 */
export interface RawMapSnapshot {
  v: string;
  /** namespace uids, indexed by nsIdx */
  ns: string[];
  /** link-type uids, indexed by typeIdx */
  et: string[];
  clusters: Array<{ id: number; ns: number; size: number; label?: string }>;
  nodes: Array<[string, string, string | null, number, number, number, number, number, number, number]>;
  edges: Array<[number, number, number, number]>;
}

/** Cluster record after packing — sorted by size desc, label optional. */
export interface PackedCluster {
  /** cluster id from the wire (parallel to nodeMeta clusterIdx) */
  index: number;
  ns: number;
  label: string | null;
  members: number;
  centroid: [number, number, number];
}

/**
 * Packed snapshot — typed arrays only (transferable from the worker).
 * Strings live in one UTF-8 blob addressed by per-field offsets, so a
 * decode happens only when a tooltip/label actually needs the text.
 */
export interface PackedMapSnapshot {
  version: string;
  nodeCount: number;
  edgeCount: number;
  withPreview: boolean;

  /** UTF-8 bytes of every node string (uuid, name, preview) concatenated */
  nodeStrBytes: Uint8Array;
  /** 3 offsets per node into nodeStrBytes: [uuidStart, nameStart, previewStart] + sentinel */
  nodeStrOffsets: Int32Array;

  /** per node: [nsIdx, clusterIdx, size, flags] */
  nodeMeta: Float32Array;
  /** per node xyz, already in snapshot scale (bbox cube [-1000, 1000]³) */
  nodePositions: Float32Array;

  /** per edge: [srcIdx, tgtIdx, typeIdx] (stride 3) */
  edgeData: Int32Array;
  /** per edge: weight (float) */
  edgeWeights: Float32Array;

  /** link-type uids (parallel to edgeData typeIdx) */
  edgeTypes: string[];
  /** namespace uids (parallel to nodeMeta nsIdx) */
  namespaces: string[];

  /** cluster records sorted by size desc */
  clusters: PackedCluster[];

  /** CSR adjacency for BFS: neighbors of node i are adjList[adjOffsets[i]..adjOffsets[i+1]) */
  adjOffsets: Int32Array;
  adjList: Int32Array;

  /** dominant namespace index per cluster (cluster star color) */
  clusterNs: Int32Array;
}

/** Worker → main progress report during snapshot load. */
export interface LoadProgress {
  phase: "connect" | "download" | "parse" | "pack" | "mock";
  /** 0..1, when known */
  fraction: number;
}

/** Decoded node strings — built lazily for tooltips/labels/selection. */
export interface NodeStrings {
  uuid: string;
  name: string;
  preview: string | null;
}
