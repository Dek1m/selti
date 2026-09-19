import { describe, expect, it } from "vitest";
import { ladderHue, namespaceColor } from "./colors";
import { queryTokens, splitHighlights } from "./highlight";
import { timeAgo } from "./time";

describe("namespace spectrum", () => {
  it("maps known layers to --sl-ns-* tokens", () => {
    expect(namespaceColor("user_facts")).toBe("var(--sl-ns-user-facts)");
    expect(namespaceColor("infrastructure")).toBe("var(--sl-ns-infrastructure)");
  });

  it("falls back to the deterministic ladder for unknown uids", () => {
    expect(namespaceColor("some_new_ns")).toBe(`hsl(${ladderHue("some_new_ns")} 70% 70%)`);
    expect(namespaceColor(null)).toBe("var(--sl-ns-default)");
  });

  it("ladder hue is stable and within 0..330", () => {
    expect(ladderHue("abc")).toBe(ladderHue("abc"));
    expect(ladderHue("xyz")).toBeLessThanOrEqual(330);
    expect(ladderHue("xyz")).toBeGreaterThanOrEqual(0);
  });
});

describe("query highlighting", () => {
  it("extracts meaningful tokens only", () => {
    expect(queryTokens("Деплой фазы 2 — деплой")).toEqual(["деплой", "фазы"]);
  });

  it("splits text into hit/miss segments, case-insensitive", () => {
    const parts = splitHighlights("Деплой Фазы 2 завершён", ["деплой"])!;
    expect(parts.map((p) => p.hit)).toEqual([true, false]);
    expect(parts[0].text).toBe("Деплой");
  });

  it("returns null when nothing matches", () => {
    expect(splitHighlights("тишина", ["шум"])).toBeNull();
  });
});

describe("timeAgo", () => {
  it("formats relative intervals in russian", () => {
    expect(timeAgo(null)).toBe("—");
    const at = (minAgo: number) => new Date(Date.now() - minAgo * 60_000).toISOString();
    expect(timeAgo(at(0.5))).toBe("только что");
    expect(timeAgo(at(12))).toBe("12 мин назад");
    expect(timeAgo(at(5 * 60))).toBe("5 ч назад");
    expect(timeAgo(at(3 * 24 * 60))).toBe("3 д назад");
  });
});
