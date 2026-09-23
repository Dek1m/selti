// Typed selti API calls. The REST layer accepts a single `namespace`
// per search request, while the UI offers multi-select (OR semantics,
// WEB_UI_DESIGN §4.2) — so multiple selections fan out into parallel
// requests merged client-side (dedup by id, re-sorted by score).

import { apiGet } from "./client";
import type {
  HealthPayload,
  MemoryDetail,
  NamespaceInfo,
  NamespaceStat,
  ProjectCard,
  ProjectContext,
  ProjectDetail,
  RelationsPayload,
  SearchHit,
} from "./types";

/** Page size for /ui pagination — the backend ranks offset+limit
 * deterministically, so pages are stable across requests. */
export const PAGE_SIZE = 20;

export type StatusFilter = "asserted" | "superseded" | "retracted" | "uncertain";
export type PeriodPreset = "24h" | "7d" | "30d" | "all";

const PERIOD_MS: Record<Exclude<PeriodPreset, "all">, number> = {
  "24h": 24 * 3600_000,
  "7d": 7 * 24 * 3600_000,
  "30d": 30 * 24 * 3600_000,
};

export interface SearchFilters {
  query: string;
  namespaces: string[];
  project: string | null;
  status: StatusFilter | null;
  /** superseded/retracted live behind the historical flag on the backend */
  includeHistorical: boolean;
  period: PeriodPreset;
}

export interface SearchOutcome {
  results: SearchHit[];
  tookMs: number;
}

function searchParams(
  f: SearchFilters,
  namespace: string | null,
  offset: number,
  limit: number,
  withPositions: boolean,
): URLSearchParams {
  const p = new URLSearchParams({ query: f.query, limit: String(limit), offset: String(offset) });
  if (namespace) p.set("namespace", namespace);
  if (f.project) p.set("project_id", f.project);
  if (f.status) p.set("status", f.status);
  if (f.includeHistorical) p.set("include_historical", "true");
  if (withPositions) p.set("with_positions", "true");
  if (f.period !== "all") {
    p.set("created_after", new Date(Date.now() - PERIOD_MS[f.period]).toISOString());
  }
  return p;
}

/** Merge fan-out results: dedup by id, keep top by score. */
export function mergeHits(lists: SearchHit[][]): SearchHit[] {
  const byId = new Map<string, SearchHit>();
  for (const list of lists) {
    for (const hit of list) {
      const prev = byId.get(hit.id);
      if (!prev || hit.score > prev.score) byId.set(hit.id, hit);
    }
  }
  return [...byId.values()].sort((a, b) => b.score - a.score);
}

/** withPositions — карта map_layout на хитах: созвездие ставит звёзды в те же точки, что и полная карта */
export async function searchGranules(f: SearchFilters, page = 1, withPositions = false): Promise<SearchOutcome> {
  const t0 = performance.now();
  const offset = (page - 1) * PAGE_SIZE;
  const scopes = f.namespaces.length > 0 ? f.namespaces : [null];
  if (scopes.length === 1) {
    // Single channel: the backend slices its deterministic ranking.
    const results = await apiGet<SearchHit[]>(
      "/api/search",
      searchParams(f, scopes[0], offset, PAGE_SIZE, withPositions),
    );
    return { results, tookMs: performance.now() - t0 };
  }
  // Fan-out: each channel returns its own [0, offset+PAGE_SIZE) head, the
  // merged ranking is sliced afterwards — naive per-channel offsets would
  // skip different heads and double pages across channels.
  const fetches = scopes.map((ns) =>
    apiGet<SearchHit[]>("/api/search", searchParams(f, ns, 0, offset + PAGE_SIZE, withPositions)),
  );
  const lists = await Promise.all(fetches);
  return {
    results: mergeHits(lists).slice(offset, offset + PAGE_SIZE),
    tookMs: performance.now() - t0,
  };
}

export function getMemory(id: string, withPositions = false): Promise<MemoryDetail> {
  const params = new URLSearchParams({ include_history: "true" });
  if (withPositions) params.set("with_positions", "true");
  return apiGet<MemoryDetail>(`/api/memories/${encodeURIComponent(id)}`, params);
}

export function getRelations(id: string, withPositions = false): Promise<RelationsPayload> {
  const params = withPositions ? new URLSearchParams({ with_positions: "true" }) : undefined;
  return apiGet<RelationsPayload>(`/api/memories/${encodeURIComponent(id)}/relations`, params);
}

export function getSimilar(id: string): Promise<SearchHit[]> {
  return apiGet<SearchHit[]>(`/api/memories/${encodeURIComponent(id)}/similar`);
}

export function getNamespaces(): Promise<NamespaceInfo[]> {
  return apiGet<NamespaceInfo[]>("/api/namespaces");
}

export function getStats(): Promise<NamespaceStat[]> {
  return apiGet<NamespaceStat[]>("/api/stats");
}

export function getProjects(): Promise<{ projects: ProjectCard[] }> {
  return apiGet<{ projects: ProjectCard[] }>("/api/projects");
}

export function getProject(slug: string): Promise<ProjectDetail> {
  return apiGet<ProjectDetail>(`/api/projects/${encodeURIComponent(slug)}`);
}

export function getContext(slug: string): Promise<ProjectContext> {
  return apiGet<ProjectContext>(`/api/contexts/${encodeURIComponent(slug)}`);
}

export function getHealth(): Promise<HealthPayload> {
  return apiGet<HealthPayload>("/health");
}
