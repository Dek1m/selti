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
