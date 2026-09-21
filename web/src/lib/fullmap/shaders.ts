// GLSL port of the EVE star-map programs (rendering.ts) onto three.js 3D
// (§2.4): Points with glow stars — opaque core + quadratic halo, importance
// driven, extinguished outline for superseded embers; LineSegments as
// gradient gates. Additive blending everywhere, depthWrite off.
//
// Per-star state carried in attributes, refreshed without rebuilding
// buffers: aBfs (glass level from the click BFS), aHighlight (search
// segment: 1 = member of a hit cluster, 2 = the hit itself).
// Distance fade + fog in the fragment stage melt the far field (§4.4).

export const STAR_VERTEX = /* glsl */ `
attribute float aSize;       // importance 1..5
attribute vec3 aColor;       // namespace spectrum
attribute float aFlags;      // bit0 = frozen (вечный факт)
attribute float aBfs;        // BFS level from selection (-1 = no selection)
attribute float aHighlight;  // search segment: 0 none, 1 cluster member, 2 hit

uniform float uPixelRatio;
uniform float uSizeScale;    // LOD sprite scale by zoom
uniform float uTime;
uniform float uTwinkle;      // 0 when prefers-reduced-motion

varying vec3 vColor;
varying float vGlow;
varying float vFrozen;
varying float vGlass;        // final alpha multiplier from the glass curve
varying float vDesat;
varying float vHighlight;
varying float vFade;         // distance fade to camera
varying float vTwinklePhase;

const float FADE_START = 900.0;
const float FADE_END = 2200.0;

void main() {
  vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
  float dist = -mvPosition.z;                       // camera-space depth

  // Screen-constant star size, эталон созвездия (итерация 3): базовый
  // спрайт 14.5..32.5 px диаметра — крупная графичная точка, дальше
  // глубину читают fade/culling, не мельчание.
  float sizePx = (10.0 + aSize * 4.5) * uSizeScale * uPixelRatio;

  // camera distance fade (§4.4, усилен по фидбеку): near = solid, far = gone
  float fade = 1.0 - smoothstep(FADE_START, FADE_END, dist);
  vFade = fade * fade;

  // glass curve (§4.2) — BFS level drives opacity/desaturation
  float level = aBfs;
  float glass = (level < 0.0) ? 1.0 : mix(0.95, 0.12, smoothstep(0.0, 6.0, level));
  if (level == 0.0) glass = 1.0;
  vGlass = glass;
  vDesat = (level < 0.0) ? 0.0 : smoothstep(0.0, 6.0, level) * 0.85;

  // search segment emphasis: hits burn brighter and larger
  vHighlight = aHighlight;
  float highlightBoost = (aHighlight >= 2.0) ? 1.7 : (aHighlight >= 1.0) ? 1.3 : 1.0;
  sizePx *= highlightBoost;

  // importance glow, same mapping family as the 2D starGlow()
  float glow = (aSize <= 0.0) ? 0.3 : 0.2 + clamp((aSize - 1.0) / 4.0, 0.0, 1.0) * 0.8;
  vGlow = glow * highlightBoost;

  // frozen eternal facts read as ice-tinted stars
  vFrozen = step(0.5, mod(aFlags, 2.0));

  // gentle twinkle: slow per-star phase, disabled for reduced motion
  vTwinklePhase = fract(sin(dot(position.xy, vec2(12.9898, 78.233))) * 43758.5453);

  gl_PointSize = clamp(sizePx, 2.0 * uPixelRatio, 64.0 * uPixelRatio);
  gl_Position = projectionMatrix * mvPosition;

  vColor = aColor;
}
`;

export const STAR_FRAGMENT = /* glsl */ `
precision highp float;

uniform float uTime;
uniform float uTwinkle;
uniform vec3 uFogColor;
uniform vec3 uIceColor;

varying vec3 vColor;
varying float vGlow;
varying float vFrozen;
varying float vGlass;
varying float vDesat;
varying float vHighlight;
varying float vFade;
varying float vTwinklePhase;

void main() {
  // gl_PointCoord: [-0..1]² → centered [-1..1]
  vec2 uv = gl_PointCoord * 2.0 - 1.0;
  float dist = length(uv);
  if (dist > 1.0) discard;

  float twinkle = 1.0 - uTwinkle * 0.28 * (0.5 + 0.5 * sin(uTime * 1.7 + vTwinklePhase * 6.2831));

  // desaturated tint for glassy stars: mix toward luma (§4.2)
  float luma = dot(vColor, vec3(0.2126, 0.7152, 0.0722));
  vec3 color = mix(vColor, vec3(luma), vDesat);
  // frozen granules carry an ice sheen on top of their layer color
  color = mix(color, uIceColor, vFrozen * 0.55);

  // графичное ядро (итерация 3): ~50% диаметра спрайта, резкий край —
  // яркое цветное ядро, не размытый шар; деликатный white-hot только в центре
  float coreR = 0.5;
  float core = 1.0 - smoothstep(coreR * 0.82, coreR * 1.06, dist);
  float hot = 1.0 - smoothstep(0.0, coreR * 0.55, dist);
  vec3 coreColor = mix(color, vec3(1.0), 0.28 * hot);

  // мягкий СВЕТЯЩИЙСЯ ореол (итерация 3): от кромки ядра до края спрайта
  // с плавным квадратичным спадом — сила 0.75, аддитивные перекрытия
  // соседних ореолов читаются туманностью
  float haloT = clamp((dist - coreR) / (1.0 - coreR), 0.0, 1.0);
  float halo = pow(1.0 - haloT, 2.0) * min(vGlow, 1.0);

  float alpha = max(core, halo * 0.75);
  // search hits get a warm rim so they read above their cluster
  float rim = (vHighlight >= 2.0) ? (1.0 - smoothstep(0.55, 1.0, dist)) * 0.35 : 0.0;
  alpha = max(alpha, rim);
  alpha *= vGlass * vFade * twinkle;

  // fog toward the abyss color melts the far plane (§4.4)
  color = mix(color, uFogColor, (1.0 - vFade) * 0.6);

  gl_FragColor = vec4(mix(coreColor, color, haloT) * alpha, alpha);
}
`;

