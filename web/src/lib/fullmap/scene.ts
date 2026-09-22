// FullMapScene — the three.js layer behind the "Полная карта" mode (M3).
//
//   • Points + ShaderMaterial: the full 15k graph in one draw call, GLSL
//     ported from the 2D star map (glow by importance, namespace colors,
//     ember outlines) plus 3D-specific glass / distance-fade / twinkle.
//   • LineSegments: gradient gates, additive, one draw call, index-culled
//     beyond the fade threshold (§4.4).
//   • Fixed camera framing from the ellipse edge (разворот Мастера):
//     RMB = orbit, LMB = pan, wheel = zoom, damped; click-vs-pan at 5px.
//     Всегда полный граф с куллингом 280/1200 — кластерный LOD убран.
//   • Search segment (§4.1): hit clusters light up whole — nebula shells
//     at cluster centroids, hits brighter, the rest turns to glass.
//   • Glass (§4.2): click → BFS levels → continuous opacity/desaturation.
//   • Screen-space picking + DOM tooltip + top-K labels (§4.3, §7).

import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { bfsLevels } from "./bfs";
import { EDGE_VISIBLE_CAP, selectVisibleEdges, selectVisibleNodes } from "./edges";
import { selectLabeledNodes, type LabelCandidate } from "./lod";
import { ellipseLayout, mixedLayout, FULL_VOLUME, type LayoutBounds, type VolumeBounds } from "./layout";
import { unpackNodeString } from "./pack";
import { STAR_FRAGMENT, STAR_VERTEX } from "./shaders";
import type { PackedCluster, PackedMapSnapshot } from "./types";

const CLICK_SLOP_PX = 5;
const PICK_RADIUS_PX = 12;
const LABEL_MAX = 22;
const EDGE_CULL_COOLDOWN_MS = 150;
const LABEL_COOLDOWN_MS = 140;

/**
 * Жёсткий кап видимых звёзд (фидбек Мастера 1/7): полный граф держит
 * fill-rate глоу под контролем — рисуем только фрустум + margin, максимум
 * NODE_VISIBLE_CAP одновременно, приоритет «ярче/ближе важнее».
 */
/**
 * Сфера видимости (формализация Мастера): всё за этим радиусом от камеры
 * выгружается из draw-range. Бесшовность гарантируется тем, что fade
 * звёзд достигает нуля ровно на этой дистанции (FADE_END шейдера = 3200).
 */
export const VIEW_SPHERE_R = 3200;

const NODE_VISIBLE_CAP = 280;
const NODE_CULL_COOLDOWN_MS = 150;
/** NDC margin around the viewport before a star leaves the draw set. */
const NODE_CULL_MARGIN = 1.15;

export interface FullMapSceneCallbacks {
  /** hover moved onto a star (index) or off (null); screen px included */
  onHover: (node: { index: number; x: number; y: number } | null) => void;
  /** click resolved as a star */
  onSelect: (node: { index: number } | null) => void;
}

/** Neutral palette pieces resolved once from CSS tokens. */
export interface ScenePalette {
  fog: THREE.Color;
  namespaceRgb: Array<[number, number, number]>;
  ice: THREE.Color;
  supersedes: THREE.Color;
  contradicts: THREE.Color;
}

export class FullMapScene {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene: THREE.Scene;
  readonly camera: THREE.PerspectiveCamera;
  readonly controls: OrbitControls;

  private container: HTMLElement;
  private callbacks: FullMapSceneCallbacks;
  private packed: PackedMapSnapshot | null = null;
  private palette: ScenePalette | null = null;
  private reducedMotion = false;

  private fullPoints: THREE.Points | null = null;
  private fullEdges: THREE.LineSegments | null = null;
  private nebulaGroup = new THREE.Group();
  private nebulaTexture: THREE.Texture | null = null;
  private labelLayer: HTMLDivElement;

  private bfsAttr: THREE.BufferAttribute | null = null;
  private highlightAttr: THREE.BufferAttribute | null = null;
  private clusterHighlightAttr: THREE.BufferAttribute | null = null;
  private nodeIndex: THREE.BufferAttribute | null = null;
  private nodeIndexArray: Uint32Array | null = null;
  private nodeVisibleCount = 0;
  private nodeVisible: Uint8Array | null = null;
  private showAuxiliaryEdges = false;
  private lastNodeCull = 0;
  private edgeDebug = typeof window !== "undefined" && new URLSearchParams(window.location.search).has("debug");
  private lastEdgeDebugLog = 0;
  private lastEdgeStats = { candidates: 0, bothVisible: 0, drawn: 0 };

