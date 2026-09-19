// /ui/graph — the constellation (§6). WebGL via sigma, laid out by
// forceatlas2. Lazy-loaded: this module pulls graphology + sigma into
// their own chunk so the search bundle stays lean.

import { useQueries, useQuery } from "@tanstack/react-query";
import Graph from "graphology";
import { circular } from "graphology-layout";
import forceAtlas2 from "graphology-layout-forceatlas2";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { SigmaContainer, useLoadGraph, useRegisterEvents } from "@react-sigma/core";
import "@react-sigma/core/lib/style.css";
import { getRelations, searchGranules, type SearchFilters } from "../api/selti";
import { GranulePanel } from "../components/GranulePanel";
import { namespaceColor, resolveCssColor } from "../lib/colors";
import { buildGraphModel, edgeThickness, nodeSize, type GraphModel } from "../lib/graph";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

/** Deterministic PRNG — "recalculate layout" reseeds the FA2 start. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Model → renderable graph: circular seed positions + seeded jitter,
 * then synchronous forceatlas2 (≤120 nodes — sub-frame on the main thread). */
function toSigmaGraph(model: GraphModel, layoutSeed: number): Graph {
  const graph = new Graph({ multi: false, type: "directed" });
  const nodeColor = (ns: string | null) => resolveCssColor(namespaceColor(ns));
  for (const node of model.nodes) {
    graph.addNode(node.id, {
      label: node.label,
      size: nodeSize(node),
      color: nodeColor(node.namespace),
      namespace: node.namespace ?? "",
      seed: node.seed,
      x: 0,
      y: 0,
    });
  }
  for (const edge of model.edges) {
    // parallel pair (different link types) keeps the first line only
    if (!graph.hasEdge(edge.source, edge.target)) {
      graph.addEdge(edge.source, edge.target, {
        size: edgeThickness(edge.weight),
        color: resolveCssColor("var(--sl-border-strong)"),
      });
    }
  }
  circular.assign(graph, { scale: 120 });
  const rng = mulberry32(layoutSeed);
  graph.forEachNode((id) => {
    graph.setNodeAttribute(id, "x", graph.getNodeAttribute(id, "x") + (rng() - 0.5) * 80);
    graph.setNodeAttribute(id, "y", graph.getNodeAttribute(id, "y") + (rng() - 0.5) * 80);
  });
  forceAtlas2.assign(graph, {
    iterations: 120,
    settings: { barnesHutOptimize: true, adjustSizes: true, scalingRatio: 8, gravity: 0.35, slowDown: 4 },
  });
  return graph;
}

/** Sigma wiring: load the graph instance, forward node clicks. */
function GraphEffects({ graph, onSelect, onHover }: {
  graph: Graph;
  onSelect: (id: string | null) => void;
  onHover: (id: string | null) => void;
}) {
  const loadGraph = useLoadGraph();
  const registerEvents = useRegisterEvents();

  useEffect(() => {
    loadGraph(graph, true);
  }, [graph, loadGraph]);

  useEffect(() => {
    registerEvents({
      clickNode: ({ node }) => onSelect(node),
      clickStage: () => onSelect(null),
      enterNode: ({ node }) => onHover(node),
      leaveNode: () => onHover(null),
    });
  }, [registerEvents, onSelect, onHover]);

  return null;
}

const GRAPH_FILTERS: SearchFilters = {
  query: "",
  namespaces: [],
  project: null,
  status: null,
  includeHistorical: false,
  period: "all",
};

