import { useQuery } from "@tanstack/react-query";
import { getHealth, getStats } from "../api/selti";
import type { HealthPayload, NamespaceStat } from "../api/types";
import { namespaceColor } from "../lib/colors";
import { timeAgo } from "../lib/time";

/** One spectral stack bar: segments sized by granule share (§8, row 2). */
export function SpectrumBar({ stats, label }: { stats: NamespaceStat[]; label: string }) {
  const total = stats.reduce((sum, s) => sum + s.count, 0);
  if (total === 0) return null;
  return (
    <div
      className="spectrum-bar"
      role="img"
      aria-label={`${label}: ${total.toLocaleString("ru-RU")} гранул в ${stats.length} слоях`}
    >
      {stats.map((s) => (
        <span
          key={s.namespace}
          className="spectrum-seg"
          style={{ width: `${(s.count / total) * 100}%`, background: namespaceColor(s.namespace) }}
          title={`${s.namespace}: ${s.count.toLocaleString("ru-RU")}`}
        />
      ))}
    </div>
  );
}

function MetricCard({ icon, value, label, hint }: { icon: string; value: string; label: string; hint?: string }) {
  return (
    <div className="metric-card">
      <div className="metric-icon">
        <i className={`bi ${icon}`} aria-hidden="true" />
      </div>
      <span className="metric-value">{value}</span>
      <span className="metric-label">{label}</span>
      {hint && <span className="metric-hint">{hint}</span>}
    </div>
  );
}

function HealthRow({ name, value }: { name: string; value: string | undefined }) {
  const ok = value !== undefined && (value === "ok" || value.startsWith("ok"));
  return (
    <div className="health-row">
      <span className="health-name">{name}</span>
      <span className={`health-val${ok ? "" : " bad"}`}>{value ?? "—"}</span>
    </div>
  );
}

function HealthCard({ health }: { health?: HealthPayload }) {
  if (!health) return null;
  const degraded = health.status !== "ok";
  return (
    <section className="stats-card health-card" aria-label="Здоровье сервиса">
      <h3 className="stats-card-title">
        <i className={`bi ${degraded ? "bi-exclamation-triangle" : "bi-heart-pulse"}`} aria-hidden="true" />
        Сервис
      </h3>
      <div className={`health-status${degraded ? " degraded" : ""}`}>
        <span className="pulse-dot" aria-hidden="true" />
        {health.status}
        <span className="mono health-version">v{health.version}</span>
      </div>
      <div className="health-checks">
        <HealthRow name="postgres" value={health.checks.postgres} />
        <HealthRow name="redis" value={health.checks.redis} />
        <HealthRow name="celery" value={health.checks.celery} />
      </div>
    </section>
  );
}

/** /ui/stats — depth dashboard: totals, namespace spectrum, service health. */
export function StatsScreen() {
  const stats = useQuery({ queryKey: ["stats"], queryFn: getStats, staleTime: 60_000 });
  const health = useQuery({ queryKey: ["health"], queryFn: getHealth, staleTime: 30_000, retry: 1 });

  const rows = [...(stats.data ?? [])].sort((a, b) => b.count - a.count);
  const total = rows.reduce((sum, s) => sum + s.count, 0);
  const maxCount = rows[0]?.count ?? 0;
  const freshest = rows.reduce<string | null>(
    (acc, s) => (!acc || (s.last_updated && s.last_updated > acc) ? s.last_updated : acc),
    null,
  );

  return (
    <div className="stage stats-stage">
      <h2 className="screen-title">Глубина памяти</h2>

      {stats.isPending ? (
        <div className="stats-grid" aria-hidden="true">
          <div className="sk" style={{ height: 120 }} />
          <div className="sk" style={{ height: 120 }} />
          <div className="sk" style={{ height: 120 }} />
        </div>
      ) : stats.isError ? (
        <div className="state-block error">
          <i className="bi bi-wifi-off" aria-hidden="true" />
          <h3>Статистика недоступна</h3>
          <p>{(stats.error as Error).message}</p>
          <button className="btn" onClick={() => void stats.refetch()}>
            <i className="bi bi-arrow-clockwise" aria-hidden="true" /> Повторить
          </button>
        </div>
      ) : (
        <>
          <div className="stats-grid">
            <MetricCard
              icon="bi-database"
              value={total.toLocaleString("ru-RU")}
              label="гранул всего"
              hint={`в ${rows.length} слоях памяти`}
            />
            <MetricCard
              icon="bi-layers"
              value={String(rows.length)}
              label="namespace"
              hint={rows.length > 0 ? "спектр слоёв" : undefined}
            />
            <MetricCard
              icon="bi-lightning-charge"
              value={freshest ? timeAgo(freshest) : "—"}
              label="последняя запись"
              hint="обновление слоя"
            />
            <HealthCard health={health.data} />
          </div>

          {rows.length > 0 && (
            <section className="stats-card" aria-label="Распределение по namespace">
              <h3 className="stats-card-title">
                <i className="bi bi-rainbow" aria-hidden="true" /> Спектр
              </h3>
              <SpectrumBar stats={rows} label="Распределение гранул" />
              <div className="ns-bars">
                {rows.map((s) => (
                  <div className="ns-bar-row" key={s.namespace}>
                    <span className="ns-tag">
                      <span className="dot" style={{ background: namespaceColor(s.namespace) }} />
                      {s.namespace}
                    </span>
                    <div className="ns-bar-track">
                      <i
                        className="ns-bar-fill"
                        style={{
                          width: maxCount > 0 ? `${(s.count / maxCount) * 100}%` : "0%",
                          background: namespaceColor(s.namespace),
                          color: namespaceColor(s.namespace),
                        }}
                      />
                    </div>
                    <span className="ns-bar-count">{s.count.toLocaleString("ru-RU")}</span>
                    <span className="ns-bar-time">{timeAgo(s.last_updated)}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
