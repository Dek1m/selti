// /ui/graph — the deep chart (§6). Единый three.js-движок (fullmap) для
// обоих режимов: «Созвездие» — компактный поисковый граф (кап 120, seeds
// по ?q=), «Полная карта» — все 15k гранул с куллингом. EVE-style HUD,
// region legend, glass по клику, глубина BFS.

import { useQueries, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { getMemory, getRelations, searchGranules, type SearchFilters } from "../api/selti";
import type { MemoryRecord } from "../api/types";
import { FullMapLayer, type MapStats } from "../components/FullMapLayer";
import { GranulePanel } from "../components/GranulePanel";
import { GraphErrorBoundary } from "../components/GraphErrorBoundary";
import { namespaceColor, resolveCssColor, toRgba } from "../lib/colors";
import {
  buildGraphModel,
  graphNodeFromRecord,
  type GraphModel,
  type GraphNodeRecord,
} from "../lib/graph";
import { drawStarfield } from "../lib/starfield";
import type { RawMapSnapshot } from "../lib/fullmap/types";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

const GRAPH_FILTERS: SearchFilters = {
  query: "",
  namespaces: [],
  project: null,
  status: null,
  includeHistorical: false,
  period: "all",
};

/**
 * Deep-space backdrop behind the 3D map. Static dots only — redrawn on
 * resize and camera parallax, never per frame.
 */
function StarfieldLayer() {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    let frame = 0;
    const redraw = () => {
      drawStarfield(canvas);
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(redraw);
    };
    redraw();
    const observer = new ResizeObserver(schedule);
    observer.observe(canvas);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, []);

  return <canvas ref={ref} className="graph-stars" aria-hidden="true" />;
}

/**
 * Модель созвездия → RawMapSnapshot для общего 3D-движка: узлы как
 * [id, label, null, nsIdx, -1, importance, flags], рёбра с типами.
 * Координаты не нужны — движок раскладывает детерминированным объёмом.
 */
function modelToSnapshot(model: GraphModel): RawMapSnapshot {
  const namespaces: string[] = [];
  const nsIndex = new Map<string, number>();
  const nsIdxOf = (uid: string | null): number => {
    const key = uid ?? "default";
    let idx = nsIndex.get(key);
    if (idx === undefined) {
      idx = namespaces.push(key) - 1;
      nsIndex.set(key, idx);
    }
    return idx;
  };

  const edgeTypes: string[] = [];
  const etIndex = new Map<string, number>();
  const etIdxOf = (linkType: string): number => {
    let idx = etIndex.get(linkType);
    if (idx === undefined) {
      idx = edgeTypes.push(linkType) - 1;
      etIndex.set(linkType, idx);
    }
    return idx;
  };

  const indexOf = new Map<string, number>();
  const nodes = model.nodes.map((node, i) => {
    indexOf.set(node.id, i);
    const flags = (node.status === "superseded" || node.status === "retracted" ? 2 : 0) | (node.status === "uncertain" ? 0 : 0);
    return [
      node.id,
      node.label,
      null,
      nsIdxOf(node.namespace),
      -1,
      node.importance ?? 3,
      flags,
      0,
      0,
      0,
    ] as RawMapSnapshot["nodes"][number];
  });

  const edges = model.edges.map((edge) => [
    indexOf.get(edge.source) ?? 0,
    indexOf.get(edge.target) ?? 0,
    etIdxOf(edge.linkType),
    Math.max(1, edge.weight),
  ] as RawMapSnapshot["edges"][number]);

  return { v: "constellation", ns: namespaces, et: edgeTypes, clusters: [], nodes, edges };
}