export function GraphScreen() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [input, setInput] = useState(searchParams.get("q") ?? "");
  const query = useDebouncedValue(input, 300).trim();
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [layoutSeed, setLayoutSeed] = useState(1);
  const [legendOpen, setLegendOpen] = useState(true);
  const inputRef = useRef<HTMLInputElement>(null);

  // Shareable links: ?q= mirrors the debounced query (replace → no history spam)
  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    if (query) next.set("q", query);
    else next.delete("q");
    if (next.toString() !== searchParams.toString()) setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  // New query → new constellation, selection drops
  useEffect(() => setSelected(null), [query]);

  const search = useQuery({
    queryKey: ["graph-search", query],
    queryFn: () => searchGranules({ ...GRAPH_FILTERS, query }, 1),
    enabled: query.length > 0,
    staleTime: 60_000,
  });

  const hits = search.data?.results ?? [];
  const seeds = hits.slice(0, 8);
  const relations = useQueries({
    queries: seeds.map((hit) => ({
      queryKey: ["relations", hit.id],
      queryFn: () => getRelations(hit.id),
      staleTime: 60_000,
      retry: 1,
    })),
  });
  const relationsSettled = relations.every((q) => !q.isPending);

  const model = useMemo(() => {
    if (!search.data || !relationsSettled) return null;
    const byId = new Map<string, Awaited<ReturnType<typeof getRelations>>>();
    seeds.forEach((hit, i) => {
      const data = relations[i].data;
      if (data) byId.set(hit.id, data);
    });
    return buildGraphModel(hits, byId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search.data, relationsSettled, relations.map((q) => q.dataUpdatedAt).join(",")]);

  const graph = useMemo(() => (model && model.nodes.length > 0 ? toSigmaGraph(model, layoutSeed) : null), [model, layoutSeed]);

  const hoveredLabel = useMemo(() => {
    if (!hovered || !graph || !graph.hasNode(hovered)) return null;
    return graph.getNodeAttribute(hovered, "label") as string;
  }, [hovered, graph]);

  const legend = useMemo(() => {
    const namespaces = new Set<string>();
    model?.nodes.forEach((n) => namespaces.add(n.namespace ?? "default"));
    return [...namespaces].sort().map((ns) => ({ ns, color: resolveCssColor(namespaceColor(ns)) }));
  }, [model]);

  const reset = () => {
    setInput("");
    setSelected(null);
    inputRef.current?.focus();
  };

  return (
    <div className="graph-root">
      <div className="graph-float" role="complementary" aria-label="Управление графом">
        <div className="graph-searchbox">
          <i className="bi bi-search icon" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Спроси глубину — построю созвездие…"
            aria-label="Запрос для созвездия"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        <div className="graph-controls">
          <button className="btn" onClick={() => setLayoutSeed((s) => s + 1)} disabled={!graph}>
            <i className="bi bi-arrow-repeat" aria-hidden="true" /> Раскладка
          </button>
          <button className="btn" onClick={reset} disabled={!input && !selected}>
            <i className="bi bi-x-circle" aria-hidden="true" /> Сброс
          </button>
        </div>
        {model && (
          <p className="graph-count">
            {model.nodes.length} узлов · {model.edges.length} связей
            {hoveredLabel && <span className="graph-hover"> · {hoveredLabel}</span>}
          </p>
        )}
        {legend.length > 0 && (
          <div className="graph-legend">
            <button
              className="graph-legend-toggle"
              aria-expanded={legendOpen}
              onClick={() => setLegendOpen((v) => !v)}
            >
              <i className={`bi bi-chevron-${legendOpen ? "down" : "right"}`} aria-hidden="true" /> Спектр
            </button>
            {legendOpen && (
              <ul>
                {legend.map(({ ns, color }) => (
                  <li key={ns}>
                    <span className="dot" style={{ background: color }} />
                    {ns}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      <div
        className="graph-canvas"
        role="application"
        aria-label="Визуальный граф знаний, используйте экран Поиск"
      >
        {query === "" ? (
          <div className="state-block graph-empty">
            <i className="bi bi-diagram-3" aria-hidden="true" />
            <h3>Введите запрос — построю созвездие</h3>
            <p>Узлы — гранулы, цвет — слой памяти, размер — важность. Связи подтянутся к топ-8 гранулам.</p>
          </div>
        ) : search.isPending || !relationsSettled ? (
          <div className="state-block graph-empty">
            <span className="pulse-dot big" aria-hidden="true" />
            <h3>Собираю созвездие…</h3>
          </div>
        ) : search.isError ? (
          <div className="state-block error graph-empty">
            <i className="bi bi-wifi-off" aria-hidden="true" />
            <h3>Selti не отвечает</h3>
            <p>{(search.error as Error).message}</p>
            <button className="btn" onClick={() => void search.refetch()}>
              <i className="bi bi-arrow-clockwise" aria-hidden="true" /> Повторить
            </button>
          </div>
        ) : hits.length === 0 ? (
          <div className="state-block graph-empty">
            <i className="bi bi-stars" aria-hidden="true" />
            <h3>По «{query}» глубина молчит</h3>
            <p>Попробуйте другой запрос — созвездие строится от результатов поиска.</p>
          </div>
        ) : graph ? (
          <SigmaContainer
            settings={{
              defaultEdgeColor: resolveCssColor("var(--sl-border-strong)"),
              labelColor: { color: resolveCssColor("var(--sl-text-2)") },
              labelFont: '600 11px "JetBrains Mono", ui-monospace, monospace',
              labelRenderedSizeThreshold: 9,
              labelDensity: 0.4,
              labelGridCellSize: 70,
              minCameraRatio: 0.15,
              maxCameraRatio: 6,
              zIndex: true,
              renderEdgeLabels: false,
            }}
          >
            <GraphEffects graph={graph} onSelect={setSelected} onHover={setHovered} />
          </SigmaContainer>
        ) : null}
      </div>

      {selected && (
        <GranulePanel id={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}