  /**
   * HUD-диагностика (?debug=1 → #fullmap-debug): всё, что нужно, чтобы
   * за один взгляд понять, где рёбра теряются — отбор, drawRange,
   * boundingSphere, актуальные uniforms и счётчик пайплайна.
   */
  getDebugInfo(): string {
    if (!this.fullEdges || !this.packed) return "no edge mesh";
    const geo = this.fullEdges.geometry;
    const bs = geo.boundingSphere;
    const material = this.fullEdges.material as THREE.ShaderMaterial;
    const uViewport = material.uniforms.uViewport.value as THREE.Vector2;
    const info = this.renderer.info.render;

    // parent-цепочка до сцены (пункт 1): Mesh обязан висеть на Scene
    const parents: string[] = [];
    let node: THREE.Object3D | null = this.fullEdges;
    while (node) {
      parents.push(node.type + (node.name ? `:${node.name}` : ""));
      node = node.parent;
    }

    // фактические вершины ПЕРВОГО квада из живого буфера (пункт 2)
    let quad = "quad: none";
    const posAttr = geo.getAttribute("position") as THREE.BufferAttribute | undefined;
    const otherAttr = geo.getAttribute("aOther") as THREE.BufferAttribute | undefined;
    const sideAttr = geo.getAttribute("aSide") as THREE.BufferAttribute | undefined;
    const index = geo.getIndex();
    if (posAttr && otherAttr && sideAttr && index && geo.drawRange.count > 0) {
      const f = (v: number) => v.toFixed(1);
      const parts: string[] = [];
      const corners = [0, 1, 2, 3].map((c) => {
        const v = index.array[c];
        const px = posAttr.getX(v);
        const py = posAttr.getY(v);
        const pz = posAttr.getZ(v);
        const ox = otherAttr.getX(v);
        const oy = otherAttr.getY(v);
        const oz = otherAttr.getZ(v);
        parts.push(
          `v${c}[i=${v}] pos=(${f(px)},${f(py)},${f(pz)}) other=(${f(ox)},${f(oy)},${f(oz)}) side=${sideAttr.getX(v)}`,
        );
        const bad = ![px, py, pz, ox, oy, oz].every(Number.isFinite);
        return bad ? "NaN!" : "ok";
      });
      // CPU-эмуляция вершинного шейдера: итоговые экранные px углов
      this.camera.updateMatrixWorld();
      const vp = new THREE.Matrix4().multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse);
      const ve = vp.elements;
      const vw = this.renderer.domElement.width;
      const vh = this.renderer.domElement.height;
      const projPx = (x: number, y: number, z: number) => {
        const w = ve[3] * x + ve[7] * y + ve[11] * z + ve[15];
        if (!Number.isFinite(w) || Math.abs(w) < 1e-6) return "w~0";
        const nx = (ve[0] * x + ve[4] * y + ve[8] * z + ve[12]) / w;
        const ny = (ve[1] * x + ve[5] * y + ve[9] * z + ve[13]) / w;
        return `${(((nx + 1) / 2) * vw).toFixed(0)},${(((1 - ny) / 2) * vh).toFixed(0)}`;
      };
      const q0 = index.array[0];
      const q3 = index.array[3];
      const cornersPx = [
        projPx(posAttr.getX(q0), posAttr.getY(q0), posAttr.getZ(q0)),
        projPx(posAttr.getX(q0 + 2), posAttr.getY(q0 + 2), posAttr.getZ(q0 + 2)),
        projPx(posAttr.getX(q3), posAttr.getY(q3), posAttr.getZ(q3)),
      ].join(" / ");
      quad = `quad: ${corners.join(",")} | cornersPx(A/B/B') ${cornersPx} | ${parts.slice(0, 1).join(" | ")}`;
    }
    const mainEdgePos = this.mainEdges?.geometry.getAttribute("position") as THREE.BufferAttribute | undefined;
    const raw6 = mainEdgePos
      ? [0, 3].map((f) => `${mainEdgePos.getX(f).toFixed(0)},${mainEdgePos.getY(f).toFixed(0)},${mainEdgePos.getZ(f).toFixed(0)}`).join(" / ")
      : "none";

    const parentChain = parents.join(" < ");
    // бисект-меш: зелёный wireframe на геометрии лент
    // позиционный буфер: первые два узла + NaN-скан (диагноз Мастера)
    let nanCount = 0;
    const npos = this.packed.nodePositions;
    for (let i = 0; i < npos.length; i++) if (Number.isNaN(npos[i])) nanCount++;
    const f3 = (i: number) =>
      `[${npos[i * 3].toFixed(0)},${npos[i * 3 + 1].toFixed(0)},${npos[i * 3 + 2].toFixed(0)}]`;