export function GraphScreen() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [input, setInput] = useState(searchParams.get("q") ?? "");
  const query = useDebouncedValue(input, 300).trim();
  const [selected, setSelected] = useState<string | null>(null);
  const [legendOpen, setLegendOpen] = useState(true);
  const [hiddenLayers, setHiddenLayers] = useState<ReadonlySet<string>>(new Set());
  const inputRef = useRef<HTMLInputElement>(null);
  // M3: "full" renders the 3D whole-memory map, "constellation" — поисковый граф
  const view: "constellation" | "full" = searchParams.get("view") === "full" ? "full" : "constellation";
  const [mapStats, setMapStats] = useState<MapStats | null>(null);

  const setView = useCallback(
    (next: "constellation" | "full") => {
      const params = new URLSearchParams(searchParams);
      if (next === "full") params.set("view", "full");
      else params.delete("view");
      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams],
  );

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
    enabled: query.length > 0 && view === "constellation",
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

  // Gray strangers: relation neighbors the search never returned — fetch
  // their granules and relight the stars with real layers.
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

  // созвездие → снапшот для общего 3D-движка (seed стабилен — детерминизм)
  const constellationSnapshot = useMemo(() => {
    if (!enrichedModel || enrichedModel.nodes.length === 0) return null;
    return modelToSnapshot(enrichedModel);
  }, [enrichedModel]);

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

  // Layer visibility скрытых слоёв — через подсветку легенды: движок full
  // не знает про слои созвездия, поэтому скрываем регионы перерасборкой
  // модели (простота: скрытые слои просто не отдаются в снапшот)
  const constellationForEngine = useMemo(() => {
    if (!constellationSnapshot) return null;
    if (hiddenLayers.size === 0) return constellationSnapshot;
    const keep = enrichedModel
      ? enrichedModel.nodes.filter((node) => !hiddenLayers.has(node.namespace ?? "default")).map((node) => node.id)
      : [];
    const keepSet = new Set(keep);
    const filtered: RawMapSnapshot = {
      ...constellationSnapshot,
      nodes: constellationSnapshot.nodes.filter((node) => keepSet.has(node[0])),
      edges: constellationSnapshot.edges.filter(
        (edge) => keepSet.has(constellationSnapshot.nodes[edge[0]][0]) && keepSet.has(constellationSnapshot.nodes[edge[1]][0]),
      ),
    };
    return filtered;
  }, [constellationSnapshot, hiddenLayers, enrichedModel]);

  const reset = () => {
    setInput("");
    setSelected(null);
    inputRef.current?.focus();
  };

  return (
    <div className="graph-root">
      <StarfieldLayer />

      <div
        className="graph-canvas"
        role="application"
        aria-label="Визуальный граф знаний, используйте экран Поиск"
      >
        {view === "full" ? (
          <GraphErrorBoundary>
            <FullMapLayer query={query} selected={selected} onSelect={setSelected} onStats={setMapStats} />
          </GraphErrorBoundary>
        ) : query === "" ? (
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
        ) : (
          <GraphErrorBoundary>
            <FullMapLayer
              mode="constellation"
              constellationSnapshot={constellationForEngine}
              query=""
              selected={selected}
              onSelect={setSelected}
              onStats={setMapStats}
            />
          </GraphErrorBoundary>
        )}
      </div>

      <div className="graph-vignette" aria-hidden="true" />

      <div className="graph-float" role="complementary" aria-label="Управление графом">
        <header className="graph-hud-head">
          <span className="graph-hud-title">Deep Chart</span>
          <span className="graph-hud-live">
            <i className="live-dot" aria-hidden="true" />
            {view === "constellation" && search.isFetching ? "Scanning" : "Online"}
          </span>
        </header>

        <div className="graph-view-toggle" role="tablist" aria-label="Режим карты">
          <button
            className={`view-tab${view === "constellation" ? " on" : ""}`}
            role="tab"
            aria-selected={view === "constellation"}
            onClick={() => setView("constellation")}
          >
            <i className="bi bi-stars" aria-hidden="true" /> Созвездие
          </button>
          <button
            className={`view-tab${view === "full" ? " on" : ""}`}
            role="tab"
            aria-selected={view === "full"}
            onClick={() => setView("full")}
          >
            <i className="bi bi-globe2" aria-hidden="true" /> Полная карта
          </button>
        </div>

        <div className="graph-searchbox">
          <i className="bi bi-search icon" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              view === "full" ? "Поиск по полной карте — сегмент кластеров…" : "Спроси глубину — построю созвездие…"
            }
            aria-label="Запрос для созвездия"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        <div className="graph-controls">
          <button className="btn" onClick={reset} disabled={!input && !selected}>
            <i className="bi bi-x-circle" aria-hidden="true" /> Сброс
          </button>
        </div>
        {view === "full" ? (
          <>
            {mapStats && (
              <p className="graph-count">
                {mapStats.nodes.toLocaleString("ru-RU")} узлов · {mapStats.edges.toLocaleString("ru-RU")} связей ·{" "}
                {mapStats.clusters.toLocaleString("ru-RU")} кластеров
              </p>
            )}
            {mapStats?.mock && (
              <p className="graph-map-mock" title="Эндпоинты /api/map/meta и /api/map/full ещё не развёрнуты">
                <i className="bi bi-flask" aria-hidden="true" /> мок-снапшот (бэкенд M1 в пути)
              </p>
            )}
          </>
        ) : (
          enrichedModel && (
            <p className="graph-count">
              {enrichedModel.nodes.length} узлов · {enrichedModel.edges.length} связей
            </p>
          )
        )}
        {view === "constellation" && regions.length > 0 && (
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
