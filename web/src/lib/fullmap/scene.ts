// FullMapScene — the three.js layer behind the "Полная карта" mode (M3).
//
//   • Points + ShaderMaterial: the full 15k graph in one draw call, GLSL
//     ported from the 2D star map (glow by importance, namespace colors,
//     ember outlines) plus 3D-specific glass / distance-fade / twinkle.
//   • LineSegments: gradient gates, additive, one draw call, index-culled
//     beyond the fade threshold (§4.4).
//   • Cluster LOD (§M3): far camera collapses the map into cluster "star
//     systems" + aggregate gates; near camera unfolds the full graph.
//   • OrbitControls remapped per the Master's decision: RMB = orbit,
//     LMB = pan, wheel = zoom, damped; click-vs-pan discriminator at 5px.
//   • Search segment (§4.1): hit clusters light up whole — nebula shells
//     at cluster centroids, hits brighter, the rest turns to glass.
//   • Glass (§4.2): click → BFS levels → continuous opacity/desaturation.
//   • Screen-space picking + DOM tooltip + top-K labels (§4.3, §7).

import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { bfsLevels } from "./bfs";
import { collectLabelCandidates, lodModeFor, selectLabeledNodes, type LodMode } from "./lod";
import { unpackNodeString } from "./pack";
import { EDGE_FRAGMENT, EDGE_VERTEX, STAR_FRAGMENT, STAR_VERTEX } from "./shaders";
import type { PackedCluster, PackedMapSnapshot } from "./types";

const CLICK_SLOP_PX = 5;
const PICK_RADIUS_PX = 12;
const LABEL_MAX = 22;
const EDGE_CULL_COOLDOWN_MS = 150;
const LABEL_COOLDOWN_MS = 140;

export interface FullMapSceneCallbacks {
  /** hover moved onto a star (index) or off (null); screen px included */
  onHover: (node: { index: number; x: number; y: number } | null) => void;
  /** click resolved as a star (full mode) or a system (cluster mode) */
  onSelect: (node: { index: number } | { cluster: number } | null) => void;
  /** LOD mode changed — HUD can react */
  onLodChange: (mode: LodMode) => void;
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
  private coarsePointer = false;

  private fullPoints: THREE.Points | null = null;
  private fullEdges: THREE.LineSegments | null = null;
  private clusterPoints: THREE.Points | null = null;
  private clusterEdges: THREE.LineSegments | null = null;
  private nebulaGroup = new THREE.Group();
  private nebulaTexture: THREE.Texture | null = null;
  private labelLayer: HTMLDivElement;

  private bfsAttr: THREE.BufferAttribute | null = null;
  private highlightAttr: THREE.BufferAttribute | null = null;
  private clusterHighlightAttr: THREE.BufferAttribute | null = null;
  private edgeIndex: THREE.BufferAttribute | null = null;
  private edgeIndexArray: Uint32Array | null = null;
  private lastEdgeCull = 0;
  private lastLabelRefresh = 0;
  private clusterRadii = new Map<number, number>();
  private clusterByIndex = new Map<number, PackedCluster>();

