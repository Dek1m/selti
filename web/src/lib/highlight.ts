// Query-token highlighting for result excerpts (<mark>, §4.3).
// Pure string splitting — React nodes are assembled by the component.

/** Escape a string for literal use inside a RegExp. */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Tokens worth highlighting: query words of 2+ chars, case-insensitive. */
export function queryTokens(query: string): string[] {
  return [...new Set(query.trim().toLowerCase().split(/\s+/).filter((t) => t.length >= 2))];
}

/**
 * Split text into plain / match segments for the given tokens.
 * Returns null when nothing matches — render the text as is.
 */
export function splitHighlights(text: string, tokens: string[]): { text: string; hit: boolean }[] | null {
  if (tokens.length === 0) return null;
  const re = new RegExp(`(${tokens.map(escapeRegExp).join("|")})`, "gi");
  const parts = text.split(re);
  if (parts.length === 1) return null;
  return parts.filter(Boolean).map((part) => ({
    text: part,
    hit: tokens.includes(part.toLowerCase()),
  }));
}
