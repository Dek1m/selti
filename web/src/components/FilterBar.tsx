import { useQuery } from "@tanstack/react-query";
import { getNamespaces, getProjects, getStats } from "../api/selti";
import type { PeriodPreset, StatusFilter } from "../api/selti";
import { namespaceColor } from "../lib/colors";
import { useFilters } from "../store/filters";
import { NsDot } from "./NsDot";

const PERIODS: { key: PeriodPreset; label: string }[] = [
  { key: "24h", label: "24ч" },
  { key: "7d", label: "7д" },
  { key: "30d", label: "30д" },
  { key: "all", label: "всё" },
];

const STATUSES: { key: StatusFilter | null; label: string }[] = [
  { key: null, label: "живые" },
  { key: "superseded", label: "superseded" },
  { key: "retracted", label: "retracted" },
  { key: "uncertain", label: "uncertain" },
];

/**
 * Filter chips (§4.2). Namespace chips are multi-select (OR) with the
 * spectrum dot; the rest are segmented single-select. Status is single
 * because the REST parameter is singular.
 */
export function FilterBar() {
  const { namespaces, status, period, project, toggleNamespace, setStatus, setPeriod, setProject } =
    useFilters();

  const nsQuery = useQuery({ queryKey: ["namespaces"], queryFn: getNamespaces, staleTime: 5 * 60_000 });
  const statsQuery = useQuery({ queryKey: ["stats"], queryFn: getStats, staleTime: 60_000 });
  const projectsQuery = useQuery({ queryKey: ["projects"], queryFn: getProjects, staleTime: 60_000 });

  const counts = new Map((statsQuery.data ?? []).map((s) => [s.namespace, s.count]));

  return (
    <div className="filters" role="group" aria-label="Фильтры поиска">
      {(nsQuery.data ?? []).map((ns) => {
        const pressed = namespaces.includes(ns.uid);
        return (
          <button
            key={ns.uid}
            className="chip"
            aria-pressed={pressed}
            title={ns.description ?? ns.uid}
            style={{ ["--chip-hue" as string]: namespaceColor(ns.uid) }}
            onClick={() => toggleNamespace(ns.uid)}
          >
            <NsDot uid={ns.uid} />
            {ns.name}
            <span className="count">{counts.get(ns.uid) ?? ""}</span>
          </button>
        );
      })}

      <select
        className="chip"
        aria-label="Проект"
        value={project ?? ""}
        onChange={(e) => setProject(e.target.value)}
      >
        <option value="">все проекты</option>
        {(projectsQuery.data?.projects ?? []).map((p) => (
          <option key={p.id} value={p.slug}>
            {p.slug}
          </option>
        ))}
      </select>

      {PERIODS.map((p) => (
        <button
          key={p.key}
          className="chip"
          aria-pressed={period === p.key}
          onClick={() => setPeriod(p.key)}
        >
          {p.label}
        </button>
      ))}

      {STATUSES.map((s) => (
        <button
          key={s.label}
          className="chip"
          aria-pressed={status === s.key}
          onClick={() => setStatus(s.key)}
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}
