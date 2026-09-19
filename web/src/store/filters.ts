// Filter state (WEB_UI_DESIGN §4.2): the store is the single source of
// truth while the screen is mounted; useUrlSync mirrors it into the URL
// for shareable deep links. Status is single-select because the REST
// parameter is singular — multi-select lands when the API accepts arrays.

import { create } from "zustand";
import type { PeriodPreset, StatusFilter } from "../api/selti";

export interface FiltersSnapshot {
  query: string;
  namespaces: string[];
  status: StatusFilter | null;
  period: PeriodPreset;
  project: string | null;
  /** 1-based; any filter change snaps it back to the first page */
  page: number;
}

interface FiltersState extends FiltersSnapshot {
  setQuery: (q: string) => void;
  toggleNamespace: (uid: string) => void;
  setStatus: (s: StatusFilter | null) => void;
  setPeriod: (p: PeriodPreset) => void;
  setProject: (slug: string | null) => void;
  setPage: (page: number) => void;
  hydrate: (from: Partial<FiltersSnapshot>) => void;
  reset: () => void;
  hasActive: () => boolean;
}

const INITIAL: FiltersSnapshot = {
  query: "",
  namespaces: [],
  status: null,
  period: "all",
  project: null,
  page: 1,
};

export const useFilters = create<FiltersState>((set, get) => ({
  ...INITIAL,
  setQuery: (query) => set({ query, page: 1 }),
  toggleNamespace: (uid) =>
    set((s) => ({
      namespaces: s.namespaces.includes(uid)
        ? s.namespaces.filter((n) => n !== uid)
        : [...s.namespaces, uid],
      page: 1,
    })),
  setStatus: (status) => set({ status, page: 1 }),
  setPeriod: (period) => set({ period, page: 1 }),
  setProject: (project) => set({ project: project || null, page: 1 }),
  setPage: (page) => set({ page: Math.max(1, page) }),
  hydrate: (from) => set(from),
  reset: () => set(INITIAL),
  hasActive: () => {
    const s = get();
    return s.namespaces.length > 0 || s.status !== null || s.period !== "all" || s.project !== null;
  },
}));
