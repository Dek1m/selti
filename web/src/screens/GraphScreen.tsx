// /ui/graph — the deep chart (§6). EVE Online star map: WebGL stars with
// importance-driven halos, gradient hyperspace gates, dashed supersedes
// routes, extinguished superseded granules, parallax star field and a
// working HUD. Lazy-loaded: this module pulls graphology + sigma into
// their own chunk so the search bundle stays lean.

import { useQueries, useQuery } from "@tanstack/react-query";
import Graph from "graphology";
import { circular } from "graphology-layout";
import forceAtlas2 from "graphology-layout-forceatlas2";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { SigmaContainer, useLoadGraph, useRegisterEvents } from "@react-sigma/core";
import type { Sigma } from "sigma";
import "@react-sigma/core/lib/style.css";
import { getMemory, getRelations, searchGranules, type SearchFilters } from "../api/selti";
import type { MemoryRecord } from "../api/types";
import { GranulePanel } from "../components/GranulePanel";
import { GraphErrorBoundary } from "../components/GraphErrorBoundary";
import { namespaceColor, resolveCssColor, toRgba } from "../lib/colors";
import {
  buildGraphModel,
  edgeKind,
  edgeThickness,
  graphNodeFromRecord,
  nodeSize,
  starGlow,
  type GraphModel,
  type GraphNodeRecord,
} from "../lib/graph";
import { drawStarfield } from "../lib/starfield";
import { eveDrawNodeHover, HyperspaceEdgeProgram, StarNodeProgram } from "../lib/rendering";
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

/** Link weight → gate opacity: heavier relations burn brighter. */
function routeAlpha(weight: number): number {
  return 0.16 + Math.min(2, Math.max(0, weight - 1)) * 0.09;
}

/** Model → renderable graph: circular seed positions + seeded jitter,
 * then synchronous forceatlas2 (≤120 nodes — sub-frame on the main thread). */
