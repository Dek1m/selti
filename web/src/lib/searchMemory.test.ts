// Юниты searchMemory: roundtrip, стирание пустой строкой, SSR-безопасность.
// vitest гоняет node-окружение — sessionStorage нет, подменяем заглушкой.

import { afterEach, describe, expect, it, vi } from "vitest";
import { getSearchQuery, setSearchQuery } from "./searchMemory";

function stubSessionStorage(): Map<string, string> {
  const store = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  });
  return store;
}

function stubThrowingSessionStorage(): void {
  vi.stubGlobal("sessionStorage", {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
    removeItem: () => {
      throw new Error("denied");
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("searchMemory", () => {
  it("set → get roundtrip", () => {
    const store = stubSessionStorage();
    setSearchQuery("кластеры Level 2");
    expect(getSearchQuery()).toBe("кластеры Level 2");
    expect(store.get("selti.search.query")).toBe("кластеры Level 2");
  });

  it("пустая строка стирает ключ, а не пишет пустоту", () => {
    const store = stubSessionStorage();
    setSearchQuery("деплой");
    setSearchQuery("");
    expect(store.has("selti.search.query")).toBe(false);
    expect(getSearchQuery()).toBe("");
  });

  it("get без сохранённого ключа — пустая строка", () => {
    stubSessionStorage();
    expect(getSearchQuery()).toBe("");
  });

  it("get при недоступном хранилище (SSR) — пустая строка без исключения", () => {
    stubThrowingSessionStorage();
    expect(getSearchQuery()).toBe("");
  });

  it("set при недоступном хранилище (SSR) — молча не падает", () => {
    stubThrowingSessionStorage();
    expect(() => setSearchQuery("деплой")).not.toThrow();
    expect(() => setSearchQuery("")).not.toThrow();
  });
});
