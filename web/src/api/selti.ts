// Typed selti API calls. The REST layer accepts a single `namespace`
// per search request, while the UI offers multi-select (OR semantics,
// WEB_UI_DESIGN §4.2) — so multiple selections fan out into parallel
// requests merged client-side (dedup by id, re-sorted by score).

import { apiGet } from "./client";
import type {
  MemoryDetail,
  NamespaceInfo,
  NamespaceStat,
  ProjectCard,
  RelationsPayload,
  SearchHit,
} from "./types";

/** No REST offset yet — one shot per query, bounded by the API hard-cap */
export const SEARCH_LIMIT = 50;

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

function searchParams(f: SearchFilters, namespace: string | null): URLSearchParams {
  const p = new URLSearchParams({ q: f.query, limit: String(SEARCH_LIMIT) });
  if (namespace) p.set("namespace", namespace);
  if (f.project) p.set("project_id", f.project);
  if (f.status) p.set("status", f.status);
  if (f.includeHistorical) p.set("include_historical", "true");
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
  return [...byId.values()].sort((a, b) => b.score - a.score).slice(0, SEARCH_LIMIT);
}

export async function searchGranules(f: SearchFilters): Promise<SearchOutcome> {
  const t0 = performance.now();
  const scopes = f.namespaces.length > 0 ? f.namespaces : [null];
  const lists = await Promise.all(
    scopes.map((ns) => apiGet<SearchHit[]>("/api/search", searchParams(f, ns))),
  );
  return { results: mergeHits(lists), tookMs: performance.now() - t0 };
}

export function getMemory(id: string): Promise<MemoryDetail> {
  return apiGet<MemoryDetail>(`/api/memories/${encodeURIComponent(id)}`, new URLSearchParams({ include_history: "true" }));
}

export function getRelations(id: string): Promise<RelationsPayload> {
  return apiGet<RelationsPayload>(`/api/memories/${encodeURIComponent(id)}/relations`);
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
