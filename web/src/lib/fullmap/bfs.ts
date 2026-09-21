// BFS engine over the CSR adjacency (§2.3/§4.2): levels from a seed star,
// the continuous glass curve that turns distance into opacity/desaturation.
// Pure functions on typed arrays — no three.js here, unit-testable.

import type { PackedMapSnapshot } from "./types";

/**
 * BFS distances from `seeds` over CSR adjacency. Returns a fresh Int32Array
 * of nodeCount entries: -1 = unreachable, 0 = a seed itself.
 * Visits every node once — milliseconds on 15k/30k, no allocations in the loop.
 */
export function bfsLevels(packed: PackedMapSnapshot, seeds: number[]): Int32Array {
  const levels = new Int32Array(packed.nodeCount).fill(-1);
  const queue = new Int32Array(packed.nodeCount);
  let head = 0;
  let tail = 0;

  for (const seed of seeds) {
    if (seed < 0 || seed >= packed.nodeCount || levels[seed] >= 0) continue;
    levels[seed] = 0;
    queue[tail++] = seed;
  }

  const { adjOffsets, adjList } = packed;
  while (head < tail) {
    const node = queue[head++];
    const next = levels[node] + 1;
    for (let i = adjOffsets[node]; i < adjOffsets[node + 1]; i++) {
      const neighbor = adjList[i];
      if (levels[neighbor] >= 0) continue;
      levels[neighbor] = next;
      queue[tail++] = neighbor;
    }
  }
  return levels;
}

const GLASS_NEAR = 0.95;
const GLASS_FAR = 0.12;
/** BFS radius where the glass ramp ends (§4.2 example curve). */
export const GLASS_RADIUS = 6;

function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

/**
 * The Master's glass curve: `mix(0.95, 0.12, smoothstep(0, 6, dist))`.
 * dist < 0 → no selection, the star stays solid (1). Selected star (0) and
 * its neighbors (1) read almost opaque, the far field melts into glass.
 */
export function glassAlpha(dist: number): number {
  if (dist < 0) return 1;
  return GLASS_NEAR + (GLASS_FAR - GLASS_NEAR) * smoothstep(0, GLASS_RADIUS, dist);
}

/**
 * Desaturation amount 0..1 paired with glassAlpha: glassy stars lose their
 * color toward a neutral tint (the shader mixes rgb toward luma by this).
 */
export function glassDesaturation(dist: number): number {
  if (dist < 0) return 0;
  return smoothstep(0, GLASS_RADIUS, dist) * 0.85;
}

/**
 * Emphasis boost for the selected star itself: size ×, halo ×.
 * Neighbors stay untouched — "чуть приглушены" is the default curve.
 */
export function selectionBoost(dist: number): number {
  return dist === 0 ? 1.6 : 1;
}
