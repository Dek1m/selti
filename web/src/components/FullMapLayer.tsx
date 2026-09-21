// React shell around FullMapScene (M3 "Полная карта"): owns the snapshot
// worker, load progress, palette resolution, the search segment (?q=),
// selection glass, the hover tooltip and the camera HUD buttons.
// Falls back to the deterministic mock snapshot while Соны's
// /api/map/meta + /api/map/full are not deployed — same downstream path.

import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { searchGranules, type SearchFilters } from "../api/selti";
import { FullMapScene, type ScenePalette } from "../lib/fullmap/scene";
import { unpackNodeString } from "../lib/fullmap/pack";
import type { PackedMapSnapshot } from "../lib/fullmap/types";
import { hslToRgb, namespaceColor, resolveCssColor } from "../lib/colors";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

export interface MapStats {
  nodes: number;
  edges: number;
  clusters: number;
  mock: boolean;
}

interface FullMapLayerProps {
  query: string;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onStats: (stats: MapStats) => void;
}

interface TooltipState {
  name: string;
  preview: string | null;
  x: number;
  y: number;
}

const EMPTY_FILTERS: SearchFilters = {
  query: "",
  namespaces: [],
  project: null,
  status: null,
  includeHistorical: false,
  period: "all",
};

/** Resolve a namespace uid to an rgb triplet for the GPU attributes. */
function nsToRgb(uid: string | null): [number, number, number] {
  const css = resolveCssColor(namespaceColor(uid));
  if (css.startsWith("#")) {
    return [
      parseInt(css.slice(1, 3), 16) / 255,
      parseInt(css.slice(3, 5), 16) / 255,
      parseInt(css.slice(5, 7), 16) / 255,
    ];
  }
  const hsl = css.match(/^hsl\(\s*(-?[\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s*\)$/i);
  if (hsl) {
    const [r, g, b] = hslToRgb(parseFloat(hsl[1]), parseFloat(hsl[2]) / 100, parseFloat(hsl[3]) / 100);
    return [r / 255, g / 255, b / 255];
  }
  return [0.54, 0.59, 0.67];
}

const LOAD_STEPS: Record<string, string> = {
  connect: "Соединяюсь с картой…",
  download: "Тяну снапшот…",
  parse: "Разбираю колонки…",
  pack: "Пакую typed arrays…",
  mock: "Собираю мок-вселенную…",
};

export function FullMapLayer({ query, selected, onSelect, onStats }: FullMapLayerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const labelLayerRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<FullMapScene | null>(null);
  const packedRef = useRef<PackedMapSnapshot | null>(null);
  const indexByIdRef = useRef<Map<string, number>>(new Map());

  const [progress, setProgress] = useState<{ phase: string; fraction: number } | null>({
    phase: "connect",
    fraction: 0,
  });
  const [error, setError] = useState<string | null>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const [lodMode, setLodMode] = useState<"full" | "clusters">("full");

  // ── snapshot load (worker) + scene lifetime ──
  useEffect(() => {
    const container = containerRef.current;
    const labelLayer = labelLayerRef.current;
    if (!container || !labelLayer) return;

    const mockForced = new URLSearchParams(window.location.search).get("mock") === "1";
    const worker = new Worker(new URL("../lib/fullmap/map.worker.ts", import.meta.url), { type: "module" });

    const fog = new THREE.Color(resolveCssColor("var(--sl-bg-abyss)"));
    const palette: ScenePalette = {
      fog,
      namespaceRgb: [],
      ice: new THREE.Color(resolveCssColor("var(--sl-ns-frozen)")),
      supersedes: new THREE.Color(resolveCssColor("var(--sl-warn)")),
      contradicts: new THREE.Color(resolveCssColor("var(--sl-danger)")),
    };

    const scene = new FullMapScene(container, labelLayer, {
      onHover: (node) => {
        const packed = packedRef.current;
        if (!node || !packed) {
          setTooltip(null);
          return;
        }
        setTooltip({
          name: unpackNodeString(packed, node.index, 1),
          preview: packed.withPreview ? unpackNodeString(packed, node.index, 2) || null : null,
          x: node.x,
          y: node.y,
        });
      },
      onSelect: (pick) => {
        if (pick === null) {
          onSelect(null);
          return;
        }
        if ("cluster" in pick) return; // system drill-down is handled in-scene
        const packed = packedRef.current;
        if (!packed) return;
        onSelect(unpackNodeString(packed, pick.index, 0));
      },
      onLodChange: setLodMode,
    });
    scene.setPalette(palette);
    sceneRef.current = scene;

    worker.onmessage = (event: MessageEvent) => {
      const data = event.data as Record<string, unknown>;
      if (data.type === "progress") {
        setProgress(data.progress as { phase: string; fraction: number });
      } else if (data.type === "done") {
        const packed = data.packed as PackedMapSnapshot;
        packedRef.current = packed;
        palette.namespaceRgb = packed.namespaces.map((uid) => nsToRgb(uid));
        indexByIdRef.current = buildUuidIndex(packed);
        scene.setPalette(palette);
        scene.load(packed);
        onStats({
          nodes: packed.nodeCount,
          edges: packed.edgeCount,
          clusters: packed.clusters.length,
          mock: packed.version.startsWith("mock-"),
        });
        setProgress(null);
      } else if (data.type === "fail") {
        setError(data.message as string);
        setProgress(null);
      }
    };

    worker.postMessage({ type: "load", url: "/api/map/full?with_preview=true", withPreview: true, mock: mockForced });

    return () => {
      worker.terminate();
      scene.dispose();
      sceneRef.current = null;
      packedRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── selection → glass ──
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene || !packedRef.current) return;
    const index = selected ? (indexByIdRef.current.get(selected) ?? null) : null;
    scene.select(index);
  }, [selected, progress]);

  // ── search segment (?q= → clusters целиком, §4.1) ──
  const debouncedQuery = useDebouncedValue(query, 400).trim();
  useEffect(() => {
    const scene = sceneRef.current;
    const packed = packedRef.current;
    if (!scene || !packed) return;
    if (!debouncedQuery) {
      scene.clearSearchSegment();
      return;
    }
    let cancelled = false;
    void searchGranules({ ...EMPTY_FILTERS, query: debouncedQuery }, 1).then((outcome) => {
      if (cancelled) return;
      const hitIndices: number[] = [];
      const clusterSet = new Set<number>();
      for (const hit of outcome.results) {
        const index = indexByIdRef.current.get(hit.id);
        if (index === undefined) continue;
        hitIndices.push(index);
        const clusterIdx = packed.nodeMeta[index * 4 + 1] | 0;
        if (clusterIdx >= 0) clusterSet.add(clusterIdx);
      }
      scene.setSearchSegment(hitIndices, [...clusterSet]);
      // a fresh segment clears the old glass selection
      scene.select(null);
      onSelect(null);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery, progress]);

  const progressLabel = useMemo(() => {
    if (!progress) return null;
    const step = LOAD_STEPS[progress.phase] ?? "Гружу карту…";
    return `${step} ${Math.round(progress.fraction * 100)}%`;
  }, [progress]);

  return (
    <div className="map-root" role="application" aria-label="Полная карта памяти в 3D">
      <div ref={containerRef} className="map-canvas-host" />
      <div ref={labelLayerRef} className="map-labels" aria-hidden="true" />

      {tooltip && (
        <div className="map-tooltip" style={{ left: tooltip.x, top: tooltip.y }} aria-hidden="true">
          <p className="map-tooltip-name">{tooltip.name}</p>
          {tooltip.preview && <p className="map-tooltip-preview">{tooltip.preview}</p>}
        </div>
      )}

      {progressLabel && (
        <div className="state-block map-loading">
          <span className="pulse-dot big" aria-hidden="true" />
          <h3>Разворачиваю карту</h3>
          <p className="map-progress">{progressLabel}</p>
        </div>
      )}

      {error && (
        <div className="state-block error map-loading">
          <i className="bi bi-hdd-stack" aria-hidden="true" />
          <h3>Снапшот не собрался</h3>
          <p>{error}</p>
        </div>
      )}

      <div className="map-cam-buttons" role="group" aria-label="Камера">
        <span className={`map-lod-badge${lodMode === "clusters" ? " far" : ""}`}>
          {lodMode === "clusters" ? "звёздные системы" : "полный граф"}
        </span>
        <button className="btn" onClick={() => sceneRef.current?.resetCamera()} disabled={!!progress}>
          <i className="bi bi-arrow-counterclockwise" aria-hidden="true" /> Сброс камеры
        </button>
        <button className="btn" onClick={() => sceneRef.current?.topView()} disabled={!!progress}>
          <i className="bi bi-eye" aria-hidden="true" /> Вид сверху
        </button>
      </div>
    </div>
  );
}

function buildUuidIndex(packed: PackedMapSnapshot): Map<string, number> {
  const index = new Map<string, number>();
  for (let i = 0; i < packed.nodeCount; i++) {
    index.set(unpackNodeString(packed, i, 0), i);
  }
  return index;
}