function toSigmaGraph(model: GraphModel, layoutSeed: number): Graph {
  const graph = new Graph({ multi: false, type: "directed" });
  const starColor = (ns: string | null) => resolveCssColor(namespaceColor(ns));
  const extinguishedColor = resolveCssColor("var(--sl-ns-superseded)");
  const routeColor = resolveCssColor("var(--sl-warn)");
  const contradictsColor = resolveCssColor("var(--sl-danger)");

  for (const node of model.nodes) {
    const extinguished = starGlow(node) < 0;
    graph.addNode(node.id, {
      label: node.label,
      size: nodeSize(node),
      color: extinguished ? extinguishedColor : starColor(node.namespace),
      glow: starGlow(node),
      dim: 0,
      namespace: node.namespace ?? "",
      seed: node.seed,
      x: 0,
      y: 0,
    });
  }
  for (const edge of model.edges) {
    // parallel pair (different link types) keeps the first line only
    if (graph.hasEdge(edge.source, edge.target)) continue;
    const kind = edgeKind(edge.linkType);
    const from = resolveCssColor(namespaceColor(graph.getNodeAttribute(edge.source, "namespace") || null));
    const to = resolveCssColor(namespaceColor(graph.getNodeAttribute(edge.target, "namespace") || null));
    const [colorFrom, colorTo] =
      kind === "route"
        ? [toRgba(from, routeAlpha(edge.weight)), toRgba(to, routeAlpha(edge.weight))]
        : kind === "supersedes"
          ? [toRgba(routeColor, 0.55), toRgba(routeColor, 0.3)]
          : [toRgba(contradictsColor, 0.55), toRgba(contradictsColor, 0.35)];
    graph.addEdge(edge.source, edge.target, {
      size: edgeThickness(edge.weight),
      color: toRgba(from, routeAlpha(edge.weight)),
      colorFrom,
      colorTo,
      dash: kind === "supersedes" ? 1 : 0,
      dim: 0,
      weight: edge.weight,
      linkType: edge.linkType,
    });
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

/**
 * Deep-space backdrop behind the sigma canvas. Static dots only — redrawn
 * on resize and camera parallax, never per frame, so the graph keeps its
 * whole GPU budget for the constellation itself.
 */
function StarfieldLayer({ sigma }: { sigma: Sigma | null }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    let frame = 0;
    const redraw = () => {
      const camera = sigma?.getCamera();
      drawStarfield(canvas, camera ? { parallax: { x: camera.x - 0.5, y: camera.y - 0.5 } } : undefined);
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(redraw);
    };
    redraw();
    const observer = new ResizeObserver(schedule);
    observer.observe(canvas);
    const camera = sigma?.getCamera();
    camera?.on("updated", schedule);
    return () => {
      camera?.off("updated", schedule);
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [sigma]);

  return <canvas ref={ref} className="graph-stars" aria-hidden="true" />;
}

/**
 * Delicate focus ring around the hovered star, on its own overlay canvas:
 * a RAF loop that runs ONLY while hovering, so idle rendering cost is zero.
 * Honors prefers-reduced-motion with a single static ring.
 */
function HoverPing({ sigma, hovered }: { sigma: Sigma | null; hovered: string | null }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const clear = () => {
      const dpr = window.devicePixelRatio || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    };

    if (!hovered || !sigma || !sigma.getGraph().hasNode(hovered)) {
      clear();
      return;
    }

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    const draw = (phase: number) => {
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (canvas.width !== Math.round(w * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const attr = sigma.getGraph().getNodeAttributes(hovered) as { x?: number; y?: number } | undefined;
      if (!attr || typeof attr.x !== "number" || typeof attr.y !== "number") return;
      const point = sigma.graphToViewport({ x: attr.x, y: attr.y });
      const base = 12; // screen-space px, just outside the core
      const ring = (p: number, alphaScale: number) => {
        const radius = base * (1.3 + p * 1.9);
        const alpha = Math.max(0, 1 - p) * 0.45 * alphaScale;
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(56, 189, 248, ${alpha.toFixed(3)})`;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      };
      if (reduced) {
        ring(0, 0.9);
      } else {
        ring(phase, 1);
        ring((phase + 0.5) % 1, 0.65);
      }
    };

    let start = performance.now();
    const loop = (t: number) => {
      draw(((t - start) % 1700) / 1700);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [sigma, hovered]);

  return <canvas ref={ref} className="graph-pings" aria-hidden="true" />;
}

const GRAPH_FILTERS: SearchFilters = {
  query: "",
  namespaces: [],
  project: null,
  status: null,
  includeHistorical: false,
  period: "all",
};

/** Zoom → LOD step: far away, faint gates fade and let the big stars read. */
function zoomStepFor(ratio: number): number {
  if (ratio > 3.2) return 2;
  if (ratio > 1.8) return 1;
  return 0;
}

export function GraphScreen() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [input, setInput] = useState(searchParams.get("q") ?? "");
  const query = useDebouncedValue(input, 300).trim();
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [layoutSeed, setLayoutSeed] = useState(1);
  const [legendOpen, setLegendOpen] = useState(true);
  const [hiddenLayers, setHiddenLayers] = useState<ReadonlySet<string>>(new Set());
  const [sigma, setSigma] = useState<Sigma | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const zoomStepRef = useRef(0);

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

  // Gray strangers: relation neighbors the search never returned, whose layer
  // /relations does not carry. Fetch their granules and relight the stars —
  // the FA2 layout depends on topology only, so positions stay put.
  const strangers = useMemo(
    () => (model ? model.nodes.filter((node) => !node.namespace).map((node) => node.id) : []),
    [model],
  );
  const strangerQueries = useQueries({
    queries: strangers.map((id) => ({
      queryKey: ["memory", id],
      queryFn: () => getMemory(id),
      staleTime: 300_000,
      retry: 1,
    })),
  });
  const strangersStamp = strangerQueries.map((q) => q.dataUpdatedAt).join(",");

  const enrichedModel = useMemo(() => {
    if (!model) return null;
    if (strangers.length === 0) return model;
    const byId = new Map<string, GraphNodeRecord>();
    strangerQueries.forEach((query) => {
      const record = query.data as MemoryRecord | undefined;
      if (record) byId.set(record.id, record);
    });
    if (byId.size === 0) return model;
    return {
      ...model,
      nodes: model.nodes.map((node) =>
        node.namespace || !byId.has(node.id) ? node : graphNodeFromRecord(byId.get(node.id)!),
      ),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, strangers, strangersStamp]);

  const graph = useMemo(
    () => (enrichedModel && enrichedModel.nodes.length > 0 ? toSigmaGraph(enrichedModel, layoutSeed) : null),
    [enrichedModel, layoutSeed],
  );

  const hoveredLabel = useMemo(() => {
    if (!hovered || !graph || !graph.hasNode(hovered)) return null;
    return graph.getNodeAttribute(hovered, "label") as string;
  }, [hovered, graph]);

  // Region map: namespaces present in the current constellation + counts
  const regions = useMemo(() => {
    const counts = new Map<string, { count: number; color: string }>();
    enrichedModel?.nodes.forEach((n) => {
      const uid = n.namespace ?? "default";
      const entry = counts.get(uid) ?? { count: 0, color: resolveCssColor(namespaceColor(n.namespace)) };
      entry.count += 1;
      counts.set(uid, entry);
    });
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([uid, v]) => ({ uid, ...v }));
  }, [enrichedModel]);

  const toggleLayer = useCallback((uid: string) => {
    setHiddenLayers((prev) => {
      const next = new Set(prev);
      if (next.has(uid)) next.delete(uid);
      else next.add(uid);
      return next;
    });
  }, []);

  /**
   * Focus + LOD + layer filters: written straight into graph attributes.
   * Hidden nodes/edges drop out of programs; dim fades non-neighbors when
   * a star is focused, and faint gates at far zoom (LOD).
   */
  const applyGraphState = useCallback(() => {
    if (!graph) return;
    const neighborSet =
      hovered && graph.hasNode(hovered)
        ? new Set<string>([hovered, ...graph.neighbors(hovered)])
        : null;
    const lod = zoomStepRef.current;

    graph.updateEachNodeAttributes((node, attrs) => {
      const hidden = hiddenLayers.has(String(attrs.namespace ?? "default"));
      attrs.hidden = hidden;
      attrs.dim = !hidden && neighborSet && !neighborSet.has(node) ? 1 : 0;
      return attrs;
    });
    graph.updateEachEdgeAttributes((_edge, attrs, source, target, sourceAttrs, targetAttrs) => {
      const hidden = sourceAttrs.hidden || targetAttrs.hidden;
      attrs.hidden = hidden;
      const lodDim = lod >= 2 && attrs.weight <= 2 ? 0.6 : lod >= 1 && attrs.weight < 2 ? 0.5 : 0;
      attrs.dim = hidden ? 0 : neighborSet ? (neighborSet.has(source) && neighborSet.has(target) ? 0 : 1) : lodDim;
      return attrs;
    });
  }, [graph, hovered, hiddenLayers]);

  useEffect(() => applyGraphState(), [applyGraphState]);

  // Camera zoom crossing an LOD threshold rewrites dim flags (discrete, not per frame)
  useEffect(() => {
    if (!sigma) return;
    const camera = sigma.getCamera();
    zoomStepRef.current = zoomStepFor(camera.ratio);
    let last = zoomStepRef.current;
    const onUpdate = () => {
      const step = zoomStepFor(camera.ratio);
      if (step !== last) {
        last = step;
        zoomStepRef.current = step;
        applyGraphState();
      }
    };
    camera.on("updated", onUpdate);
    return () => {
      camera.off("updated", onUpdate);
    };
  }, [sigma, applyGraphState]);

  const reset = () => {
    setInput("");
    setSelected(null);
    inputRef.current?.focus();
  };

  return (
    <div className="graph-root">
      <StarfieldLayer sigma={sigma} />

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
          <GraphErrorBoundary>
            <SigmaContainer
              ref={setSigma}
              settings={{
                nodeProgramClasses: { star: StarNodeProgram },
                defaultNodeType: "star",
                edgeProgramClasses: { hyperspace: HyperspaceEdgeProgram },
                defaultEdgeType: "hyperspace",
                defaultDrawNodeHover: eveDrawNodeHover,
                defaultEdgeColor: resolveCssColor("var(--sl-border-strong)"),
                labelColor: { color: resolveCssColor("var(--sl-text-2)") },
                labelFont: '"JetBrains Mono", ui-monospace, monospace',
                labelSize: 11,
                labelWeight: "600",
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
          </GraphErrorBoundary>
        ) : null}
      </div>

      {graph && <HoverPing sigma={sigma} hovered={hovered} />}
      <div className="graph-vignette" aria-hidden="true" />

      <div className="graph-float" role="complementary" aria-label="Управление графом">
        <header className="graph-hud-head">
          <span className="graph-hud-title">Deep Chart</span>
          <span className="graph-hud-live">
            <i className="live-dot" aria-hidden="true" />
            {search.isFetching ? "Scanning" : "Online"}
          </span>
        </header>

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
        {enrichedModel && (
          <p className="graph-count">
            {enrichedModel.nodes.length} узлов · {enrichedModel.edges.length} связей
            {hoveredLabel && <span className="graph-hover"> · {hoveredLabel}</span>}
          </p>
        )}
        {regions.length > 0 && (
          <div className="graph-legend">
            <button
              className="graph-legend-toggle"
              aria-expanded={legendOpen}
              onClick={() => setLegendOpen((v) => !v)}
            >
              <i className={`bi bi-chevron-${legendOpen ? "down" : "right"}`} aria-hidden="true" /> Карта региона
            </button>
            {legendOpen && (
              <>
                <ul className="legend-regions" aria-label="Слои памяти">
                  {regions.map(({ uid, count, color }) => (
                    <li key={uid}>
                      <button
                        className={`legend-region${hiddenLayers.has(uid) ? " off" : ""}`}
                        aria-pressed={!hiddenLayers.has(uid)}
                        title={hiddenLayers.has(uid) ? `Показать слой ${uid}` : `Скрыть слой ${uid}`}
                        onClick={() => toggleLayer(uid)}
                      >
                        <span className="dot" style={{ background: color, boxShadow: `0 0 8px ${toRgba(color, 0.45)}` }} />
                        <span className="legend-region-name">{uid}</span>
                        <span className="legend-region-count">{count}</span>
                      </button>
                    </li>
                  ))}
                </ul>

                <div className="legend-block">
                  <p className="legend-label">Звёзды · важность</p>
                  <div className="legend-stars" aria-label="Размер звезды = важность гранулы">
                    {[1, 2, 3, 4, 5].map((v) => (
                      <span key={v} className={`legend-star star-${v}`} data-label={v} />
                    ))}
                    <span className="legend-hint">1 → 5</span>
                  </div>
                </div>

                <div className="legend-block">
                  <p className="legend-label">Пороги</p>
                  <ul className="legend-gates">
                    <li>
                      <span className="gate-sample route" /> связь
                    </li>
                    <li>
                      <span className="gate-sample supersedes" /> supersedes — версия
                    </li>
                    <li>
                      <span className="gate-sample contradicts" /> contradicts — спор
                    </li>
                    <li>
                      <span className="legend-star extinct" /> погасшая гранула
                    </li>
                  </ul>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {selected && (
        <GranulePanel id={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}
