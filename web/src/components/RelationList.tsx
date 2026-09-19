import { useNavigate } from "react-router";
import type { Relation } from "../api/types";
import { linkTypeColor } from "../lib/colors";

function RelationRow({ rel, direction, onPick }: { rel: Relation; direction: "out" | "in"; onPick: (id: string) => void }) {
  // outgoing: arrow to target; incoming: arrow from source
  const peerId = direction === "out" ? rel.target_id : rel.source_id;
  const label = direction === "out" ? (rel.target_name ?? rel.target_id) : rel.target_name ?? rel.source_id;
  if (!peerId) {
    return (
      <div className="rel" style={{ cursor: "default" }}>
        <span className="link-type" style={{ color: linkTypeColor(rel.link_type) }}>
          {rel.link_type}
        </span>
        <span className="target">{label ?? "внешняя сущность"}</span>
      </div>
    );
  }
  return (
    <div>
      <div
        className="rel"
        onClick={() => onPick(peerId)}
        onKeyDown={(e) => e.key === "Enter" && onPick(peerId)}
        tabIndex={0}
        role="link"
        aria-label={`Связь ${rel.link_type} → ${label}`}
      >
        <span className="link-type" style={{ color: linkTypeColor(rel.link_type) }}>
          {rel.link_type}
        </span>
        <span className="target">{label}</span>
        {rel.weight !== 1 && <span className="rel-w">w={rel.weight}</span>}
      </div>
      {rel.description && <div className="rel-desc">{rel.description}</div>}
    </div>
  );
}

/** Relations section (§5.1.6): outgoing → and incoming ← synapses. */
export function RelationList({
  relations,
  direction,
}: {
  relations: Relation[];
  direction: "out" | "in";
}) {
  const navigate = useNavigate();
  const onPick = (id: string) =>
    navigate({ pathname: `/memory/${id}`, search: window.location.search });

  if (relations.length === 0) {
    return <p className="rel-empty">{direction === "out" ? "Исходящих связей нет" : "Входящих связей нет"}</p>;
  }
  return (
    <div>
      {relations.map((rel) => (
        <RelationRow key={rel.id} rel={rel} direction={direction} onPick={onPick} />
      ))}
    </div>
  );
}
