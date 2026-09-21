// Columnar packer (PLAN_FULL_MAP_3D §3): raw JSON snapshot → typed arrays.
// Runs inside the web worker. Also builds the CSR adjacency used by the
// BFS engine and cluster centroids used by the LOD "star systems".

import type { PackedCluster, PackedMapSnapshot, RawMapSnapshot } from "./types";

const TEXT_ENCODER = new TextEncoder();

/** Concatenate every node string into one UTF-8 blob with 3 offsets per node. */
function packNodeStrings(
  nodes: RawMapSnapshot["nodes"],
  withPreview: boolean,
): { bytes: Uint8Array; offsets: Int32Array } {
  const n = nodes.length;
  const offsets = new Int32Array(n * 3 + 1);
  // size pass: measure first, allocate once — 15k nodes ≈ 7MB, avoid churn
  let total = 0;
  for (let i = 0; i < n; i++) {
    const node = nodes[i];
    total += TEXT_ENCODER.encode(node[0]).length;
    total += TEXT_ENCODER.encode(node[1]).length;
    const preview = withPreview ? node[2] : null;
    total += preview ? TEXT_ENCODER.encode(preview).length : 0;
  }

  const bytes = new Uint8Array(total);
  let cursor = 0;
  for (let i = 0; i < n; i++) {
    const node = nodes[i];
    offsets[i * 3] = cursor;
    const uuid = TEXT_ENCODER.encode(node[0]);
    bytes.set(uuid, cursor);
    cursor += uuid.length;

    offsets[i * 3 + 1] = cursor;
    const name = TEXT_ENCODER.encode(node[1]);
    bytes.set(name, cursor);
    cursor += name.length;

    offsets[i * 3 + 2] = cursor;
    const preview = withPreview ? node[2] : null;
    if (preview) {
      const encoded = TEXT_ENCODER.encode(preview);
      bytes.set(encoded, cursor);
      cursor += encoded.length;
    }
  }
  offsets[n * 3] = cursor;
  return { bytes, offsets };
}

/** Decode one string field out of the packed blob. */
export function unpackNodeString(packed: PackedMapSnapshot, nodeIndex: number, field: 0 | 1 | 2): string {
  const start = packed.nodeStrOffsets[nodeIndex * 3 + field];
  const end = packed.nodeStrOffsets[nodeIndex * 3 + field + 1];
  if (start === end) return field === 2 ? "" : "";
  return new TextDecoder().decode(packed.nodeStrBytes.subarray(start, end));
}

/**
 * Pack the raw snapshot. Adjacency is deduplicated (multi-edges collapse)
 * so BFS visits each neighbor once; edges keep their original multiplicity
 * for rendering.
 */