// Ribbon edges (итерация 3): glLineWidth в WebGL мёртв (1px), поэтому каждое
// ребро — экранный квад: 4 вершины (концы A/B × сторона ±1), 6 индексов.
// Vertex строит прямоугольник шириной uEdgeWidth*2 в ЭКРАННЫХ пикселях —
// связи читаются как тонкие цветные нити постоянной толщины.
export const EDGE_VERTEX = /* glsl */ `
attribute vec3 aOther;   // позиция противоположного конца ребра
attribute vec3 aColor;   // per-vertex: gradient across the segment
attribute float aWeight;
attribute float aKind;   // 0 route, 1 supersedes, 2 contradicts
attribute float aEnd;    // 0 → source vertex, 1 → target vertex
attribute float aSide;   // -1 / +1 — сторона ленты
attribute float aHighlight; // both endpoints in a lit cluster

uniform vec2 uViewport;   // px
uniform float uEdgeWidth; // полная толщина в px

varying vec3 vColor;
varying float vAlpha;
varying float vKind;
varying float vEnd;
varying float vPhase;     // pulse phase for contradicts glow waves

const float FADE_START = 1000.0;
const float FADE_END = 2100.0;

void main() {
  vec4 clipA = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  vec4 clipB = projectionMatrix * modelViewMatrix * vec4(aOther, 1.0);
  vec4 clipSelf = mix(clipA, clipB, aEnd);
  float dist = -mix(modelViewMatrix * vec4(position, 1.0), modelViewMatrix * vec4(aOther, 1.0), aEnd).z;

  // screen-space perpendicular: ndc → px → сдвиг обратно в ndc
  vec2 ndcA = clipA.xy / max(clipA.w, 0.0001);
  vec2 ndcB = clipB.xy / max(clipB.w, 0.0001);
  vec2 screenDir = ndcB - ndcA;
  screenDir.x *= uViewport.x * 0.5;
  screenDir.y *= uViewport.y * 0.5;
  float len = length(screenDir);
  vec2 perpPx = (len > 0.0001) ? vec2(-screenDir.y, screenDir.x) / len : vec2(1.0, 0.0);
  vec2 ndcPerp = perpPx / vec2(uViewport.x * 0.5, uViewport.y * 0.5);
  float halfWidth = uEdgeWidth * 0.5;

  // ndc-смещение добавляем до перспективного деления → умножаем на w
  vec4 clip = clipSelf + vec4(ndcPerp * aSide * halfWidth * 2.0 * clipSelf.w, 0.0, 0.0);

  // per-vertex fade: an edge is as strong as its fainter endpoint (§4.4)
  float fade = 1.0 - smoothstep(FADE_START, FADE_END, dist);

  // читаемость без паутины (итерация 2): с капом рёбер хватает скромной базы
  float base = mix(0.15, 0.45, clamp((aWeight - 1.0) / 2.0, 0.0, 1.0));
  // contradicts burns red regardless of endpoint layers (сияние — в пульсе)
  if (aKind > 1.5) base = 0.5;

  vAlpha = base * fade * (1.0 + aHighlight * 1.6);
  // phase from position → per-edge desynced pulse waves
  vPhase = dot(position, vec3(0.0137, 0.0171, 0.0113));
  vEnd = aEnd;
  vKind = aKind;
  vColor = aColor;

  gl_Position = clip;
}
`;

export const EDGE_FRAGMENT = /* glsl */ `
precision highp float;

uniform float uTime;

varying vec3 vColor;
varying float vAlpha;
varying float vKind;
varying float vEnd;
varying float vPhase;

void main() {
  float alpha = vAlpha;

  // dashed jump routes for supersedes: six dim gaps along the gate
  if (vKind > 0.5 && vKind < 1.5) {
    float phase = fract(vEnd * 6.0);
    alpha *= mix(0.15, 1.0, step(phase, 0.55));
  }

  // contradicts СИЯЮТ (фидбек 6): редкий тип, time-based glow wave по ребру
  if (vKind > 1.5) {
    alpha *= 0.55 + 0.45 * sin(uTime * 2.6 + vPhase);
  }

  // чистый градиент цвет-из → цвет-в: fog не подмешиваем, чтобы переход
  // между слоями читался (фидбек 5); таяние дальних делает alpha
  gl_FragColor = vec4(vColor * alpha, alpha);
}
`;
