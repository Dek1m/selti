import { afterEach, describe, expect, it, vi } from "vitest";
import { apiGet, buildUrl } from "./client";

describe("buildUrl", () => {
  it("appends query params with a separator only when present", () => {
    expect(buildUrl("/api/search")).toBe("/api/search");
    expect(buildUrl("/api/search", new URLSearchParams({ q: "деплой" }))).toBe(
      "/api/search?q=%D0%B4%D0%B5%D0%BF%D0%BB%D0%BE%D0%B9",
    );
  });

  it("honors the env base URL", () => {
    vi.stubEnv("VITE_SELTI_API_BASE", "https://selti.example");
    expect(buildUrl("/api/stats")).toBe("https://selti.example/api/stats");
    vi.unstubAllEnvs();
  });
});

describe("apiGet", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("sends the bearer header and parses JSON", async () => {
    vi.stubEnv("VITE_SELTI_TOKEN", "tok123");
    const fetchMock = vi.fn(async () => new Response('{"ok": true}', { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiGet("/api/namespaces")).resolves.toEqual({ ok: true });
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok123");
  });

  it("sends no auth header without a token (localhost dev)", async () => {
    const fetchMock = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiGet("/api/namespaces")).resolves.toEqual([]);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
  });

  it("throws ApiError with backend detail on non-2xx", async () => {
    const fetchMock = vi.fn(async () => new Response('{"detail": "not found"}', { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiGet("/api/memories/xyz")).rejects.toMatchObject({
      status: 404,
      message: "not found",
    });
  });

  it("falls back to statusText for non-JSON error bodies", async () => {
    const fetchMock = vi.fn(async () => new Response("<html>", { status: 502, statusText: "Bad Gateway" }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiGet("/api/stats")).rejects.toMatchObject({ status: 502, message: "Bad Gateway" });
  });
});
