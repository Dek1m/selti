import { decomposeScore, formatScore, scoreAriaLabel, type ScoreFactors } from "../lib/score";

/** §4.3 spectral assignment: rrf — bioluminescence, decay — azure, importance — iris */
export const FACTOR_COLORS: Record<string, string> = {
  rrf: "var(--sl-accent)",
  decay: "var(--sl-ns-code-knowledge)",
  importance: "var(--sl-ns-project-meta)",
};

/**
 * Signature score gauge (WEB_UI_DESIGN §4.3): mono score digit + spectral
 * bar decomposing score = rrf × decay × importance. Segment width is the
 * normalized factor; raw values live in the hover tooltip and aria-label.
 */
export function ScoreGauge({ score, factors }: { score: number; factors: ScoreFactors }) {
  const segments = decomposeScore(factors);
  return (
    <div className="score" role="img" aria-label={scoreAriaLabel(score, factors)}>
      <span className="val">{formatScore(score)}</span>
      <span className="bar" aria-hidden="true">
        {segments.map((f) => (
          <i
            key={f.key}
            className={`f-${f.key === "rrf" ? "rrf" : f.key === "decay" ? "dec" : "imp"}`}
            style={{ width: `${Math.max(4, f.norm * 100)}%` }}
          />
        ))}
      </span>
      <span className="lbl">rrf·dec·imp</span>
      <span className="tip">
        {segments.map((f) => (
          <div key={f.key}>
            {f.key}: {f.raw === null ? "—" : f.raw.toFixed(4)}
          </div>
        ))}
      </span>
    </div>
  );
}
