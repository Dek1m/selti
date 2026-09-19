import type { MemoryRecord } from "../api/types";

/**
 * Version lineage (§5.1.5): v1 ──supersedes──▶ v2 ──▶ v3 (asserted).
 * Items come oldest → newest; chains longer than 5 collapse the middle.
 */
export function LineageChain({
  items,
  currentId,
  viewingId,
  onPick,
}: {
  items: MemoryRecord[];
  currentId: string | null;
  viewingId: string;
  onPick: (id: string) => void;
}) {
  if (items.length <= 1) {
    return <p className="rel-empty">Одна версия — цепочки нет.</p>;
  }

  const nodes = items.map((item, idx) => ({
    item,
    v: idx + 1,
    isCurrent: item.id === currentId,
    isViewing: item.id === viewingId,
  }));

  // Compact mode for long chains: v1 … v(n-1) vN (§5.1.5)
  const collapsed = nodes.length > 5;
  const visible = collapsed ? [nodes[0], nodes[nodes.length - 1]] : nodes;

  return (
    <div className="lineage" role="list" aria-label={`Цепочка версий, ${items.length}`}>
      {visible.map((node, i) => (
        <div key={node.item.id} style={{ display: "contents", }}>
          {i > 0 && <span className="lin-link" aria-hidden="true" />}
          {i > 0 && collapsed && <span className="lin-gap" aria-hidden="true">…</span>}
          {i > 0 && collapsed && <span className="lin-link" aria-hidden="true" />}
          <div
            className={`lin-node${node.isCurrent ? " current" : ""}${node.isViewing ? " viewing" : ""}`}
            role="listitem"
          >
            <button
              className="circle"
              onClick={() => onPick(node.item.id)}
              aria-label={`Версия v${node.v}${node.isCurrent ? ", актуальная" : ""}${node.item.frozen ? ", frozen" : ""}`}
            >
              v{node.v}
            </button>
            <span className="cap">
              {node.item.frozen && (
                <>
                  <span className="snow">
                    <i className="bi bi-snow" aria-hidden="true" />
                  </span>{" "}
                </>
              )}
              {node.isCurrent ? "актуальная" : node.item.status}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
