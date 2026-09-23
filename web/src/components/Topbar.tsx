import { useQuery } from "@tanstack/react-query";
import { NavLink } from "react-router";
import { getStats } from "../api/selti";

/** Shell navigation (§3): brand, tabs, live health cluster on the right. */
export function Topbar() {
  const stats = useQuery({ queryKey: ["stats"], queryFn: getStats, staleTime: 60_000, retry: 1 });
  const total = (stats.data ?? []).reduce((sum, s) => sum + s.count, 0);
  const isErr = stats.isError;

  return (
    <header className="topbar">
      <NavLink to="/search" className="brand" aria-label="selti — на главную">
        <i className="bi bi-bucket-fill" style={{ color: "var(--sl-accent)" }} aria-hidden="true" />
        <span className="brand-name">
          sel<b>ti</b>
        </span>
      </NavLink>
      <nav className="nav" aria-label="Разделы">
        <NavLink to="/search">Поиск</NavLink>
        <NavLink to="/graph">Граф</NavLink>
        <NavLink to="/projects">Проекты</NavLink>
        <NavLink to="/stats">Статистика</NavLink>
        <NavLink to="/settings">Конфигурация</NavLink>
      </nav>
      <div className={`health${isErr ? " err" : ""}`} role="status" aria-live="polite">
        {isErr ? (
          <>
            <span className="pulse-dot" aria-hidden="true" />
            selti не отвечает
          </>
        ) : (
          <>
            <span className="pulse-dot" aria-hidden="true" />
            <span className="num">{total.toLocaleString("ru-RU")}</span> гранул · API ok
          </>
        )}
      </div>
    </header>
  );
}
