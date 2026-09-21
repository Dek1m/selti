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
attribute float aFlags;      // bit0 = frozen, bit1 = погасшая (созвездие)
attribute float aBfs;        // BFS level from selection (-1 = no selection)
attribute float aHighlight;  // search segment: 0 none, 1 cluster member, 2 hit

uniform float uPixelRatio;
uniform float uSizeScale;
uniform float uTime;
uniform float uTwinkle;      // 0 when prefers-reduced-motion
uniform float uDepthCap;     // M4: кап уровней BFS (99 = бесконечность)

varying vec3 vColor;
varying float vGlow;
varying float vGlass;
varying float vDesat;
varying float vFrozen;
varying float vDimmed;
varying float vHighlight;
varying float vFade;
varying float vTwinklePhase;

const float FADE_START = 900.0;
const float FADE_END = 2200.0;

void main() {
  vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
  float dist = -mvPosition.z;

  // эталон созвездия: базовый спрайт 14.5..32.5 px, экранно-постоянный
  float sizePx = (10.0 + aSize * 4.5) * uSizeScale * uPixelRatio;

  float fade = 1.0 - smoothstep(FADE_START, FADE_END, dist);
  vFade = fade * fade;

  // glass curve (§4.2) + M4: уровни глубже капа растворяются
  float level = aBfs;
  float glass = (level < 0.0) ? 1.0 : mix(0.95, 0.12, smoothstep(0.0, 6.0, level));
  if (level == 0.0) glass = 1.0;
  if (level >= 0.0 && level > uDepthCap) {
    float over = smoothstep(uDepthCap, uDepthCap + 2.0, level);
    glass *= 1.0 - 0.88 * over;
  }
  vGlass = glass;
  vDesat = (level < 0.0) ? 0.0 : smoothstep(0.0, 6.0, level) * 0.85;

  vHighlight = aHighlight;
  float highlightBoost = (aHighlight >= 2.0) ? 1.7 : (aHighlight >= 1.0) ? 1.3 : 1.0;
  sizePx *= highlightBoost;

  // importance glow — сила ореола, та же семья что и 2D starGlow()
  float glow = (aSize <= 0.0) ? 0.3 : 0.2 + clamp((aSize - 1.0) / 4.0, 0.0, 1.0) * 0.8;
  vGlow = glow * highlightBoost;

  vFrozen = step(0.5, mod(aFlags, 2.0));
  vDimmed = step(1.5, mod(floor(aFlags / 2.0), 2.0));

  vTwinklePhase = fract(sin(dot(position.xy, vec2(12.9898, 78.233))) * 43758.5453);

  gl_PointSize = clamp(sizePx, 3.0 * uPixelRatio, 64.0 * uPixelRatio);
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
varying float vGlass;
varying float vDesat;
varying float vFrozen;
varying float vDimmed;
varying float vHighlight;
varying float vFade;
varying float vTwinklePhase;

void main() {
  vec2 uv = gl_PointCoord * 2.0 - 1.0;
  float dist = length(uv);
  if (dist > 1.0) discard;

  float twinkle = 1.0 - uTwinkle * 0.24 * (0.5 + 0.5 * sin(uTime * 1.7 + vTwinklePhase * 6.2831));

  float luma = dot(vColor, vec3(0.2126, 0.7152, 0.0722));
  vec3 color = mix(vColor, vec3(luma), vDesat);
  color = mix(color, uIceColor, vFrozen * 0.55);

  // эталон созвездия: графичное ядро ~50% диаметра с резкой кромкой,
  // деликатный white-hot в центре и МЯГКИЙ глоу-ореол силы 0.75 за ядром
  float coreR = 0.5;
  float core = 1.0 - smoothstep(coreR * 0.82, coreR * 1.06, dist);
  float hot = 1.0 - smoothstep(0.0, coreR * 0.55, dist);
  vec3 coreColor = mix(color, vec3(1.0), 0.28 * hot);

  float haloT = clamp((dist - coreR) / (1.0 - coreR), 0.0, 1.0);
  float halo = pow(1.0 - haloT, 2.0) * min(vGlow, 1.0);

  float alpha = max(core, halo * 0.75);
  float rim = (vHighlight >= 2.0) ? (1.0 - smoothstep(0.55, 1.0, dist)) * 0.35 : 0.0;
  alpha = max(alpha, rim);
  // погасшие гранулы созвездия: тлеющий контур вместо полноценной звезды
  float ember = vDimmed * (1.0 - smoothstep(0.3, 1.0, dist)) * 0.28;
  alpha = mix(alpha, ember, vDimmed * 0.75);
  color = mix(color, vec3(luma), vDimmed * 0.6);

  alpha *= vGlass * vFade * twinkle;

  vec3 fogged = mix(color, uFogColor, (1.0 - vFade) * 0.6);
  gl_FragColor = vec4(mix(coreColor, fogged, haloT) * alpha, alpha);
}
`;

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

const float FADE_START = 1100.0;
// узлы выбираются до 2200 — лента с видимым концом обязана доживать
// до дальнего конца и таять там плавно, а не исчезать целиком
const float FADE_END = 2400.0;

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

  // валентные стержни (кристалл): толсто и заметно, полупрозрачно
  float base = mix(0.3, 0.55, clamp((aWeight - 1.0) / 2.0, 0.0, 1.0));
  // contradicts burns red regardless of endpoint layers (сияние — в пульсе)
  if (aKind > 1.5) base = 0.7;

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
