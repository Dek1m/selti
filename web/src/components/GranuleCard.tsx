import { memo } from "react";
import type { SearchHit } from "../api/types";
import { namespaceColor } from "../lib/colors";
import { splitHighlights, queryTokens } from "../lib/highlight";
import { factorsFromHit } from "../lib/score";
import { timeAgo } from "../lib/time";
import { NsDot } from "./NsDot";
import { ScoreGauge } from "./ScoreGauge";

function Excerpt({ content, tokens }: { content: string; tokens: string[] }) {
  const parts = splitHighlights(content, tokens);
  if (!parts) return <>{content}</>;
  return (
    <>
      {parts.map((p, i) =>
        p.hit ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>,
      )}
    </>
  );
}

/** Result card (§4.3): spine → content excerpt → score panel. */
export const GranuleCard = memo(function GranuleCard({
  hit,
  query,
  active,
  onOpen,
}: {
  hit: SearchHit;
  query: string;
  active: boolean;
  onOpen: (hit: SearchHit) => void;
}) {
  const tokens = queryTokens(query);
  return (
    <article
      className={`result${active ? " active" : ""}`}
      onClick={() => onOpen(hit)}
      onKeyDown={(e) => e.key === "Enter" && onOpen(hit)}
      tabIndex={0}
      role="link"
      aria-label={`Гранула ${hit.id}`}
    >
      <span className="spine" style={{ background: namespaceColor(hit.namespace) }} aria-hidden="true" />
      <div className="result-body">
        <p className="excerpt">
          <Excerpt content={hit.content} tokens={tokens} />
        </p>
        <div className="result-meta">
          <span className="ns-tag">
            <NsDot uid={hit.namespace} size={7} />
            {hit.namespace ?? "default"}
          </span>
          {hit.project_id && <span>{hit.project_id}</span>}
          <span className="mono">{timeAgo(hit.created_at)}</span>
          <span className="id">{hit.id.slice(0, 8)}</span>
          {hit.frozen && (
            <span className="badge frozen">
              <i className="bi bi-snow" aria-hidden="true" /> frozen
            </span>
          )}
          {hit.status !== "asserted" && <span className={`badge ${hit.status}`}>{hit.status}</span>}
        </div>
      </div>
      <ScoreGauge score={hit.score} factors={factorsFromHit(hit)} />
    </article>
  );
});
