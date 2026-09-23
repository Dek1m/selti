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
import { createEdgeBuffers, createEdgeGeometry, createEdgeMaterial, type EdgeLayerBuffers } from "./edgeLayers";
import { selectLabeledNodes, type LabelCandidate } from "./lod";
import { layoutBBox, mixedLayout, FULL_VOLUME, hashUuid, serverLayout, type LayoutBounds, type VolumeBounds } from "./layout";
import { PulseOrchestrator } from "./pulses";
import { unpackNodeString } from "./pack";
import { createSunLayers } from "./sunLayers";
import { FLASH_SLOTS, STAR_FRAGMENT, STAR_VERTEX } from "./shaders";
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
  /** hover moved onto a star (index) or off (null); screen px + star radius */
  onHover: (node: { index: number; x: number; y: number; radiusPx: number } | null) => void;
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
   * актуальные uniforms слоёв, активные импульсы и счётчик пайплайна.
   */
  getDebugInfo(): string {
    if (!this.packed || !this.mainEdges || !this.supersedesEdges || !this.contradictsEdges) {
      return "no edge meshes";
    }
    const drawCount = (mesh: THREE.LineSegments) => mesh.geometry.drawRange.count;
    const uniformsOf = (mesh: THREE.LineSegments) => (mesh.material as THREE.ShaderMaterial).uniforms;
    const starU = this.fullPoints ? (this.fullPoints.material as THREE.ShaderMaterial).uniforms : null;
    const flashes = starU
      ? (starU.uFlashes.value as THREE.Vector2[]).map((v) => `#${v.x.toFixed(0)}@${v.y.toFixed(1)}`).join(",")
      : "none";
    const pulses = (uniformsOf(this.mainEdges).uPulses.value as THREE.Vector4[])
      .map((v) => `e${v.x.toFixed(0)}@${v.y.toFixed(1)}+${v.z.toFixed(1)}s→${v.w.toFixed(0)}`)
      .join(",");
    const info = this.renderer.info.render;

    // первые вершины живого буфера main (NaN-скан позиций узлов ниже)
    const posAttr = this.mainEdges.geometry.getAttribute("position") as THREE.BufferAttribute;
    const raw = [0, 3].map((f) => `${posAttr.getX(f).toFixed(0)},${posAttr.getY(f).toFixed(0)},${posAttr.getZ(f).toFixed(0)}`).join(" / ");
    let nanCount = 0;
    const npos = this.packed.nodePositions;
    for (let i = 0; i < npos.length; i++) if (Number.isNaN(npos[i])) nanCount++;

    return [
      `build ${__BUILD_ID__}`,
      `nodes ${this.nodeVisibleCount}/${this.packed.nodeCount} nan=${nanCount}`,
      `edges main/sup/con drawn ${drawCount(this.mainEdges) / 2}/${drawCount(this.supersedesEdges) / 2}/${drawCount(this.contradictsEdges) / 2} (cap ${EDGE_VISIBLE_CAP}) cand ${this.lastEdgeStats.candidates} both ${this.lastEdgeStats.bothVisible}`,
      `uSpike=${uniformsOf(this.mainEdges).uSpike.value} uDim=${uniformsOf(this.mainEdges).uDim.value} uBase=${uniformsOf(this.mainEdges).uBaseAlpha.value}`,
      `pulses [${pulses}] flashes [${flashes}]`,
      `mainEdge pos0/1: ${raw}`,
      `pipeline calls=${info.calls} tris=${info.triangles} points=${info.points}`,
      `suns used=${this.suns ? this.suns.count : "none"} (cap 40, range 250)`,
      `camera pos=${this.camera.position.x.toFixed(0)},${this.camera.position.y.toFixed(0)},${this.camera.position.z.toFixed(0)} target=${this.controls.target.x.toFixed(0)},${this.controls.target.y.toFixed(0)},${this.controls.target.z.toFixed(0)} dist=${this.camera.position.distanceTo(this.controls.target).toFixed(0)} fly=${this.flyAnimation ? 1 : 0}`,
    ].join(" | ");
  }
  private lastEdgeCull = 0;
  private lastLabelRefresh = 0;
  private cameraDirty = true;
  private clusterRadii = new Map<number, number>();
  private clusterByIndex = new Map<number, PackedCluster>();

  private levels: Int32Array | null = null;
  private smooth(edge0: number, edge1: number, x: number): number {
    const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
    return t * t * (3 - 2 * t);
  }

  private hoverIndex: number | null = null;

  /**
   * Реальный экранный радиус видимого диска звезды (px CSS-вьюпорта):
   * максимум из Points-точки с магнификацией ×4 (вблизи) и инстанс-солнца
   * при подлёте — подписи отступают именно от кромки, а не от центра
   * (правило Мастера: кромка + 5px). Формулы зеркалят STAR_VERTEX
   * (mag = 1+3×(1−smoothstep 60..400)) и updateSuns (кап 12, буст ×2).
   */
  private starScreenRadiusPx(index: number): number {
    if (!this.packed) return 0;
    const importance = this.packed.nodeMeta[index * 4 + 2];
    const star = new THREE.Vector3(
      this.packed.nodePositions[index * 3],
      this.packed.nodePositions[index * 3 + 1],
      this.packed.nodePositions[index * 3 + 2],
    );
    const dist = this.camera.position.distanceTo(star);
    const base = (4.5 + importance * 1.9) * this.renderer.getPixelRatio();
    const mag = 1 + 3 * (1 - this.smooth(60.0, 400.0, dist));
    let radius = (base * mag) / 2;
    if (dist < 250) {
      const rect = this.renderer.domElement.getBoundingClientRect();
      const fovScale = rect.height / 2 / Math.tan((this.camera.fov * Math.PI) / 360);
      const selectedBoost = index === this.selectedNode ? 2.0 : 1.0;
      const scale = Math.min(12, Math.max(1.5, (base * dist) / 1276)) * selectedBoost;
      radius = Math.max(radius, (scale * fovScale) / Math.max(dist, 1));
    }
    return radius;
  }

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
    this.controls.minDistance = 60; // было 120 — выталкивало камеру из близкого фокуса (фидбек Мастера 23.09)
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
    // серверные координаты map_layout + fallback для узлов без строки
    // (Мастер: «координаты гранул должны браться из таблицы»)
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
      serverLayout(uuids, packed.nodePositions, layout.bounds);
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
    // per-star twinkle phase из хэша uuid (фидбек Мастера: уникальная фаза)
    const phases = new Float32Array(n);
    // глобальный индекс звезды для матчинга всполохов (статичен)
    const starIds = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      phases[i] = (hashUuid(uuids[i]) >>> 8) / 0x1000000 * 6.2831;
      starIds[i] = i;
    }
    this.bfsAttr = new THREE.BufferAttribute(bfs, 1);
    this.highlightAttr = new THREE.BufferAttribute(highlight, 1);
    geometry.setAttribute("aBfs", this.bfsAttr);
    geometry.setAttribute("aHighlight", this.highlightAttr);
    geometry.setAttribute("aPhase", new THREE.BufferAttribute(phases, 1));
    geometry.setAttribute("aIndex", new THREE.BufferAttribute(starIds, 1));
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
    this.buildSuns();
    this.rebuildClusterRadii(packed);
    this.rebuildVisibleNodes(performance.now(), true);
    this.cullEdges(null);
    // созвездие: серверные координаты галактики (bbox ±1000) на порядок
    // шире прежнего диска 220 — стартовый кадр подгоняем под фактическое
    // облако, иначе камера конструктора оказывается внутри скопления
    if (layout.kind === "ellipse") this.frameConstellation();
    this.resize();
  }

  /**
   * Стартовый кадр созвездия по фактическому bbox (серверные координаты +
   * fallback): камера над центром облака под прежним ракурсом (0, 620, 950),
   * дистанция покрывает полудиагональ с запасом. Верхний зажим держит
   * центр кадра внутри сферы видимости VIEW_SPHERE_R — дальний туман не
   * съедает середину созвездия даже при широкой выдаче.
   */
  private frameConstellation(): void {
    const packed = this.packed;
    if (!packed) return;
    const bbox = layoutBBox(packed.nodePositions, packed.nodeCount);
    if (!bbox) return;
    const center = new THREE.Vector3(
      (bbox.min[0] + bbox.max[0]) / 2,
      (bbox.min[1] + bbox.max[1]) / 2,
      (bbox.min[2] + bbox.max[2]) / 2,
    );
    const radius = Math.hypot(
      bbox.max[0] - bbox.min[0],
      bbox.max[1] - bbox.min[1],
      bbox.max[2] - bbox.min[2],
    ) / 2;
    const fitDist = (radius / Math.tan((this.camera.fov * Math.PI) / 360)) * 1.12;
    const dist = Math.max(
      this.controls.minDistance,
      Math.min(fitDist, VIEW_SPHERE_R * 0.8),
    );
    const direction = new THREE.Vector3(0, 620, 950).normalize();
    this.camera.position.copy(center).addScaledVector(direction, dist);
    this.controls.target.copy(center);
    this.controls.update();
  }

  /** importance per node, кешируется при load — selectVisibleEdges читает её */
  private edgeImportance: Float32Array | null = null;

  /** InstancedMesh сфер-солнц: 40 инстансов, палитра из цвета слоя. */
  private buildSuns(): void {
    const SUN_CAP = 40;
    const layers = createSunLayers(SUN_CAP);
    this.suns = layers.suns;
    this.halo = layers.halo;
    this.sunsSeed = layers.seed;
    this.scene.add(this.suns);
    this.scene.add(this.halo);
  }

  private halo: THREE.InstancedMesh | null = null;

  private sunsSeed: THREE.InstancedBufferAttribute | null = null;

  /** Сборка близких звёзд (дист < SUN_RANGE) в инстансы солнц. */
  private updateSuns(now: number): void {
    if (!this.suns || !this.packed || !this.sunsSeed) return;
    if (now - this.lastSunUpdate < 120) return;
    this.lastSunUpdate = now;

    const positions = this.packed.nodePositions;
    const meta = this.packed.nodeMeta;
    const nsRgb = this.palette?.namespaceRgb ?? [];
    const cam = this.camera.position;
    const sunRangeSq = 250 * 250;

    const matrix = new THREE.Matrix4();
    const color = new THREE.Color();
    let used = 0;

    for (let k = 0; k < this.nodeVisibleCount && used < 40; k++) {
      const i = this.nodeIndexArray ? this.nodeIndexArray[k] : -1;
      if (i < 0) continue;
      const dx = positions[i * 3] - cam.x;
      const dy = positions[i * 3 + 1] - cam.y;
      const dz = positions[i * 3 + 2] - cam.z;
      const distSq = dx * dx + dy * dy + dz * dz;
      if (distSq > sunRangeSq) continue;

      const dist = Math.sqrt(distSq);
      const sizePx = (4.5 + meta[i * 4 + 2] * 1.9) * this.renderer.getPixelRatio();
      // выбранная звезда — вдвое крупнее (фидбек Мастера)
      const selectedBoost = i === this.selectedNode ? 2.0 : 1.0;
      const scale = Math.min(12, Math.max(1.5, (sizePx * dist) / 1276)) * selectedBoost;

      matrix.makeScale(scale, scale, scale);
      matrix.setPosition(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
      this.suns.setMatrixAt(used, matrix);
      this.halo?.setMatrixAt(used, matrix);

      const nsIdx = meta[i * 4] | 0;
      const rgb = nsRgb[nsIdx] ?? [0.54, 0.59, 0.67];
      color.setRGB(rgb[0], rgb[1], rgb[2]);
      this.suns.setColorAt(used, color);
      this.halo?.setColorAt(used, color);
      this.sunsSeed.setX(used, (hashUuid(`${i}`) >>> 12) / 0x1000000);
      used++;
    }

    this.suns.count = used;
    this.suns.instanceMatrix.needsUpdate = true;
    this.sunsSeed.needsUpdate = true;
    if (this.suns.instanceColor) this.suns.instanceColor.needsUpdate = true;
    if (this.halo) {
      this.halo.count = used;
      this.halo.instanceMatrix.needsUpdate = true;
      if (this.halo.instanceColor) this.halo.instanceColor.needsUpdate = true;
    }
  }


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
        uFocusBlur: { value: 0 },
        // всполохи звезды-истока (пишет оркестратор импульсов)
        uFlashes: {
          value: Array.from({ length: FLASH_SLOTS }, () => new THREE.Vector2(-1, -1000)),
        },
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
   * основной (градиент слой→слой) и янтарные supersedes / красные
   * contradicts. Материал общий — шейдерный EDGE_VERTEX/EDGE_FRAGMENT:
   * базовый вид идентичен прежнему LineBasicMaterial (градиент, фиксиро-
   * ванная alpha слоя), поверх — редкие импульсы по расписанию JS-
   * оркестратора (pulses.ts). Куллинг перезаписывает position/aColor/
   * aEdgeId (кап 700 на меш).
   */
  private buildEdgeLines(): void {
    const cap = EDGE_VISIBLE_CAP;

    const contradicts = this.palette?.contradicts ?? new THREE.Color("#ff7a8a");
    const warn = this.palette?.supersedes ?? new THREE.Color("#ffc15e");

    // main — NormalBlending (additive на плотных линиях белеет, фидбек);
    // спец-слои — additive, как раньше
    this.mainEdges = new THREE.LineSegments(createEdgeGeometry(cap), createEdgeMaterial(0.22, THREE.NormalBlending, this.reducedMotion));
    this.supersedesEdges = new THREE.LineSegments(createEdgeGeometry(cap), createEdgeMaterial(0.6, THREE.AdditiveBlending, this.reducedMotion));
    this.contradictsEdges = new THREE.LineSegments(createEdgeGeometry(cap), createEdgeMaterial(0.75, THREE.AdditiveBlending, this.reducedMotion));

    // живой JS-срез буферов: оркестратор выбирает импульсы из ВИДИМЫХ рёбер
    this.edgeBuffers = {
      main: createEdgeBuffers(cap),
      supersedes: createEdgeBuffers(cap),
      contradicts: createEdgeBuffers(cap),
    };

    // спец-слои монохромны: цвет заливается один раз, куллинг его не трогает
    this.fillEdgeFixedColor(this.supersedesEdges, warn);
    this.fillEdgeFixedColor(this.contradictsEdges, contradicts);

    for (const mesh of [this.mainEdges, this.supersedesEdges, this.contradictsEdges]) {
      mesh.frustumCulled = false;
      this.scene.add(mesh);
    }

    // оркестратор v2: тик 1.5-2.5с, НЕ каждый кадр; reduced-motion — off
    if (!this.reducedMotion) {
      this.pulseOrch = new PulseOrchestrator();
      this.pulseTimer = window.setInterval(() => this.pulseTick(), 700); // было 2000 — частота втрое (Мастер 23.09)
    }
  }

  /**
   * Тик оркестратора: спавн редких импульсов + запись uniform-массивов
   * (рёбра и всполохи звёзд из одного события spawn). Между тиками
   * ноль per-frame CPU: GLSL анимирует по uTime сам.
   */
  private pulseTick(): void {
    if (this.disposed || !this.pulseOrch || !this.edgeBuffers) return;
    // спавн из основного слоя (основная масса рёбер); матч в шейдере — по
    // глобальному id, чужие слои с тем же id не пересекаются
    this.pulseOrch.tick(this.elapsedNow, this.edgeBuffers.main);

    const pulseData = this.pulseOrch.edgeUniformData();
    for (const mesh of [this.mainEdges, this.supersedesEdges, this.contradictsEdges]) {
      const material = mesh?.material as THREE.ShaderMaterial | undefined;
      if (!material) continue;
      const uniforms = material.uniforms.uPulses.value as THREE.Vector4[];
      for (let i = 0; i < uniforms.length; i++) {
        const [edgeId, start, duration, toB] = pulseData[i];
        uniforms[i].set(edgeId, start, duration, toB);
      }
    }

    const starMaterial = this.fullPoints?.material as THREE.ShaderMaterial | undefined;
    if (starMaterial) {
      const flashData = this.pulseOrch.flashUniformData();
      const flashes = starMaterial.uniforms.uFlashes.value as THREE.Vector2[];
      for (let i = 0; i < flashes.length; i++) {
        const [starIndex, start] = flashData[i];
        flashes[i].set(starIndex, start);
      }
    }
  }

  private pulseOrch: PulseOrchestrator | null = null;
  private pulseTimer: number | null = null;
  /** последний elapsed кадра — шкала тиков оркестратора = шкала uTime */
  private elapsedNow = 0;
  private edgeBuffers: { main: EdgeLayerBuffers; supersedes: EdgeLayerBuffers; contradicts: EdgeLayerBuffers } | null = null;

  /** Монохромная заливка aColor спец-слоя (одинаковый rgb на оба конца). */
  private fillEdgeFixedColor(mesh: THREE.LineSegments, color: THREE.Color): void {
    const attr = mesh.geometry.getAttribute("aColor") as THREE.BufferAttribute;
    const arr = attr.array as Float32Array;
    for (let i = 0; i < arr.length; i += 3) {
      arr[i] = color.r;
      arr[i + 1] = color.g;
      arr[i + 2] = color.b;
    }
    attr.needsUpdate = true;
  }

  private suns: THREE.InstancedMesh | null = null;
  private lastSunUpdate = 0;
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

  private selectedNode = -1;

  /** Click glass: BFS from the star, continuous fade by level (§4.2). */
  select(index: number | null): void {
    if (!this.packed) return;
    if (!this.bfsAttr) return;
    this.selectedNode = index ?? -1;
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

  /**
   * Оффсет подлёта камеры при фокусе гранулы (клик по канвасу и по лейблу —
   * единая константа): ~200 юнитов — внутренний край зоны объёмных солнц
   * (250), кольцо-хромосфера и звезда в одном кадре; clamp gl_PointSize
   * 128·pixelRatio не даёт диску распухнуть в кашу (магнификация ×4
   * даёт ~31px у важности 5 — запас до клампа четырёхкратный).
   */
  private static readonly FOCUS_OFFSET = new THREE.Vector3(26, 38, 78); // ~90 юнитов — вплотную, как прежний full-подлёт (фидбек Мастера 23.09)

  focusNode(index: number): void {
    if (!this.packed) return;
    const target = new THREE.Vector3(
      this.packed.nodePositions[index * 3],
      this.packed.nodePositions[index * 3 + 1],
      this.packed.nodePositions[index * 3 + 2],
    );
    const position = target.clone().add(FullMapScene.FOCUS_OFFSET);
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
    // nearest-with-threshold: порог индивидуален — не меньше PICK_RADIUS_PX,
    // но и не меньше всей площади звезды + 8px. Фиксированные 12px ловили
    // только близкие/крупные звёзды: в созвездии клик мимо мелкой звезды
    // считался «кликом в пустоту» и откатывал камеру на прежнюю рамку
    // (фидбек Мастера 23.09: «созвездие перезагружается, не наезжает»).
    let bestScore = Number.POSITIVE_INFINITY;

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
      const threshold = Math.max(PICK_RADIUS_PX, this.starScreenRadiusPx(i) + 8);
      if (distPx <= threshold && distPx < bestScore) {
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
    // единая дистанция подлёта с кликом по лейблу (FOCUS_OFFSET)
    this.focusNode(picked);
    this.callbacks.onSelect({ index: picked });
  }

  private framedPrev: { pos: THREE.Vector3; target: THREE.Vector3 } | null = null;

  private depthCap = Number.POSITIVE_INFINITY;

  /** Связи при выделенной звезде — ×0.3 прозрачности (расфокус сцены). */
  private setEdgeDimmed(dimmed: boolean): void {
    // uDim = 0.7 → (1 - uDim) = ×0.3, гаснет и база, и импульс
    const v = dimmed ? 0.7 : 0;
    for (const mesh of [this.mainEdges, this.supersedesEdges, this.contradictsEdges]) {
      const material = mesh?.material as THREE.ShaderMaterial | undefined;
      if (!material) continue;
      material.uniforms.uDim.value = v;
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
   * Edge culling + кап 700 (эталон EVE): выбранные рёбра раскладываются
   * в position/aColor/aEdgeId буферы трёх LineSegments-мешей (main/
   * supersedes/contradicts). Пары вершин копируются из nodePositions,
   * цвета main — из спектра слоёв концов (градиент), aEdgeId — глобальный
   * индекс ребра (матч импульсов оркестратора). Throttle 150 мс.
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
    const mainCol = (this.mainEdges.geometry.getAttribute("aColor") as THREE.BufferAttribute).array as Float32Array;
    const mainIdAttr = (this.mainEdges.geometry.getAttribute("aEdgeId") as THREE.BufferAttribute).array as Float32Array;
    const supPos = (this.supersedesEdges.geometry.getAttribute("position") as THREE.BufferAttribute).array as Float32Array;
    const supIdAttr = (this.supersedesEdges.geometry.getAttribute("aEdgeId") as THREE.BufferAttribute).array as Float32Array;
    const conPos = (this.contradictsEdges.geometry.getAttribute("position") as THREE.BufferAttribute).array as Float32Array;
    const conIdAttr = (this.contradictsEdges.geometry.getAttribute("aEdgeId") as THREE.BufferAttribute).array as Float32Array;
    const bufMeta = this.edgeBuffers;
    // bufMeta создаётся вместе с мешами в buildEdgeLines; гвард обязателен:
    // tsc -b (web-build в CI) ловит null здесь, а tsc --noEmit на references-
    // only корневом tsconfig — нет (инцидент: прод остался на старом бандле)
    if (!bufMeta) return;
    let mainN = 0;
    let supN = 0;
    let conN = 0;

    for (let k = 0; k < selected.length; k++) {
      const e = selected[k];
      const src = this.packed.edgeData[e * 3];
      const tgt = this.packed.edgeData[e * 3 + 1];
      const kindName = this.packed.edgeTypes[this.packed.edgeData[e * 3 + 2]] ?? "";

      let buf: Float32Array;
      let idAttr: Float32Array;
      let jsIds: Float32Array;
      let jsNodes: Int32Array;
      let slot: number;
      if (kindName === "supersedes") {
        buf = supPos;
        idAttr = supIdAttr;
        jsIds = bufMeta.supersedes.ids;
        jsNodes = bufMeta.supersedes.nodes;
        slot = supN++ * 6;
      } else if (kindName === "contradicts") {
        buf = conPos;
        idAttr = conIdAttr;
        jsIds = bufMeta.contradicts.ids;
        jsNodes = bufMeta.contradicts.nodes;
        slot = conN++ * 6;
      } else {
        buf = mainPos;
        idAttr = mainIdAttr;
        jsIds = bufMeta.main.ids;
        jsNodes = bufMeta.main.nodes;
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
      // глобальный id ребра на обе вершины: импульс матчится в шейдере по
      // нему, перезапись слота куллингом не переносит спайк на чужое ребро
      const edgeSlot = slot / 6;
      idAttr[slot / 3] = e;
      idAttr[slot / 3 + 1] = e;
      // JS-срез для оркестратора: выбор случайного видимого ребра + исток
      jsIds[edgeSlot] = e;
      jsNodes[edgeSlot * 2] = src;
      jsNodes[edgeSlot * 2 + 1] = tgt;
    }
    // пустые хвосты срезов — невалидные id (маска -1), чтобы оркестратор и
    // шейдер не подсвечивали мусор за пределами нарисованного набора
    bufMetaFill(bufMeta.main.ids, mainN);
    bufMetaFill(bufMeta.supersedes.ids, supN);
    bufMetaFill(bufMeta.contradicts.ids, conN);
    bufMeta.main.count = mainN;
    bufMeta.supersedes.count = supN;
    bufMeta.contradicts.count = conN;

    (this.mainEdges.geometry.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    (this.mainEdges.geometry.getAttribute("aColor") as THREE.BufferAttribute).needsUpdate = true;
    (this.mainEdges.geometry.getAttribute("aEdgeId") as THREE.BufferAttribute).needsUpdate = true;
    this.mainEdges.geometry.setDrawRange(0, mainN * 2);
    (this.supersedesEdges.geometry.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    (this.supersedesEdges.geometry.getAttribute("aEdgeId") as THREE.BufferAttribute).needsUpdate = true;
    this.supersedesEdges.geometry.setDrawRange(0, supN * 2);
    (this.contradictsEdges.geometry.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    (this.contradictsEdges.geometry.getAttribute("aEdgeId") as THREE.BufferAttribute).needsUpdate = true;
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
        // реальный экранный радиус диска — подпись отступает от кромки
        radiusPx: this.starScreenRadiusPx(i),
        importance: this.packed.nodeMeta[i * 4 + 2],
      });
    }
    this.renderLabels(selectLabeledNodes(candidates, rect.width, rect.height, LABEL_MAX));
  }

  /** Отступ подписи от правой кромки диска звезды (правило Мастера). */
  private static readonly LABEL_EDGE_GAP_PX = 5;

  /**
   * Клик по подписи = выбор гранулы + фокус камеры (фидбек Мастера):
   * тот же коллбек onSelect, что у канвас-клика, камеру ведёт focusNode
   * (умеренный подлёт — с лейбла часто прыжок издалека). Лейблы лежат в
   * отдельном DOM-слое-сиблинге канваса, событие до OrbitControls не
   * доходит — перехват drag исключён конструктивно.
   */
  private onLabelClick = (event: MouseEvent): void => {
    const index = Number((event.currentTarget as HTMLDivElement).dataset.index);
    if (!this.packed || !Number.isInteger(index) || index < 0 || index >= this.packed.nodeCount) return;
    // до-позиция — как у канвас-клика: клик в пустоту позже вернёт рамку
    if (!this.framedPrev) {
      this.framedPrev = { pos: this.camera.position.clone(), target: this.controls.target.clone() };
    }
    this.focusNode(index);
    this.callbacks.onSelect({ index });
  };

  private renderLabels(picks: Array<{ index: number; x: number; y: number; radiusPx: number }>): void {
    if (!this.packed) return;
    const viewW = this.renderer.domElement.clientWidth;
    while (this.labels.length < picks.length) {
      const div = document.createElement('div');
      div.className = 'map-label';
      // пул живёт дольше одного набора: индекс узла читается из dataset
      // в момент клика, обработчик вешается один раз
      div.addEventListener('click', this.onLabelClick);
      this.labelLayer.appendChild(div);
      this.labels.push(div);
    }
    this.labels.forEach((div, i) => {
      const pick = picks[i];
      if (!pick) {
        div.style.display = 'none';
        delete div.dataset.index; // Number('') === 0 — узел 0 по ошибке
        return;
      }
      div.textContent = unpackNodeString(this.packed!, pick.index, 1);
      div.dataset.index = String(pick.index);
      div.style.display = 'block';
      // подпись СПРАВА от кромки диска: x = кромка + 5px, вертикаль — центр
      // звезды; у правого края окна флип влево (кромка − 5px, якорь справа)
      const gap = FullMapScene.LABEL_EDGE_GAP_PX;
      const radiusPx = pick.radiusPx;
      const leftX = pick.x + radiusPx + gap;
      const width = div.offsetWidth;
      if (leftX + width > viewW - 8) {
        div.style.transform =
          'translate(' + (pick.x - radiusPx - gap) + 'px, ' + pick.y + 'px) translate(-100%, -50%)';
      } else {
        div.style.transform = 'translate(' + leftX + 'px, ' + pick.y + 'px) translate(0, -50%)';
      }
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
    // шкала тиков оркестратора = шкала uTime шейдеров (последний кадр;
    // расхождение с реальным моментом тика < один кадр — несущественно)
    this.elapsedNow = elapsed;

    // время: twinkle звёзд + анимация плазмы солнц
    const starMaterial = this.fullPoints?.material as THREE.ShaderMaterial | undefined;
    if (starMaterial) starMaterial.uniforms.uTime.value = elapsed;
    const sunMaterial = this.suns?.material as THREE.ShaderMaterial | undefined;
    if (sunMaterial) sunMaterial.uniforms.uTime.value = elapsed;
    // рёбра: тикаем uTime синаптических импульсов (вся математика в GLSL)
    for (const mesh of [this.mainEdges, this.supersedesEdges, this.contradictsEdges]) {
      const material = mesh?.material as THREE.ShaderMaterial | undefined;
      if (material) material.uniforms.uTime.value = elapsed;
    }
    // гало-кольцо статично (решение Мастера) — uTime ему не нужен

    // 3D-солнца: близкие звёзды (<250 юнитов) — InstancedMesh, throttle 120мс
    this.updateSuns(now);

    if (this.pointerDirty) {
      this.pointerDirty = false;
      const picked = this.pick();
      if (picked !== this.hoverIndex) {
        this.hoverIndex = picked;
        if (picked === null) {
          this.callbacks.onHover(null);
        } else {
          // тултип позиционируется от КРОМКИ диска (правило Мастера:
          // правая кромка + 5px), поэтому отдаём центр звезды на экране
          // и её реальный экранный радиус — не координаты курсора
          const star = new THREE.Vector3(
            this.packed!.nodePositions[picked * 3],
            this.packed!.nodePositions[picked * 3 + 1],
            this.packed!.nodePositions[picked * 3 + 2],
          );
          const rect = this.renderer.domElement.getBoundingClientRect();
          star.project(this.camera);
          this.callbacks.onHover({
            index: picked,
            x: ((star.x + 1) / 2) * rect.width,
            y: ((1 - star.y) / 2) * rect.height,
            radiusPx: this.starScreenRadiusPx(picked),
          });
        }
      }
    }

    // viewport-culling узлов (фидбек 1): throttle 150 мс, как edge-culling
    if (this.cameraDirty) {
      this.rebuildVisibleNodes(now);
    }
    this.cameraDirty = false;

    this.cullEdges(now);
    this.refreshLabels(now);

    // камера-левитация (фидбек Мастера): медленный псевдослучайный дрейф —
    // 3 синусоиды 0.05-0.15 Гц по диагональным осям, амплитуда ~10-14 юнитов
    // (масштаб от дистанции до таргета). Аддитивный оффсет к position ПЕРЕД
    // render, база восстанавливается после — OrbitControls не ломается,
    // во время fly-to дрейф приостановлен, reduced-motion — off.
    const basePos = this.camera.position.clone();
    if (!this.reducedMotion && !this.flyAnimation) {
      // дрейф живёт и при выделенной звезде, но вдвое тише — «плавает в кадре»
      const camDist = this.camera.position.distanceTo(this.controls.target);
      const amp = 12 * Math.min(1.5, Math.max(0.35, camDist / 1500)) * (this.framedPrev ? 0.5 : 1);
      this.camera.position.set(
        basePos.x + Math.sin(elapsed * 0.47 + 1.3) * amp * 0.6 + Math.sin(elapsed * 0.94 + 4.1) * amp * 0.25,
        basePos.y + Math.sin(elapsed * 0.31 + 2.7) * amp * 0.5 + Math.sin(elapsed * 0.83 + 0.6) * amp * 0.3,
        basePos.z + Math.cos(elapsed * 0.41 + 0.9) * amp * 0.6 + Math.sin(elapsed * 0.74 + 3.3) * amp * 0.25,
      );
      this.camera.updateMatrixWorld();
    }

    this.renderer.render(this.scene, this.camera);

    // вернуть базовую позицию, чтобы OrbitControls/дрейф не накапливались
    this.camera.position.copy(basePos);
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
  }

  private disposeMap(): void {
    this.stopPulseOrchestrator();
    for (const object of [this.fullPoints, this.suns, this.halo, this.mainEdges, this.supersedesEdges, this.contradictsEdges]) {
      if (!object) continue;
      this.scene.remove(object);
      object.geometry.dispose();
      (object.material as THREE.Material).dispose();
    }
    this.suns = null;
    this.halo = null;
    this.fullPoints = null;
    this.mainEdges = null;
    this.supersedesEdges = null;
    this.contradictsEdges = null;
    this.edgeBuffers = null;
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

  /** Полная остановка оркестратора (rebuild карты, unmount, reduced-motion). */
  private stopPulseOrchestrator(): void {
    if (this.pulseTimer !== null) {
      window.clearInterval(this.pulseTimer);
      this.pulseTimer = null;
    }
    this.pulseOrch = null;
  }

  dispose(): void {
    this.disposed = true;
    this.stopPulseOrchestrator();
    cancelAnimationFrame(this.frame);
    this.resizeObserver.disconnect();
    this.disposeMap();
    this.nebulaTexture?.dispose();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

/** Хвосты JS-среза за пределами нарисованного набора — невалидный id (-1). */
function bufMetaFill(ids: Float32Array, count: number): void {
  for (let i = count; i < ids.length; i++) ids[i] = -1;
}