  private lodState: LodMode | null = null;
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
    this.coarsePointer = window.matchMedia("(pointer: coarse)").matches;

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
    this.camera.position.set(0, 900, 2600);

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
  load(packed: PackedMapSnapshot): void {
    this.disposeMap();
    this.packed = packed;
    this.levels = null;
    this.hoverIndex = null;
    this.lodState = this.coarsePointer ? "clusters" : null;

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
    geometry.computeBoundingSphere();

    this.fullPoints = new THREE.Points(geometry, this.starMaterial());
    this.fullPoints.frustumCulled = false;
    this.scene.add(this.fullPoints);

    this.buildFullEdges(packed);
    this.buildClusterLevel(packed);
    this.updateLodVisibility(true);
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
        uFogColor: { value: this.palette?.fog ?? new THREE.Color("#060a12") },
        uIceColor: { value: this.palette?.ice ?? new THREE.Color("#7dd3fc") },
      },
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
  }

  /** Full-graph gates: two vertices per edge, per-vertex colors → gradient. */
  private buildFullEdges(packed: PackedMapSnapshot): void {
    const m = packed.edgeCount;
    const positions = new Float32Array(m * 6);
    const colors = new Float32Array(m * 6);
    const weights = new Float32Array(m * 2);
    const kinds = new Float32Array(m * 2);
    const ends = new Float32Array(m * 2);
    const highlights = new Float32Array(m * 2);
    const nsRgb = this.palette?.namespaceRgb ?? [];
    const contradicts = this.palette?.contradicts ?? new THREE.Color("#ff7a8a");

    for (let e = 0; e < m; e++) {
      const src = packed.edgeData[e * 3];
      const tgt = packed.edgeData[e * 3 + 1];
      const typeIdx = packed.edgeData[e * 3 + 2];
      const weight = packed.edgeWeights[e];
      const kindName = packed.edgeTypes[typeIdx] ?? "";
      const kind = kindName === "supersedes" ? 1 : kindName === "contradicts" ? 2 : 0;

      const srcRgb = nsRgb[packed.nodeMeta[src * 4] | 0] ?? [0.54, 0.59, 0.67];
      const tgtRgb = nsRgb[packed.nodeMeta[tgt * 4] | 0] ?? [0.54, 0.59, 0.67];

      positions.set([packed.nodePositions[src * 3], packed.nodePositions[src * 3 + 1], packed.nodePositions[src * 3 + 2]], e * 6);
      positions.set([packed.nodePositions[tgt * 3], packed.nodePositions[tgt * 3 + 1], packed.nodePositions[tgt * 3 + 2]], e * 6 + 3);

      // contradicts burns red regardless of endpoint layers (2D parity)
      const fromRgb = kind === 2 ? [contradicts.r, contradicts.g, contradicts.b] : srcRgb;
      const toRgb = kind === 2 ? [contradicts.r, contradicts.g, contradicts.b] : tgtRgb;
      colors.set(fromRgb, e * 6);
      colors.set(toRgb, e * 6 + 3);

      weights[e * 2] = weight;
      weights[e * 2 + 1] = weight;
      kinds[e * 2] = kind;
      kinds[e * 2 + 1] = kind;
      ends[e * 2] = 0;
      ends[e * 2 + 1] = 1;
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("aColor", new THREE.BufferAttribute(colors, 3));
    geometry.setAttribute("aWeight", new THREE.BufferAttribute(weights, 1));
    geometry.setAttribute("aKind", new THREE.BufferAttribute(kinds, 1));
    geometry.setAttribute("aEnd", new THREE.BufferAttribute(ends, 1));
    geometry.setAttribute("aHighlight", new THREE.BufferAttribute(highlights, 1));

    // index = edge identity; edge-culling rewrites this buffer
    this.edgeIndexArray = new Uint32Array(m * 2);
    for (let e = 0; e < m; e++) {
      this.edgeIndexArray[e * 2] = e * 2;
      this.edgeIndexArray[e * 2 + 1] = e * 2 + 1;
    }
    this.edgeIndex = new THREE.BufferAttribute(this.edgeIndexArray, 1);
    geometry.setIndex(this.edgeIndex);

    const material = new THREE.ShaderMaterial({
      vertexShader: EDGE_VERTEX,
      fragmentShader: EDGE_FRAGMENT,
      uniforms: {
        uFogColor: { value: this.palette?.fog ?? new THREE.Color("#060a12") },
      },
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });

    this.fullEdges = new THREE.LineSegments(geometry, material);
    this.fullEdges.frustumCulled = false;
    this.scene.add(this.fullEdges);
  }

  /** Cluster "star systems": centroid stars sized by membership + gates. */
  private buildClusterLevel(packed: PackedMapSnapshot): void {
    const c = packed.clusters.length;
    if (c === 0) return;

    const positions = new Float32Array(c * 3);
    const colors = new Float32Array(c * 3);
    const sizes = new Float32Array(c);
    const flags = new Float32Array(c);
    const bfs = new Float32Array(c).fill(-1);
    const highlight = new Float32Array(c);
    const nsRgb = this.palette?.namespaceRgb ?? [];

    this.clusterByIndex = new Map(packed.clusters.map((cl) => [cl.index, cl]));

    // cluster radius from the member mean distance to the centroid
    const accDist = new Float64Array(c);
    const accCnt = new Float64Array(c);
    for (let i = 0; i < packed.nodeCount; i++) {
      const clusterIdx = packed.nodeMeta[i * 4 + 1] | 0;
      if (clusterIdx < 0 || clusterIdx >= c) continue;
      const centroid = this.clusterByIndex.get(clusterIdx)?.centroid;
      if (!centroid) continue;
      const dx = packed.nodePositions[i * 3] - centroid[0];
      const dy = packed.nodePositions[i * 3 + 1] - centroid[1];
      const dz = packed.nodePositions[i * 3 + 2] - centroid[2];
      accDist[clusterIdx] += Math.sqrt(dx * dx + dy * dy + dz * dz);
      accCnt[clusterIdx] += 1;
    }

    for (let k = 0; k < c; k++) {
      const cluster = packed.clusters[k];
      const centroid = cluster.centroid;
      positions.set(centroid, k * 3);
      const rgb = nsRgb[packed.clusterNs[cluster.index] | 0] ?? [0.54, 0.59, 0.67];
      colors.set(rgb, k * 3);
      sizes[k] = 3 + Math.sqrt(cluster.members) * 0.8;
      flags[k] = 0;
      this.clusterRadii.set(cluster.index, (accDist[cluster.index] / (accCnt[cluster.index] || 1)) * 2.1 + 40);
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("aColor", new THREE.BufferAttribute(colors, 3));
    geometry.setAttribute("aSize", new THREE.BufferAttribute(sizes, 1));
    geometry.setAttribute("aFlags", new THREE.BufferAttribute(flags, 1));
    geometry.setAttribute("aBfs", new THREE.BufferAttribute(bfs, 1));
    this.clusterHighlightAttr = new THREE.BufferAttribute(highlight, 1);
    geometry.setAttribute("aHighlight", this.clusterHighlightAttr);

    this.clusterPoints = new THREE.Points(geometry, this.starMaterial());
    this.clusterPoints.frustumCulled = false;
    this.scene.add(this.clusterPoints);

    // aggregate gates between systems (weighted by inter-cluster edges)
    const gateWeight = new Map<number, number>();
    for (let e = 0; e < packed.edgeCount; e++) {
      const srcCluster = packed.nodeMeta[packed.edgeData[e * 3] * 4 + 1] | 0;
      const tgtCluster = packed.nodeMeta[packed.edgeData[e * 3 + 1] * 4 + 1] | 0;
      if (srcCluster < 0 || tgtCluster < 0 || srcCluster === tgtCluster) continue;
      const a = Math.min(srcCluster, tgtCluster);
      const b = Math.max(srcCluster, tgtCluster);
      const key = a * c + b;
      gateWeight.set(key, (gateWeight.get(key) ?? 0) + packed.edgeWeights[e]);
    }

    const gateKeys = [...gateWeight.keys()];
    const g = gateKeys.length;
    const gatePositions = new Float32Array(g * 6);
    const gateColors = new Float32Array(g * 6);
    const gateWeights = new Float32Array(g * 2);
    const gateKinds = new Float32Array(g * 2);
    const gateEnds = new Float32Array(g * 2);
    const gateHighlights = new Float32Array(g * 2);

    gateKeys.forEach((key, gi) => {
      const a = Math.floor(key / c);
      const b = key % c;
      const ca = this.clusterByIndex.get(a)!.centroid;
      const cb = this.clusterByIndex.get(b)!.centroid;
      gatePositions.set(ca, gi * 6);
      gatePositions.set(cb, gi * 6 + 3);
      const rgbA = nsRgb[packed.clusterNs[a] | 0] ?? [0.54, 0.59, 0.67];
      const rgbB = nsRgb[packed.clusterNs[b] | 0] ?? [0.54, 0.59, 0.67];
      gateColors.set(rgbA, gi * 6);
      gateColors.set(rgbB, gi * 6 + 3);
      const weight = Math.min(3, 1 + Math.log2(gateWeight.get(key)!));
      gateWeights[gi * 2] = weight;
      gateWeights[gi * 2 + 1] = weight;
    });

    const gateGeometry = new THREE.BufferGeometry();
    gateGeometry.setAttribute("position", new THREE.BufferAttribute(gatePositions, 3));
    gateGeometry.setAttribute("aColor", new THREE.BufferAttribute(gateColors, 3));
    gateGeometry.setAttribute("aWeight", new THREE.BufferAttribute(gateWeights, 1));
    gateGeometry.setAttribute("aKind", new THREE.BufferAttribute(gateKinds, 1));
    gateGeometry.setAttribute("aEnd", new THREE.BufferAttribute(gateEnds, 1));
    gateGeometry.setAttribute("aHighlight", new THREE.BufferAttribute(gateHighlights, 1));

    this.clusterEdges = new THREE.LineSegments(
      gateGeometry,
      new THREE.ShaderMaterial({
        vertexShader: EDGE_VERTEX,
        fragmentShader: EDGE_FRAGMENT,
        uniforms: { uFogColor: { value: this.palette?.fog ?? new THREE.Color("#060a12") } },
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    );
    this.clusterEdges.frustumCulled = false;
    this.scene.add(this.clusterEdges);
  }

  /** Soft radial sprite used as the "туманность" shell around lit regions. */
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
      litClusters.forEach((clusterIdx) => {
        const slot = this.packed!.clusters.findIndex((cl) => cl.index === clusterIdx);
        if (slot >= 0 && slot < clusterArray.length) clusterArray[slot] = 2;
      });
      this.clusterHighlightAttr.needsUpdate = true;
    }
    this.refreshNebulas(litClusters);
  }

  clearSearchSegment(): void {
    if (!this.highlightAttr) return;
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

  private flyTo(position: THREE.Vector3, target: THREE.Vector3): void {
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
      duration: 650,
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
    if (!this.packed) return null;
    const lod = this.lodState ?? "full";
    const source =
      lod === "clusters"
        ? (this.clusterPoints?.geometry as THREE.BufferGeometry | undefined)
        : (this.fullPoints?.geometry as THREE.BufferGeometry | undefined);
    if (!source) return null;
    const count = lod === "clusters" ? this.packed.clusters.length : this.packed.nodeCount;
    const positions = source.getAttribute("position") as THREE.BufferAttribute;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const a = new THREE.Vector3();
    let best = -1;
    let bestScore = PICK_RADIUS_PX;

    for (let i = 0; i < count; i++) {
      a.set(positions.getX(i), positions.getY(i), positions.getZ(i));
      const distance = a.distanceTo(this.camera.position);
      if (distance > 6000) continue;
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
      this.callbacks.onSelect(null);
      return;
    }
    if ((this.lodState ?? "full") === "clusters") {
      // drill into the system: fly close enough to unfold the full graph
      const cluster = this.packed?.clusters[picked];
      if (cluster) {
        const target = new THREE.Vector3(...cluster.centroid);
        this.flyTo(target.clone().add(new THREE.Vector3(0, 260, 620)), target);
      }
      this.callbacks.onSelect({ cluster: picked });
    } else {
      this.callbacks.onSelect({ index: picked });
    }
  }

  // ── per-frame work ──

  private updateLodVisibility(force = false): void {
    if (!this.packed) return;
    const distance = this.camera.position.distanceTo(this.controls.target);
    const next = this.coarsePointer ? "clusters" : lodModeFor(distance, this.lodState);
    if (next === this.lodState && !force) return;
    this.lodState = next;
    if (this.fullPoints) this.fullPoints.visible = next === "full";
    if (this.fullEdges) this.fullEdges.visible = next === "full";
    if (this.clusterPoints) this.clusterPoints.visible = next === "clusters";
    if (this.clusterEdges) this.clusterEdges.visible = next === "clusters";
    if (next === "clusters") this.cullEdges(null);
    this.callbacks.onLodChange(next);
  }

  /**
   * Edge culling (§4.4): edges whose both endpoints sit beyond the fade
   * threshold drop out of the index buffer. Rewritten at most every
   * EDGE_CULL_COOLDOWN_MS while the camera moves — never per frame.
   */
  private cullEdges(now: number | null): void {
    if (!this.packed || !this.fullEdges || !this.edgeIndex || !this.edgeIndexArray) return;
    if (now !== null && now - this.lastEdgeCull < EDGE_CULL_COOLDOWN_MS) return;
    if (now !== null) this.lastEdgeCull = now;

    const cam = this.camera.position;
    const positions = this.packed.nodePositions;
    const cullDist = 3300; // just past FADE_END — fully faded edges cut
    const index = this.edgeIndexArray;
    let written = 0;

    for (let e = 0; e < this.packed.edgeCount; e++) {
      const src = this.packed.edgeData[e * 3];
      const tgt = this.packed.edgeData[e * 3 + 1];
      const dxs = positions[src * 3] - cam.x;
      const dys = positions[src * 3 + 1] - cam.y;
      const dzs = positions[src * 3 + 2] - cam.z;
      const dxt = positions[tgt * 3] - cam.x;
      const dyt = positions[tgt * 3 + 1] - cam.y;
      const dzt = positions[tgt * 3 + 2] - cam.z;
      const nearSrc = dxs * dxs + dys * dys + dzs * dzs < cullDist * cullDist;
      const nearTgt = dxt * dxt + dyt * dyt + dzt * dzt < cullDist * cullDist;
      if (!nearSrc && !nearTgt) continue;
      index[written++] = e * 2;
      index[written++] = e * 2 + 1;
    }
    this.edgeIndex.needsUpdate = true;
    this.fullEdges.geometry.setDrawRange(0, written);
  }

  /** Top-K DOM labels (§7): refresh at a throttled cadence, reuse divs. */
  private refreshLabels(now: number): void {
    if (!this.packed || (this.lodState ?? "full") !== "full") {
      this.renderLabels([]);
      return;
    }
    if (now - this.lastLabelRefresh < LABEL_COOLDOWN_MS) return;
    this.lastLabelRefresh = now;

    const rect = this.renderer.domElement.getBoundingClientRect();
    const fovScale = rect.height / 2 / Math.tan((this.camera.fov * Math.PI) / 360);
    const starWorldRadius = 3.2; // matches the shader's mid-size star
    const candidates = collectLabelCandidates(this.packed, (x, y, z) => {
      const v = new THREE.Vector3(x, y, z);
      const depth = v.distanceTo(this.camera.position);
      v.project(this.camera);
      return {
        x: ((v.x + 1) / 2) * rect.width,
        y: ((1 - v.y) / 2) * rect.height,
        depth,
        behind: v.z > 1,
        // projected star radius in px — the label threshold reads this
        radiusPx: (starWorldRadius * fovScale) / Math.max(depth, 1),
      };
    });
    this.renderLabels(selectLabeledNodes(candidates, rect.width, rect.height, LABEL_MAX));
  }

  private renderLabels(picks: Array<{ index: number; x: number; y: number }>): void {
    if (!this.packed) return;
    while (this.labels.length < picks.length) {
      const div = document.createElement("div");
      div.className = "map-label";
      this.labelLayer.appendChild(div);
      this.labels.push(div);
    }
    this.labels.forEach((div, i) => {
      const pick = picks[i];
      if (!pick) {
        div.style.display = "none";
        return;
      }
      // decode the name only here — the packed blob stays UTF-8 until needed
      div.textContent = unpackNodeString(this.packed!, pick.index, 1);
      div.style.display = "block";
      div.style.transform = `translate(${pick.x}px, ${pick.y - 14}px) translate(-50%, -100%)`;
    });
  }

  private animate(): void {
    if (this.disposed) return;
    this.frame = requestAnimationFrame(this.animate);
    const now = performance.now();

    this.stepFly(now);
    this.controls.update();
    this.updateLodVisibility();

    // sprite LOD: shrink point size when zoomed far out (fill-rate guard)
    const distance = this.camera.position.distanceTo(this.controls.target);
    for (const points of [this.fullPoints, this.clusterPoints]) {
      const material = points?.material as THREE.ShaderMaterial | undefined;
      if (material) {
        material.uniforms.uTime.value = this.clock.getElapsedTime();
        material.uniforms.uSizeScale.value = distance > 2200 ? 0.7 : distance < 700 ? 1.25 : 1;
      }
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

    if ((this.lodState ?? "full") === "full") this.cullEdges(now);
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
    for (const points of [this.fullPoints, this.clusterPoints]) {
      const material = points?.material as THREE.ShaderMaterial | undefined;
      if (material) material.uniforms.uPixelRatio.value = pixelRatio;
    }
  }

  private disposeMap(): void {
    for (const object of [this.fullPoints, this.fullEdges, this.clusterPoints, this.clusterEdges]) {
      if (!object) continue;
      this.scene.remove(object);
      object.geometry.dispose();
      (object.material as THREE.Material).dispose();
    }
    this.fullPoints = null;
    this.fullEdges = null;
    this.clusterPoints = null;
    this.clusterEdges = null;
    this.edgeIndex = null;
    this.edgeIndexArray = null;
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
