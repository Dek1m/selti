// Namespace → spectrum color (WEB_UI_DESIGN §2.2). Known layers map to
// --sl-ns-* tokens; registry namespaces without a token get a hue from
// the reserved 12-stop ladder (30° step, deterministic hash of uid) so
// the color is stable across sessions.

const TOKENED: Record<string, string> = {
  user_facts: "var(--sl-ns-user-facts)",
  project_meta: "var(--sl-ns-project-meta)",
  code_knowledge: "var(--sl-ns-code-knowledge)",
  dialogue_insights: "var(--sl-ns-dialogue-insights)",
  infrastructure: "var(--sl-ns-infrastructure)",
  default: "var(--sl-ns-default)",
};

/** Deterministic ladder hue for arbitrary strings (uid, link_type…). */
export function ladderHue(seed: string): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++) {
    h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  }
  return (h % 12) * 30;
}

export function ladderColor(seed: string): string {
  return `hsl(${ladderHue(seed)} 70% 70%)`;
}

/** Spectrum color for a namespace uid: token if defined, ladder otherwise. */
export function namespaceColor(uid: string | null): string {
  if (!uid) return TOKENED.default;
  return TOKENED[uid] ?? ladderColor(uid);
}

/** Link-type badge color — deterministic, from the same ladder. */
export const linkTypeColor = ladderColor;

// WebGL (sigma) paints with literal color strings — CSS custom properties
// are invisible to it. Resolved values are cached per token.
const resolvedTokens = new Map<string, string>();

export function resolveCssColor(color: string): string {
  if (!color.startsWith("var(")) return color;
  const cached = resolvedTokens.get(color);
  if (cached !== undefined) return cached;
  let value = "#8A97AC"; // --sl-ns-default hex fallback (SSR/tests)
  if (typeof getComputedStyle === "function") {
    const css = getComputedStyle(document.documentElement)
      .getPropertyValue(color.slice(4, -1))
      .trim();
    if (css) value = css;
  }
  resolvedTokens.set(color, value);
  return value;
}

/** hsl(h s% l%) → rgb triple (the ladder emits this format). */
export function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = ((h % 360) + 360) % 360 / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  const [r1, g1, b1] =
    hp < 1 ? [c, x, 0] : hp < 2 ? [x, c, 0] : hp < 3 ? [0, c, x] : hp < 4 ? [0, x, c] : hp < 5 ? [x, 0, c] : [c, 0, x];
  const m = l - c / 2;
  return [Math.round((r1 + m) * 255), Math.round((g1 + m) * 255), Math.round((b1 + m) * 255)];
}

const HSL_REGEX = /^hsl\(\s*(-?[\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s*\)$/i;

/**
 * Re-emit a resolved color with a new alpha as `rgba()` — the only
 * translucent literal sigma's floatColor understands. Accepts #rgb/#rrggbb
 * hex and the ladder's `hsl(h s% l%)` format; anything else passes through.
 */
export function toRgba(color: string, alpha: number): string {
  const a = Math.max(0, Math.min(1, alpha));
  if (color.startsWith("#")) {
    const hex = color.length === 4 ? color.slice(1).split("").map((c) => c + c).join("") : color.slice(1);
    const r = parseInt(hex.slice(0, 2), 16);
    const g = parseInt(hex.slice(2, 4), 16);
    const b = parseInt(hex.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${a})`;
  }
  const hsl = color.match(HSL_REGEX);
  if (hsl) {
    const [r, g, b] = hslToRgb(parseFloat(hsl[1]), parseFloat(hsl[2]) / 100, parseFloat(hsl[3]) / 100);
    return `rgba(${r}, ${g}, ${b}, ${a})`;
  }
  return color;
}
