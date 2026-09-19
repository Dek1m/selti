// ScoreGauge math (WEB_UI_DESIGN §4.3): score = rrf × decay × importance.
// Raw factors live in different ranges — RRF tops out near ~0.033
// (rank 1 in two fused lists: 2/61), decay and importance weight are
// ~0..1 — so each factor gets its own anchor for visual normalization.
// Raw values always stay visible in the tooltip / aria-label.

export type ScoreFactorKey = "rrf" | "decay" | "importance";

export interface ScoreFactors {
  rrf: number | null;
  decay: number | null;
  importance: number | null;
}

export interface ScoreFactor {
  key: ScoreFactorKey;
  raw: number | null;
  /** 0..1 normalized for the spectrum bar */
  norm: number;
}

/** Adapt a search hit's score_* fields into factor shape. */
export function factorsFromHit(hit: {
  score_rrf: number | null;
  score_decay: number | null;
  score_importance: number | null;
}): ScoreFactors {
  return { rrf: hit.score_rrf, decay: hit.score_decay, importance: hit.score_importance };
}

const ANCHORS: Record<ScoreFactorKey, number> = {
  rrf: 0.033,
  decay: 1,
  importance: 1,
};

const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));

/** Decompose raw factors into normalized display segments. */
export function decomposeScore(factors: ScoreFactors): ScoreFactor[] {
  return (Object.keys(ANCHORS) as ScoreFactorKey[]).map((key) => {
    const raw = factors[key];
    return {
      key,
      raw,
      norm: raw === null ? 0 : clamp01(raw / ANCHORS[key]),
    };
  });
}

/** Compact score label: 0.87 / 0.0296 → "0.87" / "0.03". */
export function formatScore(score: number): string {
  return score >= 0.1 ? score.toFixed(2) : score.toFixed(score >= 0.01 ? 2 : 3);
}

/** Screen-reader text per §9: full decomposition with raw values. */
export function scoreAriaLabel(score: number, factors: ScoreFactors): string {
  const part = (v: number | null): string => (v === null ? "—" : v.toFixed(3));
  return `Релевантность ${formatScore(score)}: rrf ${part(factors.rrf)}, decay ${part(factors.decay)}, importance ${part(factors.importance)}`;
}
