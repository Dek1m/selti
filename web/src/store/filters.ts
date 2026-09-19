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
}

interface FiltersState extends FiltersSnapshot {
  setQuery: (q: string) => void;
  toggleNamespace: (uid: string) => void;
  setStatus: (s: StatusFilter | null) => void;
  setPeriod: (p: PeriodPreset) => void;
  setProject: (slug: string | null) => void;
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
};

export const useFilters = create<FiltersState>((set, get) => ({
  ...INITIAL,
  setQuery: (query) => set({ query }),
  toggleNamespace: (uid) =>
    set((s) => ({
      namespaces: s.namespaces.includes(uid)
        ? s.namespaces.filter((n) => n !== uid)
        : [...s.namespaces, uid],
    })),
  setStatus: (status) => set({ status }),
  setPeriod: (period) => set({ period }),
  setProject: (project) => set({ project: project || null }),
  hydrate: (from) => set(from),
  reset: () => set(INITIAL),
  hasActive: () => {
    const s = get();
    return s.namespaces.length > 0 || s.status !== null || s.period !== "all" || s.project !== null;
  },
}));
