// Cluster LOD decision (§2.4/M3): far camera → cluster "star systems" +
// aggregate gates, near camera → the full graph. Pure threshold logic with
// hysteresis so the switch never flickers, plus the label top-K picker.

import type { PackedMapSnapshot } from "./types";

/** Camera distance where the map collapses into star systems. */
export const LOD_CLUSTER_ENTER = 2600;
/** Camera must come this close again to unfold the full graph (hysteresis). */
export const LOD_CLUSTER_EXIT = 1900;

export type LodMode = "full" | "clusters";

/**
 * Hysteresis switch on camera distance: cross `enter` → clusters, come back
 * under `exit` → full. state=null means "no decision yet" (first call).
 */
export function lodModeFor(cameraDistance: number, state: LodMode | null): LodMode {
  if (state === null) return cameraDistance >= LOD_CLUSTER_ENTER ? "clusters" : "full";
  if (state === "full" && cameraDistance >= LOD_CLUSTER_ENTER) return "clusters";
  if (state === "clusters" && cameraDistance <= LOD_CLUSTER_EXIT) return "full";
  return state;
}

export interface LabelCandidate {
  index: number;
  /** screen-space px (viewport fit) */
  x: number;
  y: number;
  /** projected radius in px */
  radiusPx: number;
  /** distance to camera, world units */
  depth: number;
  importance: number;
  behind: boolean;
}

export interface LabelPick {
  index: number;
  x: number;
  y: number;
}

/**
 * Top-K label picker (§7 "метки в 3D = DOM-пад"): only stars inside the
 * viewport, above a projected-size threshold, nearest first, importance as
 * tiebreak; a minimum pixel gap keeps the tag layer breathable.
 * Pure — tested with fake projections.
 */
export function selectLabeledNodes(
  candidates: LabelCandidate[],
  viewportWidth: number,
  viewportHeight: number,
  maxLabels: number,
): LabelPick[] {
  const visible = candidates
    .filter((c) => !c.behind && c.x >= -40 && c.x <= viewportWidth + 40 && c.y >= -40 && c.y <= viewportHeight + 40)
    .filter((c) => c.radiusPx >= 2.2)
    .sort((a, b) => a.depth - b.depth || b.importance - a.importance);

  const MIN_GAP_PX = 90;
  const picked: LabelPick[] = [];
  for (const candidate of visible) {
    if (picked.length >= maxLabels) break;
    const clear = picked.every(
      (p) => Math.hypot(p.x - candidate.x, p.y - candidate.y) >= MIN_GAP_PX,
    );
    if (clear) picked.push({ index: candidate.index, x: candidate.x, y: candidate.y });
  }
  return picked;
}

/** Gather label candidates by projecting every node (called at rAF cadence). */
export function collectLabelCandidates(
  packed: PackedMapSnapshot,
  project: (x: number, y: number, z: number) => { x: number; y: number; depth: number; behind: boolean; radiusPx: number },
): LabelCandidate[] {
  const out: LabelCandidate[] = [];
  for (let i = 0; i < packed.nodeCount; i++) {
    const p = project(
      packed.nodePositions[i * 3],
      packed.nodePositions[i * 3 + 1],
      packed.nodePositions[i * 3 + 2],
    );
    if (p.behind) {
      continue;
    }
    out.push({
      index: i,
      x: p.x,
      y: p.y,
      depth: p.depth,
      radiusPx: p.radiusPx,
      importance: packed.nodeMeta[i * 4 + 2],
      behind: false,
    });
  }
  return out;
}