export function packSnapshot(raw: RawMapSnapshot, withPreview: boolean): PackedMapSnapshot {
  const n = raw.nodes.length;
  const m = raw.edges.length;
  if (n === 0) throw new Error("empty snapshot");

  // Wire cluster ids are arbitrary ints (DB ids with gaps — прод отдаёт
  // именно такие). Normalize to compact slots 0..c-1 ONCE here, so every
  // downstream typed-array index stays in range: Float64Array writes past
  // the end silently no-op and would rot centroids to [0,0,0].
  const slotOfId = new Map<number, number>();
  raw.clusters.forEach((c, slot) => slotOfId.set(c.id, slot));

  const nodeMeta = new Float32Array(n * 4);
  const nodePositions = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const node = raw.nodes[i];
    nodeMeta[i * 4] = node[3]; // nsIdx
    const rawCluster = node[4]; // wire id | -1
    nodeMeta[i * 4 + 1] = rawCluster >= 0 ? (slotOfId.get(rawCluster) ?? -1) : -1;
    nodeMeta[i * 4 + 2] = node[5]; // size (importance 1..5)
    nodeMeta[i * 4 + 3] = node[6]; // flags bitfield
    nodePositions[i * 3] = node[7];
    nodePositions[i * 3 + 1] = node[8];
    nodePositions[i * 3 + 2] = node[9];
  }

  const edgeData = new Int32Array(m * 3);
  const edgeWeights = new Float32Array(m);
  // degree count for CSR, deduplicated per (src,tgt) pair
  const pairSeen = new Set<number>();
  const degrees = new Int32Array(n);
  for (let e = 0; e < m; e++) {
    const edge = raw.edges[e];
    edgeData[e * 3] = edge[0];
    edgeData[e * 3 + 1] = edge[1];
    edgeData[e * 3 + 2] = edge[2];
    edgeWeights[e] = edge[3];
    const key = edge[0] * n + edge[1];
    if (edge[0] !== edge[1] && !pairSeen.has(key)) {
      pairSeen.add(key);
      degrees[edge[0]]++;
      degrees[edge[1]]++;
    }
  }

  const adjOffsets = new Int32Array(n + 1);
  for (let i = 0; i < n; i++) adjOffsets[i + 1] = adjOffsets[i] + degrees[i];
  const adjList = new Int32Array(adjOffsets[n]);
  const fill = adjOffsets.slice(0, n);
  const adjSeen = new Set<number>();
  for (let e = 0; e < m; e++) {
    const src = edgeData[e * 3];
    const tgt = edgeData[e * 3 + 1];
    if (src === tgt) continue;
    const key = src * n + tgt;
    // multi-edges render, but the adjacency keeps one entry per pair
    if (adjSeen.has(key)) continue;
    adjSeen.add(key);
    adjList[fill[src]++] = tgt;
    adjList[fill[tgt]++] = src;
  }

  // cluster centroids in snapshot space + dominant namespace per cluster,
  // all indexed by the compact slot (PackedCluster.index)
  const clusterCount = raw.clusters.length;
  const clusters: PackedCluster[] = raw.clusters
    .map((c, slot) => ({
      index: slot,
      id: c.id,
      ns: c.ns,
      label: c.label ?? null,
      members: c.size,
      centroid: [0, 0, 0] as [number, number, number],
    }))
    .sort((a, b) => b.members - a.members);
  const accX = new Float64Array(clusterCount);
  const accY = new Float64Array(clusterCount);
  const accZ = new Float64Array(clusterCount);
  const accN = new Float64Array(clusterCount);
  const nsVotes = new Map<number, Int32Array>();
  for (let i = 0; i < n; i++) {
    const clusterIdx = nodeMeta[i * 4 + 1];
    if (clusterIdx < 0 || clusterIdx >= clusterCount) continue;
    accX[clusterIdx] += nodePositions[i * 3];
    accY[clusterIdx] += nodePositions[i * 3 + 1];
    accZ[clusterIdx] += nodePositions[i * 3 + 2];
    accN[clusterIdx] += 1;
    const nsIdx = nodeMeta[i * 4];
    let votes = nsVotes.get(clusterIdx);
    if (!votes) {
      votes = new Int32Array(Math.max(1, raw.ns.length));
      nsVotes.set(clusterIdx, votes);
    }
    if (nsIdx >= 0 && nsIdx < votes.length) votes[nsIdx]++;
  }
  for (const cluster of clusters) {
    const count = accN[cluster.index] || 1;
    cluster.centroid = [accX[cluster.index] / count, accY[cluster.index] / count, accZ[cluster.index] / count];
  }
  const clusterNs = new Int32Array(clusterCount);
  for (const cluster of clusters) {
    const votes = nsVotes.get(cluster.index);
    let best = cluster.ns >= 0 ? cluster.ns : 0;
    let bestVotes = -1;
    if (votes) {
      for (let ns = 0; ns < votes.length; ns++) {
        if (votes[ns] > bestVotes) {
          bestVotes = votes[ns];
          best = ns;
        }
      }
    }
    clusterNs[cluster.index] = best;
  }

  const strings = packNodeStrings(raw.nodes, withPreview);

  return {
    version: raw.v,
    nodeCount: n,
    edgeCount: m,
    withPreview,
    nodeStrBytes: strings.bytes,
    nodeStrOffsets: strings.offsets,
    nodeMeta,
    nodePositions,
    edgeData,
    edgeWeights,
    edgeTypes: raw.et,
    namespaces: raw.ns,
    clusters,
    adjOffsets,
    adjList,
    clusterNs,
  };
}
