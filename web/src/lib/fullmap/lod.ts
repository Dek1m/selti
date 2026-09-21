// Label top-K picker for the full map (§7 "метки в 3D = DOM-пад"):
// only stars inside the viewport, above a projected-size threshold,
// nearest first, importance as tiebreak; a minimum pixel gap keeps the
// tag layer breathable. Pure — tested with fake projections.
// (Кластерный LOD убран по развороту Мастера — всегда полный граф.)

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
 * Top-K label picker: viewport filter, projected-size threshold,
 * nearest first with importance tiebreak, minimum pixel gap enforced.
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
