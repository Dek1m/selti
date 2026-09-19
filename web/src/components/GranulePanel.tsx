import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { getMemory, getRelations, getSimilar } from "../api/selti";
import type { MemoryRecord, SearchHit } from "../api/types";
import { decomposeScore, factorsFromHit, formatScore } from "../lib/score";
import { formatDate, timeAgo } from "../lib/time";
import { LineageChain } from "./LineageChain";
import { NsDot } from "./NsDot";
import { RelationList } from "./RelationList";
import { FACTOR_COLORS } from "./ScoreGauge";

function ImportanceDots({ value }: { value: number }) {
  // importance grains (§5.1.4): 5 dots, filled = weight
  return (
    <span className="dots" aria-label={`importance ${value} из 5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <span key={i} className={i <= value ? "" : "off"}>
          ●
        </span>
      ))}
    </span>
  );
}

function MetaTable({ record }: { record: MemoryRecord }) {
  const extra = Object.entries(record.metadata)
    .filter(([key]) => !["entity_name", "entity_type", "title"].includes(key))
    .slice(0, 8);
  const rows: [string, React.ReactNode][] = [
    ["importance", <ImportanceDots key="imp" value={record.importance} />],
    ["user", record.user_id],
    ["project", record.project_id ?? "—"],
    ["namespace", record.namespace],
    ["entity_name", String(record.metadata.entity_name ?? "—")],
    ["entity_type", String(record.metadata.entity_type ?? "—")],
    ["created", formatDate(record.created_at)],
    ["updated", formatDate(record.updated_at)],
    ["confidence", record.confidence.toFixed(2)],
    ["access", `${record.access_count} · ${timeAgo(record.last_accessed_at)}`],
    ...extra.map(([key, value]): [string, React.ReactNode] => [key, typeof value === "object" ? JSON.stringify(value) : String(value)]),
  ];
  return (
    <table className="meta-table">
      <tbody>
        {rows.map(([key, value]) => (
          <tr key={key}>
            <td>{key}</td>
            <td>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SimilarSection({ id }: { id: string }) {
  const navigate = useNavigate();
  const similar = useQuery({ queryKey: ["similar", id], queryFn: () => getSimilar(id) });
  if (similar.isPending) return <p className="rel-empty">Ищу похожие гранулы…</p>;
  if (similar.isError) return <p className="rel-empty">Похожие не загрузились: {(similar.error as Error).message}</p>;
  if ((similar.data ?? []).length === 0) return <p className="rel-empty">Похожих гранул нет.</p>;
  return (
    <div className="similar-list">
      {(similar.data ?? []).map((hit: SearchHit) => (
        <div
          key={hit.id}
          className="similar-item"
          role="link"
          tabIndex={0}
          onClick={() => navigate({ pathname: `/memory/${hit.id}`, search: window.location.search })}
          onKeyDown={(e) => e.key === "Enter" && navigate({ pathname: `/memory/${hit.id}`, search: window.location.search })}
        >
          <p className="excerpt">{hit.content}</p>
          <div className="sim-meta">
            <span className="ns-tag">
              <NsDot uid={hit.namespace} size={7} />
              {hit.namespace ?? "default"}
            </span>
            <span className="mono" style={{ color: "var(--sl-accent)" }}>
              {formatScore(hit.score)}
            </span>
            <span>{timeAgo(hit.created_at)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * Granule panel (§5): sticky right side next to results, or centered
 * page on a direct /memory/:id link. Esc closes back to the search.
 */
export function GranulePanel({ id, onClose, centered = false }: { id: string; onClose: () => void; centered?: boolean }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [showSimilar, setShowSimilar] = useState(false);
  const [copied, setCopied] = useState(false);

  // Score decomposition renders when opened from results (§5.1.3)
  const hit = (location.state as { hit?: SearchHit } | null)?.hit ?? null;

  const memory = useQuery({ queryKey: ["memory", id], queryFn: () => getMemory(id) });
  const relations = useQuery({ queryKey: ["relations", id], queryFn: () => getRelations(id) });

  useEffect(() => setShowSimilar(false), [id]);

  // Esc closes the card (§9); switching versions swaps the route
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const record = memory.data;
  const history = record?.history;

  const copyJson = async () => {
    if (!record) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(record, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard may be denied — silent no-op, the button just stays unchanged
    }
  };

  const headBadges = (
    <>
      {record && (
        <span className={`badge ${record.status}`}>
          {record.frozen ? "frozen ❄" : record.status}
        </span>
      )}
    </>
  );

  return (
    <aside
      className={`panel${centered ? " panel-centered" : ""}`}
      role="complementary"
      aria-label="Карточка гранулы"
    >
      <div className="panel-head">
        {record && (
          <span className="ns-tag">
            <NsDot uid={record.namespace} />
            {record.namespace}
          </span>
        )}
        {headBadges}
        <span className="id">{id}</span>
        <button className="icon-btn close" aria-label="Закрыть карточку (Esc)" onClick={onClose}>
          <i className="bi bi-x-lg" aria-hidden="true" />
        </button>
      </div>

      <div className="panel-body">
        {memory.isPending ? (
          <div style={{ display: "grid", gap: 12 }} aria-hidden="true">
            <div className="sk" style={{ height: 48 }} />
            <div className="sk" style={{ height: 120 }} />
            <div className="sk" style={{ height: 160 }} />
          </div>
        ) : memory.isError ? (
          <div className="state-block error">
            <i className="bi bi-slash-circle" aria-hidden="true" />
            <h3>Гранула не найдена</h3>
            <p>Возможно, она удалена GC. {(memory.error as Error).message}</p>
          </div>
        ) : record ? (
          <>
            <section className="section">
              <p className="content">{record.content}</p>
              {record.superseded_by && (
                <div className="superseded-note">
                  <i className="bi bi-clock-history" aria-hidden="true" />
                  Это устаревшая версия →{" "}
                  <a
                    href="#"
                    onClick={(e) => {
                      e.preventDefault();
                      navigate(
                        { pathname: `/memory/${record.superseded_by}`, search: window.location.search },
                        { replace: true },
                      );
                    }}
                  >
                    актуальная
                  </a>
                </div>
              )}
            </section>

            {hit && (
              <section className="section">
                <h4 className="section-label">Релевантность</h4>
                <div className="score-detail">
                  <span className="val">{formatScore(hit.score)}</span>
                  <div className="factor-list">
                    {decomposeScore(factorsFromHit(hit)).map((f) => (
                      <div key={f.key} className="factor">
                        <span className="name">{f.key}</span>
                        <span className="track">
                          <i style={{ width: `${f.norm * 100}%`, background: FACTOR_COLORS[f.key] }} />
                        </span>
                        <span className="v">{f.raw === null ? "—" : f.raw.toFixed(3)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </section>
            )}

            <section className="section">
              <h4 className="section-label">Метаданные</h4>
              <MetaTable record={record} />
            </section>

            <section className="section">
              <h4 className="section-label">
                <i className="bi bi-clock-history" aria-hidden="true" /> История версий
                {history ? ` · ${history.items.length}` : ""}
              </h4>
              {history && (
                <LineageChain
                  items={history.items}
                  currentId={history.current_id}
                  viewingId={id}
                  onPick={(versionId) =>
                    navigate(
                      { pathname: `/memory/${versionId}`, search: window.location.search },
                      { replace: true },
                    )
                  }
                />
              )}
            </section>

            <section className="section">
              <h4 className="section-label">
                <i className="bi bi-link-45deg" aria-hidden="true" /> Связи
              </h4>
              {relations.isPending ? (
                <p className="rel-empty">Грузлю связи…</p>
              ) : relations.isError ? (
                <p className="rel-empty">Связи не загрузились: {(relations.error as Error).message}</p>
              ) : (
                <>
                  <div className="rel-group">
                    <div className="rel-title">
                      <span className="arr">→</span> Исходящие ({relations.data?.outgoing.length ?? 0})
                    </div>
                    <RelationList relations={relations.data?.outgoing ?? []} direction="out" />
                  </div>
                  <div className="rel-group">
                    <div className="rel-title">
                      <span className="arr">←</span> Входящие ({relations.data?.incoming.length ?? 0})
                    </div>
                    <RelationList relations={relations.data?.incoming ?? []} direction="in" />
                  </div>
                </>
              )}
            </section>

            {showSimilar && (
              <section className="section">
                <h4 className="section-label">
                  <i className="bi bi-stars" aria-hidden="true" /> Похожие
                </h4>
                <SimilarSection id={id} />
              </section>
            )}
          </>
        ) : null}
      </div>

      <div className="panel-actions">
        <button className="btn" onClick={() => void copyJson()} disabled={!record}>
          <i className="bi bi-clipboard" aria-hidden="true" /> {copied ? "Скопировано" : "Копировать JSON"}
        </button>
        <button className="btn primary" onClick={() => setShowSimilar((v) => !v)}>
          <i className="bi bi-stars" aria-hidden="true" /> {showSimilar ? "Скрыть похожие" : "Показать похожие"}
        </button>
        <a className="btn" href="/graph" aria-disabled="true" title="Экран «Граф» — в следующей итерации">
          <i className="bi bi-diagram-3" aria-hidden="true" /> Открыть в графе
        </a>
      </div>
    </aside>
  );
}
