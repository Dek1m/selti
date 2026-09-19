// URL ↔ store sync (§4.2): filters live in ?q=&ns=&status=&period=&project=
// so a result set is a shareable link. Hydrate once on mount, then mirror
// every store change into the URL with replace (no history spam).

import { useEffect } from "react";
import { useSearchParams } from "react-router";
import type { PeriodPreset, StatusFilter } from "../api/selti";
import { useFilters, type FiltersSnapshot } from "../store/filters";

const PERIODS: PeriodPreset[] = ["24h", "7d", "30d", "all"];
const STATUSES: StatusFilter[] = ["asserted", "superseded", "retracted", "uncertain"];

function snapshotToParams({ query, namespaces, status, period, project, page }: FiltersSnapshot): string {
  const next = new URLSearchParams();
  if (query) next.set("q", query);
  namespaces.forEach((n) => next.append("ns", n));
  if (status) next.set("status", status);
  if (period !== "all") next.set("period", period);
  if (project) next.set("project", project);
  if (page > 1) next.set("page", String(page));
  return next.toString();
}

export function useUrlSync(): void {
  const [, setParams] = useSearchParams();

  // Hydrate the store from the URL exactly once (shared-link first paint)
  useEffect(() => {
    const search = new URLSearchParams(window.location.search);
    const status = search.get("status");
    const period = search.get("period");
    useFilters.getState().hydrate({
      query: search.get("q") ?? "",
      namespaces: search.getAll("ns"),
      status: STATUSES.includes(status as StatusFilter) ? (status as StatusFilter) : null,
      period: PERIODS.includes(period as PeriodPreset) ? (period as PeriodPreset) : "all",
      project: search.get("project"),
      page: Math.max(1, parseInt(search.get("page") ?? "1", 10) || 1),
    });
  }, []);

  // Store → URL. Runs after the hydrate effect (declaration order), so the
  // immediate write already sees hydrated state and shared links stay put.
  useEffect(() => {
    const write = (state: FiltersSnapshot) => {
      const nextStr = snapshotToParams(state);
      if (nextStr !== window.location.search.slice(1)) {
        setParams(nextStr ? new URLSearchParams(nextStr) : new URLSearchParams(), { replace: true });
      }
    };
    write(useFilters.getState());
    return useFilters.subscribe(write);
  }, [setParams]);
}