    return [
      `build ${__BUILD_ID__}`,
      `pos0=${f3(0)} pos1=${f3(1)} len=${npos.length} nan=${nanCount}`,
      `nodes ${this.nodeVisibleCount}/${this.packed.nodeCount}`,
      `edges cand/drawn/both ${this.lastEdgeStats.candidates}/${this.lastEdgeStats.drawn}/${this.lastEdgeStats.bothVisible}`,
      `edgeMesh visible=${this.fullEdges.visible} drawRange=${geo.drawRange.count} bs=${bs ? bs.radius.toFixed(0) : "null"} renderOrder=${this.fullEdges.renderOrder} mw0-3=[${this.fullEdges.matrixWorld.elements.slice(0, 4).map((v) => v.toFixed(2)).join(",")}] parent=${parentChain}`,
      `uViewport=(${uViewport.x | 0}x${uViewport.y | 0}) uEdgeWidth=${material.uniforms.uEdgeWidth.value.toFixed(1)}`,
      `pipeline calls=${info.calls} tris=${info.triangles} points=${info.points}`,
      `mainEdge pos0/1: ${raw6}`,
      quad,
    ].join(" | ");
  }
  private lastEdgeCull = 0;
  private lastLabelRefresh = 0;
  private cameraDirty = true;
  private clusterRadii = new Map<number, number>();
  private clusterByIndex = new Map<number, PackedCluster>();

  private levels: Int32Array | null = null;
  private hoverIndex: number | null = null;
  private labels: HTMLDivElement[] = [];

  private frame = 0;
  private clock = new THREE.Clock();
  private resizeObserver: ResizeObserver;
  private pointerDown: { x: number; y: number; button: number; moved: boolean } | null = null;
  private pointerNdc = new THREE.Vector2();
  private pointerScreen = { x: 0, y: 0 };
  private pointerDirty = false;
  private disposed = false;

  constructor(container: HTMLElement, labelLayer: HTMLDivElement, callbacks: FullMapSceneCallbacks) {
    this.container = container;
    this.labelLayer = labelLayer;
    this.callbacks = callbacks;

    this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.domElement.classList.add("map-canvas");
    container.appendChild(this.renderer.domElement);

    this.camera = new THREE.PerspectiveCamera(55, 1, 2, 20000);
    this.camera.position.set(0, 620, 950);

    this.scene = new THREE.Scene();

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true; // inertia — the Master's requirement
    this.controls.dampingFactor = 0.08;
    this.controls.rotateSpeed = 0.55;
    this.controls.panSpeed = 0.9;
    this.controls.zoomSpeed = 1.1;
    this.controls.minDistance = 120;
    this.controls.maxDistance = 9000;
    // Master's mapping: RMB orbits, LMB pans, wheel zooms
    this.controls.mouseButtons = {
      LEFT: THREE.MOUSE.PAN,
      MIDDLE: THREE.MOUSE.DOLLY,
      RIGHT: THREE.MOUSE.ROTATE,
    };
    this.controls.addEventListener("start", () => this.onControlsStart());
    this.controls.addEventListener("change", () => {
      this.pointerDirty = true;
      this.cameraDirty = true;
    });

    this.renderer.domElement.addEventListener("contextmenu", (e) => e.preventDefault());
    this.renderer.domElement.addEventListener("pointerdown", this.onPointerDown);
    this.renderer.domElement.addEventListener("pointermove", this.onPointerMove);
    this.renderer.domElement.addEventListener("pointerup", this.onPointerUp);
    this.renderer.domElement.addEventListener("pointerleave", this.onPointerLeave);

    this.scene.add(this.nebulaGroup);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();

    this.animate = this.animate.bind(this);
    this.frame = requestAnimationFrame(this.animate);
  }

  // ── data wiring ──

  setPalette(palette: ScenePalette): void {
    this.palette = palette;
  }

  /** Build/replace the GPU buffers from a packed snapshot. */
  load(
    packed: PackedMapSnapshot,
    layout: { kind: "volume"; bounds: VolumeBounds } | { kind: "ellipse"; bounds: LayoutBounds } = {
      kind: "volume",
      bounds: FULL_VOLUME,
    },
  ): void {
    this.disposeMap();
    this.packed = packed;
    this.levels = null;
    this.hoverIndex = null;

    // разворот Мастера: клиентская детерминированная раскладка — full =
    // объём EVE-стиля (60% кластерные сгустки + 40% фон), созвездие =
    // компактный объём; серверные координаты full-снапшота игнорируем
    const uuids: string[] = [];
    for (let i = 0; i < packed.nodeCount; i++) {
      uuids.push(unpackNodeString(packed, i, 0));
    }
    if (layout.kind === "volume") {
      const slotOf = (i: number) => packed.nodeMeta[i * 4 + 1] | 0;
      mixedLayout(uuids, slotOf, packed.clusters.length, packed.nodePositions, {
        span: layout.bounds.span,
        height: layout.bounds.height,
      });
    } else {
      ellipseLayout(uuids, packed.nodePositions, layout.bounds);
    }
    // центроиды кластеров (оболочки-туманности) — по НОВЫМ позициям
    const accX = new Float64Array(packed.clusters.length);
    const accY = new Float64Array(packed.clusters.length);
    const accZ = new Float64Array(packed.clusters.length);
    const accN = new Float64Array(packed.clusters.length);
    for (let i = 0; i < packed.nodeCount; i++) {
      const slot = packed.nodeMeta[i * 4 + 1] | 0;
      if (slot < 0 || slot >= packed.clusters.length) continue;
      accX[slot] += packed.nodePositions[i * 3];
      accY[slot] += packed.nodePositions[i * 3 + 1];
      accZ[slot] += packed.nodePositions[i * 3 + 2];
      accN[slot] += 1;
    }
    for (const cluster of packed.clusters) {
      const c = accN[cluster.index] || 1;
      cluster.centroid = [accX[cluster.index] / c, accY[cluster.index] / c, accZ[cluster.index] / c];
    }

    const n = packed.nodeCount;
    const colors = new Float32Array(n * 3);
    const sizes = new Float32Array(n);
    const flags = new Float32Array(n);
    const bfs = new Float32Array(n).fill(-1);
    const highlight = new Float32Array(n);
    const nsRgb = this.palette?.namespaceRgb ?? [];

    for (let i = 0; i < n; i++) {
      const nsIdx = packed.nodeMeta[i * 4] | 0;
      const rgb = nsRgb[nsIdx] ?? [0.54, 0.59, 0.67];
      colors[i * 3] = rgb[0];
      colors[i * 3 + 1] = rgb[1];
      colors[i * 3 + 2] = rgb[2];
      sizes[i] = packed.nodeMeta[i * 4 + 2];
      flags[i] = packed.nodeMeta[i * 4 + 3];
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(packed.nodePositions, 3));
    geometry.setAttribute("aColor", new THREE.BufferAttribute(colors, 3));
    geometry.setAttribute("aSize", new THREE.BufferAttribute(sizes, 1));
    geometry.setAttribute("aFlags", new THREE.BufferAttribute(flags, 1));
    this.bfsAttr = new THREE.BufferAttribute(bfs, 1);
    this.highlightAttr = new THREE.BufferAttribute(highlight, 1);
    geometry.setAttribute("aBfs", this.bfsAttr);
    geometry.setAttribute("aHighlight", this.highlightAttr);
    // viewport-culled draw set (фидбек 1): Points рисует только индексы,
    // отобранные rebuildVisibleNodes — скрытые узлы не рендерятся вовсе
    this.nodeIndexArray = new Uint32Array(n);
    this.nodeIndex = new THREE.BufferAttribute(this.nodeIndexArray, 1);
    geometry.setIndex(this.nodeIndex);
    geometry.setDrawRange(0, 0);
    geometry.computeBoundingSphere();

    this.fullPoints = new THREE.Points(geometry, this.starMaterial());
    this.fullPoints.frustumCulled = false;
    this.scene.add(this.fullPoints);

    this.buildEdgeLines();
    this.rebuildClusterRadii(packed);
    this.rebuildVisibleNodes(performance.now(), true);
    this.cullEdges(null);
    // ribbon-материалы созданы здесь впервые — их uViewport обязан получить
    // реальные размеры немедленно (иначе ленты строятся от viewport 1×1
    // и улетают мимо экрана: «рёбер нет вообще»)
    this.resize();
  }

  /** importance per node, кешируется при load — selectVisibleEdges читает её */
  private edgeImportance: Float32Array | null = null;

  /** Радиусы кластеров для оболочек-туманностей (по числу членов). */
  private rebuildClusterRadii(packed: PackedMapSnapshot): void {
    this.clusterByIndex = new Map(packed.clusters.map((cl) => [cl.index, cl]));
    this.clusterRadii.clear();
    for (const cluster of packed.clusters) {
      this.clusterRadii.set(cluster.index, 40 + Math.sqrt(cluster.members) * 11);
    }
  }

  private starMaterial(): THREE.ShaderMaterial {
    return new THREE.ShaderMaterial({
      vertexShader: STAR_VERTEX,
      fragmentShader: STAR_FRAGMENT,
      uniforms: {
        uPixelRatio: { value: this.renderer.getPixelRatio() },
        uSizeScale: { value: 1 },
        uTime: { value: 0 },
        uTwinkle: { value: this.reducedMotion ? 0 : 1 },
        uDepthCap: { value: 99 },
        uFogColor: { value: this.palette?.fog ?? new THREE.Color("#060a12") },
        uIceColor: { value: this.palette?.ice ?? new THREE.Color("#7dd3fc") },
      },
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
  }

  /**
   * Рёбра — THREE.LineSegments (эталон EVE, финал): три меша —
   * основной с vertexColors (градиент слой→слой из коробки), янтарные
   * supersedes и красные contradicts. Куллинг перезаписывает
   * position/color буферы видимого набора (кап 1200 суммарно).
   */
  private buildEdgeLines(): void {
    const cap = EDGE_VISIBLE_CAP;
    const make = (vertexColors: boolean) => {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(cap * 6), 3));
      if (vertexColors) geometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(cap * 6), 3));
      geometry.setDrawRange(0, 0);
      return geometry;
    };

    const contradicts = this.palette?.contradicts ?? new THREE.Color("#ff7a8a");
    const warn = this.palette?.supersedes ?? new THREE.Color("#ffc15e");

    this.mainEdges = new THREE.LineSegments(
      make(true),
      new THREE.LineBasicMaterial({
        vertexColors: true,
        transparent: true,
        opacity: 0.22,
        blending: THREE.NormalBlending, // additive на плотных линиях белеет (фидбек)
        depthTest: false,
        depthWrite: false,
      }),
    );
    this.supersedesEdges = new THREE.LineSegments(
      make(false),
      new THREE.LineBasicMaterial({
        color: warn,
        transparent: true,
        opacity: 0.6,
        blending: THREE.AdditiveBlending,
        depthTest: false,
        depthWrite: false,
      }),
    );
    this.contradictsEdges = new THREE.LineSegments(
      make(false),
      new THREE.LineBasicMaterial({
        color: contradicts,
        transparent: true,
        opacity: 0.75,
        blending: THREE.AdditiveBlending,
        depthTest: false,
        depthWrite: false,
      }),
    );
    for (const mesh of [this.mainEdges, this.supersedesEdges, this.contradictsEdges]) {
      mesh.frustumCulled = false;
      this.scene.add(mesh);
    }
  }

  private mainEdges: THREE.LineSegments | null = null;
  private supersedesEdges: THREE.LineSegments | null = null;
  private contradictsEdges: THREE.LineSegments | null = null;

  private makeNebulaTexture(): THREE.Texture {
    const size = 128;
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d")!;
    const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, "rgba(120, 190, 255, 0.55)");
    grad.addColorStop(0.45, "rgba(90, 150, 255, 0.18)");
    grad.addColorStop(1, "rgba(80, 130, 255, 0)");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, size, size);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
  }

  // ── interaction state (called from React) ──

  /** Click glass: BFS from the star, continuous fade by level (§4.2). */
  select(index: number | null): void {
    if (!this.packed) return;
    if (!this.bfsAttr) return;
    this.cameraDirty = true; // выбор пробивается сквозь visible-cap
    // стеклянный расфокус (фидбек Мастера): невыбранные — размытые пятна
    const starMaterial = this.fullPoints?.material as THREE.ShaderMaterial | undefined;
    if (starMaterial) starMaterial.uniforms.uFocusBlur.value = index === null ? 0 : 1;
    this.setEdgeDimmed(index !== null);
    const attr = this.bfsAttr;
    const array = attr.array as Float32Array;
    if (index === null) {
      this.levels = null;
      array.fill(-1);
    } else {
      this.levels = bfsLevels(this.packed, [index]);
      for (let i = 0; i < this.packed.nodeCount; i++) array[i] = this.levels[i];
    }
    attr.needsUpdate = true;
  }

  /**
   * Search segment (§4.1): hits burn brightest, their whole clusters glow,
   * everything else dims toward glass. Nebula shells mark the regions.
   */
  setSearchSegment(hitIndices: number[], clusterIndices: number[]): void {
    if (!this.packed || !this.highlightAttr) return;
    this.cameraDirty = true; // сегмент пробивается сквозь visible-cap
    const attr = this.highlightAttr;
    const array = attr.array as Float32Array;
    array.fill(0);

    const litClusters = new Set(clusterIndices);
    for (const index of hitIndices) {
      if (index >= 0 && index < this.packed.nodeCount) array[index] = 2;
    }
    for (let i = 0; i < this.packed.nodeCount; i++) {
      if (array[i] === 2) continue;
      const clusterIdx = this.packed.nodeMeta[i * 4 + 1] | 0;
      if (litClusters.has(clusterIdx)) array[i] = 1;
    }
    attr.needsUpdate = true;

    // cluster level mirrors the segment: lit systems glow too
    if (this.clusterHighlightAttr) {
      const clusterArray = this.clusterHighlightAttr.array as Float32Array;
      clusterArray.fill(0);
      // nodeMeta clusterIdx is a compact slot == position in packed.clusters
      litClusters.forEach((slot) => {
        if (slot >= 0 && slot < clusterArray.length) clusterArray[slot] = 2;
      });
      this.clusterHighlightAttr.needsUpdate = true;
    }
    this.refreshNebulas(litClusters);
  }

  clearSearchSegment(): void {
    if (!this.highlightAttr) return;
    this.cameraDirty = true;
    (this.highlightAttr.array as Float32Array).fill(0);
    this.highlightAttr.needsUpdate = true;
    if (this.clusterHighlightAttr) {
      (this.clusterHighlightAttr.array as Float32Array).fill(0);
      this.clusterHighlightAttr.needsUpdate = true;
    }
    this.refreshNebulas(new Set());
  }

  private refreshNebulas(litClusters: Set<number>): void {
    this.nebulaGroup.clear();
    if (!this.packed || litClusters.size === 0) return;
    if (!this.nebulaTexture) this.nebulaTexture = this.makeNebulaTexture();

    for (const clusterIdx of litClusters) {
      const cluster = this.clusterByIndex.get(clusterIdx);
      if (!cluster) continue;
      const radius = this.clusterRadii.get(clusterIdx) ?? 140;
      const sprite = new THREE.Sprite(
        new THREE.SpriteMaterial({
          map: this.nebulaTexture,
          transparent: true,
          opacity: 0.34,
          depthWrite: false,
          blending: THREE.AdditiveBlending,
        }),
      );
      sprite.position.set(cluster.centroid[0], cluster.centroid[1], cluster.centroid[2]);
      sprite.scale.setScalar(radius * 2.6);
      this.nebulaGroup.add(sprite);
    }
  }

  /** Camera fly-to helpers (HUD buttons, cluster drill-down). */
  resetCamera(): void {
    this.flyTo(new THREE.Vector3(0, 900, 2600), new THREE.Vector3(0, 0, 0));
  }

  topView(): void {
    this.flyTo(new THREE.Vector3(0, 2800, 0.001), new THREE.Vector3(0, 0, 0));
  }

  focusNode(index: number): void {
    if (!this.packed) return;
    const target = new THREE.Vector3(
      this.packed.nodePositions[index * 3],
      this.packed.nodePositions[index * 3 + 1],
      this.packed.nodePositions[index * 3 + 2],
    );
    const position = target.clone().add(new THREE.Vector3(140, 180, 420));
    this.flyTo(position, target);
  }

  private flyAnimation: { fromPos: THREE.Vector3; toPos: THREE.Vector3; fromTarget: THREE.Vector3; toTarget: THREE.Vector3; start: number; duration: number } | null = null;

  private flyTo(position: THREE.Vector3, target: THREE.Vector3, duration = 650): void {
    if (this.reducedMotion) {
      this.camera.position.copy(position);
      this.controls.target.copy(target);
      this.controls.update();
      return;
    }
    this.flyAnimation = {
      fromPos: this.camera.position.clone(),
      toPos: position.clone(),
      fromTarget: this.controls.target.clone(),
      toTarget: target.clone(),
      start: performance.now(),
      duration,
    };
  }

  private stepFly(now: number): void {
    const flight = this.flyAnimation;
    if (!flight) return;
    const t = Math.min(1, (now - flight.start) / flight.duration);
    const eased = 1 - Math.pow(1 - t, 3); // easeOutCubic
    this.camera.position.lerpVectors(flight.fromPos, flight.toPos, eased);
    this.controls.target.lerpVectors(flight.fromTarget, flight.toTarget, eased);
    if (t >= 1) this.flyAnimation = null;
  }

  // ── pointer handling ──

  private onControlsStart(): void {
    // any drag hides the tooltip immediately (§4.3)
    if (this.hoverIndex !== null) {
      this.hoverIndex = null;
      this.callbacks.onHover(null);
    }
    this.flyAnimation = null;
  }

  private onPointerDown = (event: PointerEvent): void => {
    this.pointerDown = { x: event.clientX, y: event.clientY, button: event.button, moved: false };
  };

  private onPointerMove = (event: PointerEvent): void => {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointerScreen = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    this.pointerNdc.set(
      (this.pointerScreen.x / rect.width) * 2 - 1,
      -(this.pointerScreen.y / rect.height) * 2 + 1,
    );
    this.pointerDirty = true;
    if (this.pointerDown) {
      const dist = Math.hypot(event.clientX - this.pointerDown.x, event.clientY - this.pointerDown.y);
      if (dist > CLICK_SLOP_PX) this.pointerDown.moved = true;
    }
  };

  private onPointerUp = (event: PointerEvent): void => {
    const down = this.pointerDown;
    this.pointerDown = null;
    if (!down || down.moved || down.button !== 0) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointerScreen = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    this.pointerNdc.set(
      (this.pointerScreen.x / rect.width) * 2 - 1,
      -(this.pointerScreen.y / rect.height) * 2 + 1,
    );
    this.emitClick();
  };

  private onPointerLeave = (): void => {
    if (this.hoverIndex !== null) {
      this.hoverIndex = null;
      this.callbacks.onHover(null);
    }
  };

  /**
   * Screen-space picking (§2.4): project stars, nearest within radius wins.
   * Weighted by the on-screen star size so bright giants pick easier.
   */
  private pick(): number | null {
    if (!this.packed || !this.fullPoints) return null;
    const source = this.fullPoints.geometry as THREE.BufferGeometry;
    const positions = source.getAttribute("position") as THREE.BufferAttribute;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const a = new THREE.Vector3();
    let best = -1;
    let bestScore = PICK_RADIUS_PX;

    // только отрисованные звёзды: тултип на куллнутом узле — ложный шанс
    for (let k = 0; k < this.nodeVisibleCount; k++) {
      const i = this.nodeIndexArray ? this.nodeIndexArray[k] : -1;
      if (i < 0) continue;
      a.set(positions.getX(i), positions.getY(i), positions.getZ(i));
      a.project(this.camera);
      if (a.z > 1) continue;
      const sx = ((a.x + 1) / 2) * rect.width;
      const sy = ((1 - a.y) / 2) * rect.height;
      const distPx = Math.hypot(sx - this.pointerScreen.x, sy - this.pointerScreen.y);
      if (distPx < bestScore) {
        bestScore = distPx;
        best = i;
      }
    }
    return best >= 0 ? best : null;
  }

  private emitClick(): void {
    const picked = this.pick();
    if (picked === null) {
      // повторный клик в пустоту: отлёт на прежнюю рамку (если был подлёт)
      if (this.framedPrev) {
        const prev = this.framedPrev;
        this.framedPrev = null;
        this.flyTo(prev.pos, prev.target, 800);
      }
      this.callbacks.onSelect(null);
      return;
    }
    // до-позиция запоминается один раз — до снятия выбора
    if (!this.framedPrev) {
      this.framedPrev = { pos: this.camera.position.clone(), target: this.controls.target.clone() };
    }
    this.flyToStar(picked);
    this.callbacks.onSelect({ index: picked });
  }

  /**
   * Космический подлёт «вплотную»: камера останавливается в 50 юнитах от
   * звезды со своей текущей стороны, звезда — центр-слева (панель справа
   * не перекрывает). Плавная интерполяция позиции и таргета, 900 мс.
   */
  private flyToStar(index: number): void {
    if (!this.packed) return;
    const star = new THREE.Vector3(
      this.packed.nodePositions[index * 3],
      this.packed.nodePositions[index * 3 + 1],
      this.packed.nodePositions[index * 3 + 2],
    );
    const stopDist = 50;
    const dir = this.camera.position.clone().sub(star);
    if (dir.lengthSq() < 1e-6) dir.set(0, 0.3, 1);
    dir.normalize();

    const camPos = star.clone().add(dir.multiplyScalar(stopDist));
    // звезда центр-слева: таргет смещаем вправо по экрану на ~15% ширины кадра
    const viewDir = star.clone().sub(camPos).normalize();
    const right = new THREE.Vector3().crossVectors(viewDir, new THREE.Vector3(0, 1, 0)).normalize();
    const halfWidth = stopDist * Math.tan((this.camera.fov * Math.PI) / 360) * this.camera.aspect;
    const target = star.clone().add(right.multiplyScalar(halfWidth * 0.3));

    this.flyTo(camPos, target, 900);
  }

  private framedPrev: { pos: THREE.Vector3; target: THREE.Vector3 } | null = null;

  private depthCap = Number.POSITIVE_INFINITY;

  /** Связи при выделенной звезде — ×0.3 прозрачности (расфокус сцены). */
  private setEdgeDimmed(dimmed: boolean): void {
    const k = dimmed ? 0.3 : 1;
    for (const mesh of [this.mainEdges, this.supersedesEdges, this.contradictsEdges]) {
      const material = mesh?.material as THREE.LineBasicMaterial | undefined;
      if (!material) continue;
      const base = mesh === this.mainEdges ? 0.22 : mesh === this.supersedesEdges ? 0.6 : 0.75;
      material.opacity = base * k;
    }
  }

  /**
   * M4: слайдер глубины 1-6/∞. Кап уровней BFS от выбранной звезды:
   * уровни глубже капа растворяются продолжением glass-кривой (uniform,
   * мгновенно, без сети); отбор узлов опускает их в конец приоритета.
   */
  setDepth(cap: number | null): void {
    const next = cap ?? Number.POSITIVE_INFINITY;
    if (this.depthCap === next) return;
    this.depthCap = next;
    const uniformValue = Number.isFinite(next) ? next : 99;
    for (const points of [this.fullPoints]) {
      const material = points?.material as THREE.ShaderMaterial | undefined;
      if (material) material.uniforms.uDepthCap.value = uniformValue;
    }
    this.cameraDirty = true;
  }

  /** Тумблер «служебные связи» (related_to weight < 1) — off по умолчанию. */
  setShowAuxiliaryEdges(show: boolean): void {
    if (this.showAuxiliaryEdges === show) return;
    this.showAuxiliaryEdges = show;
    this.cameraDirty = true;
    this.cullEdges(null); // немедленная перестройка
  }

  /**
   * Viewport-culling узлов (фидбек Мастера 1): проецируем звёзды через
   * view-projection матрицу, связный greedy-отбор (итерация 4) —
   * максимум NODE_VISIBLE_CAP, пересборка throttle 150 мс при движении.
   */
  private rebuildVisibleNodes(now: number, force = false): void {
    if (!this.packed || !this.fullPoints || !this.nodeIndex || !this.nodeIndexArray) return;
    if (!force && now - this.lastNodeCull < NODE_CULL_COOLDOWN_MS) return;
    this.lastNodeCull = now;

    const cam = this.camera.position;
    this.camera.updateMatrixWorld();
    const vp = new THREE.Matrix4().multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse);
    const e = vp.elements;
    const positions = this.packed.nodePositions;
    const meta = this.packed.nodeMeta;
    const levels = this.levels;
    const highlight = this.highlightAttr ? (this.highlightAttr.array as Float32Array) : null;
    const margin = NODE_CULL_MARGIN;
    const maxDistSq = VIEW_SPHERE_R * VIEW_SPHERE_R; // сфера видимости

    const candIdx: number[] = (this.candIdx ||= []);
    const candScore: number[] = (this.candScore ||= []);
    candIdx.length = 0;
    candScore.length = 0;

    for (let i = 0; i < this.packed.nodeCount; i++) {
      const x = positions[i * 3];
      const y = positions[i * 3 + 1];
      const z = positions[i * 3 + 2];
      const dx = x - cam.x;
      const dy = y - cam.y;
      const dz = z - cam.z;
      const distSq = dx * dx + dy * dy + dz * dz;
      if (distSq > maxDistSq) continue;

      const cw = e[3] * x + e[7] * y + e[11] * z + e[15];
      if (cw <= 0) continue;
      const nx = (e[0] * x + e[4] * y + e[8] * z + e[12]) / cw;
      const ny = (e[1] * x + e[5] * y + e[9] * z + e[13]) / cw;
      if (nx < -margin || nx > margin || ny < -margin || ny > margin) continue;

      const importance = meta[i * 4 + 2];
      const dist = Math.sqrt(distSq);
      let score = importance * 3 + (1 - dist / 2200) * 2;
      if (levels) {
        const level = levels[i];
        if (level === 0) score += 1000;
        else if (level === 1) score += 200;
        else if (level > 0) score += Math.max(0, 30 - level * 3);
        if (level > this.depthCap) score -= 60;
      }
      if (highlight) {
        if (highlight[i] >= 2) score += 500;
        else if (highlight[i] >= 1) score += 120;
      }
      candIdx.push(i);
      candScore.push(score);
    }

    // связный greedy (итерация 4): seeds + добор соседей выбранных —
    // кадр собирается в «молекулы» со стержнями внутрь набора
    const importance = this.edgeImportance ?? new Float32Array(this.packed.nodeCount);
    for (let i = 0; i < this.packed.nodeCount; i++) importance[i] = meta[i * 4 + 2];
    this.edgeImportance = importance;
    const selection = selectVisibleNodes(
      Int32Array.from(candIdx),
      Float32Array.from(candScore),
      this.packed.adjOffsets,
      this.packed.adjList,
      importance,
      // молекулы: 35 сгустков × 8 узлов — плотные группы со стержнями внутрь
      { seedCount: Math.floor(NODE_VISIBLE_CAP / 8), clusterSize: 8, cap: NODE_VISIBLE_CAP },
    );

    let drawCount = 0;
    const visible = selection.visible;
    for (let i = 0; i < this.packed.nodeCount; i++) {
      if (visible[i]) this.nodeIndexArray[drawCount++] = i;
    }

    this.nodeVisibleCount = drawCount;
    this.nodeIndex.needsUpdate = true;
    this.fullPoints.geometry.setDrawRange(0, drawCount);

    // mirror of the draw set — edge selection reads it
    if (!this.nodeVisible || this.nodeVisible.length < this.packed.nodeCount) {
      this.nodeVisible = new Uint8Array(this.packed.nodeCount);
    }
    this.nodeVisible.set(visible);
  }

  private candIdx: number[] | null = null;
  private candScore: number[] | null = null;

  /**
   * Edge culling + кап 1200 (эталон EVE): выбранные рёбра раскладываются
   * в position/color буферы трёх LineSegments-мешей (main/supersedes/
   * contradicts). Пары вершин копируются из nodePositions, цвета main —
   * из спектра слоёв концов (градиент из коробки). Throttle 150 мс.
   */
  private cullEdges(now: number | null): void {
    if (!this.packed || !this.nodeVisible || !this.mainEdges || !this.supersedesEdges || !this.contradictsEdges) return;
    if (now !== null && now - this.lastEdgeCull < EDGE_CULL_COOLDOWN_MS) return;
    if (now !== null) this.lastEdgeCull = now;

    const importance = this.edgeImportance ?? new Float32Array(this.packed.nodeCount);
    for (let i = 0; i < this.packed.nodeCount; i++) {
      importance[i] = this.packed.nodeMeta[i * 4 + 2];
    }
    this.edgeImportance = importance;
    const stats = { candidates: 0, bothVisible: 0, drawn: 0 };
    this.lastEdgeStats = stats;
    const selected = selectVisibleEdges(
      this.packed.edgeData,
      this.packed.edgeWeights,
      this.packed.edgeTypes,
      importance,
      this.nodeVisible,
      { showAuxiliary: this.showAuxiliaryEdges, cap: EDGE_VISIBLE_CAP, stats },
    );
    if (this.edgeDebug && now !== null && now - this.lastEdgeDebugLog > 2000) {
      this.lastEdgeDebugLog = now;
      console.debug(
        `[fullmap] nodes ${this.nodeVisibleCount}/${this.packed.nodeCount} · edges candidates ${stats.candidates} → drawn ${stats.drawn} (cap ${EDGE_VISIBLE_CAP}) · both-ends-visible ${stats.bothVisible}`,
      );
    }

    const positions = this.packed.nodePositions;
    const meta = this.packed.nodeMeta;
    const nsRgb = this.palette?.namespaceRgb ?? [];

    const mainPos = (this.mainEdges.geometry.getAttribute("position") as THREE.BufferAttribute).array as Float32Array;
    const mainCol = (this.mainEdges.geometry.getAttribute("color") as THREE.BufferAttribute).array as Float32Array;
    const supPos = (this.supersedesEdges.geometry.getAttribute("position") as THREE.BufferAttribute).array as Float32Array;
    const conPos = (this.contradictsEdges.geometry.getAttribute("position") as THREE.BufferAttribute).array as Float32Array;
    let mainN = 0;
    let supN = 0;
    let conN = 0;

    for (let k = 0; k < selected.length; k++) {
      const e = selected[k];
      const src = this.packed.edgeData[e * 3];
      const tgt = this.packed.edgeData[e * 3 + 1];
      const kindName = this.packed.edgeTypes[this.packed.edgeData[e * 3 + 2]] ?? "";

      let buf: Float32Array;
      let slot: number;
      if (kindName === "supersedes") {
        buf = supPos;
        slot = supN++ * 6;
      } else if (kindName === "contradicts") {
        buf = conPos;
        slot = conN++ * 6;
      } else {
        buf = mainPos;
        slot = mainN * 6;
        const cA = nsRgb[meta[src * 4] | 0] ?? [0.54, 0.59, 0.67];
        const cB = nsRgb[meta[tgt * 4] | 0] ?? [0.54, 0.59, 0.67];
        mainCol[slot] = cA[0]; mainCol[slot + 1] = cA[1]; mainCol[slot + 2] = cA[2];
        mainCol[slot + 3] = cB[0]; mainCol[slot + 4] = cB[1]; mainCol[slot + 5] = cB[2];
        mainN++;
      }
      buf[slot] = positions[src * 3];
      buf[slot + 1] = positions[src * 3 + 1];
      buf[slot + 2] = positions[src * 3 + 2];
      buf[slot + 3] = positions[tgt * 3];
      buf[slot + 4] = positions[tgt * 3 + 1];
      buf[slot + 5] = positions[tgt * 3 + 2];
    }

    (this.mainEdges.geometry.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    (this.mainEdges.geometry.getAttribute("color") as THREE.BufferAttribute).needsUpdate = true;
    this.mainEdges.geometry.setDrawRange(0, mainN * 2);
    (this.supersedesEdges.geometry.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    this.supersedesEdges.geometry.setDrawRange(0, supN * 2);
    (this.contradictsEdges.geometry.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    this.contradictsEdges.geometry.setDrawRange(0, conN * 2);
  }

  /** Top-K DOM labels (§7): только видимые звёзды, throttle 140 мс. */
  private refreshLabels(now: number): void {
    if (!this.packed) {
      this.renderLabels([]);
      return;
    }
    if (now - this.lastLabelRefresh < LABEL_COOLDOWN_MS) return;
    this.lastLabelRefresh = now;

    const rect = this.renderer.domElement.getBoundingClientRect();
    const fovScale = rect.height / 2 / Math.tan((this.camera.fov * Math.PI) / 360);
    const starWorldRadius = 3.2;
    const candidates: LabelCandidate[] = [];
    const total = Math.max(this.nodeVisibleCount, 0);
    for (let k = 0; k < total; k++) {
      const i = this.nodeIndexArray ? this.nodeIndexArray[k] : -1;
      if (i < 0) continue;
      const x = this.packed.nodePositions[i * 3];
      const y = this.packed.nodePositions[i * 3 + 1];
      const z = this.packed.nodePositions[i * 3 + 2];
      const v = new THREE.Vector3(x, y, z);
      const depth = v.distanceTo(this.camera.position);
      v.project(this.camera);
      candidates.push({
        index: i,
        x: ((v.x + 1) / 2) * rect.width,
        y: ((1 - v.y) / 2) * rect.height,
        depth,
        behind: v.z > 1,
        radiusPx: (starWorldRadius * fovScale) / Math.max(depth, 1),
        importance: this.packed.nodeMeta[i * 4 + 2],
      });
    }
    this.renderLabels(selectLabeledNodes(candidates, rect.width, rect.height, LABEL_MAX));
  }

  private renderLabels(picks: Array<{ index: number; x: number; y: number }>): void {
    if (!this.packed) return;
    while (this.labels.length < picks.length) {
      const div = document.createElement('div');
      div.className = 'map-label';
      this.labelLayer.appendChild(div);
      this.labels.push(div);
    }
    this.labels.forEach((div, i) => {
      const pick = picks[i];
      if (!pick) {
        div.style.display = 'none';
        return;
      }
      div.textContent = unpackNodeString(this.packed!, pick.index, 1);
      div.style.display = 'block';
      div.style.transform = 'translate(' + pick.x + 'px, ' + (pick.y - 14) + 'px) translate(-50%, -100%)';
    });
  }

  // ── per-frame work ──

  private animate(): void {
    if (this.disposed) return;
    this.frame = requestAnimationFrame(this.animate);
    const now = performance.now();
    const elapsed = this.clock.getElapsedTime();

    this.stepFly(now);
    this.controls.update();

    // время: twinkle звёзд + пульс contradicts
    for (const points of [this.fullPoints]) {
      const material = points?.material as THREE.ShaderMaterial | undefined;
      if (material) material.uniforms.uTime.value = elapsed;
    }
    for (const edges of [this.fullEdges]) {
      const material = edges?.material as THREE.ShaderMaterial | undefined;
      if (material) material.uniforms.uTime.value = elapsed;
    }

    if (this.pointerDirty) {
      this.pointerDirty = false;
      const picked = this.pick();
      if (picked !== this.hoverIndex) {
        this.hoverIndex = picked;
        this.callbacks.onHover(
          picked === null ? null : { index: picked, x: this.pointerScreen.x, y: this.pointerScreen.y },
        );
      }
    }

    // viewport-culling узлов (фидбек 1): throttle 150 мс, как edge-culling
    if (this.cameraDirty) {
      this.rebuildVisibleNodes(now);
    }
    this.cameraDirty = false;

    this.cullEdges(now);
    this.refreshLabels(now);
    this.renderer.render(this.scene, this.camera);
  }

  private resize(): void {
    const width = this.container.clientWidth || 1;
    const height = this.container.clientHeight || 1;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
    const pixelRatio = this.renderer.getPixelRatio();
    for (const points of [this.fullPoints]) {
      const material = points?.material as THREE.ShaderMaterial | undefined;
      if (material) material.uniforms.uPixelRatio.value = pixelRatio;
    }
    // ribbon edges: viewport в физических px, толщина — 2 CSS px
    for (const edges of [this.fullEdges]) {
      const material = edges?.material as THREE.ShaderMaterial | undefined;
      if (!material) continue;
      (material.uniforms.uViewport.value as THREE.Vector2).set(width * pixelRatio, height * pixelRatio);
      material.uniforms.uEdgeWidth.value = 6.0 * pixelRatio;
    }
  }

  private disposeMap(): void {
    for (const object of [this.fullPoints, this.mainEdges, this.supersedesEdges, this.contradictsEdges]) {
      if (!object) continue;
      this.scene.remove(object);
      object.geometry.dispose();
      (object.material as THREE.Material).dispose();
    }
    this.fullPoints = null;
    this.fullEdges = null;
    this.mainEdges = null;
    this.supersedesEdges = null;
    this.contradictsEdges = null;
    this.nodeIndex = null;
    this.nodeIndexArray = null;
    this.nodeVisibleCount = 0;
    this.nodeVisible = null;
    this.edgeImportance = null;
    this.bfsAttr = null;
    this.highlightAttr = null;
    this.nebulaGroup.clear();
    this.renderLabels([]);
  }

  dispose(): void {
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.resizeObserver.disconnect();
    this.disposeMap();
    this.nebulaTexture?.dispose();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
