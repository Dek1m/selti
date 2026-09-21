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

const float FADE_START = 1400.0;
const float FADE_END = 3400.0;

void main() {
  vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
  float dist = -mvPosition.z;                       // camera-space depth
  float sizeWorld = (1.6 + aSize * 1.5) * uSizeScale;
  float sizePx = sizeWorld * uPixelRatio * (900.0 / max(dist, 1.0));

  // camera distance fade (§4.4): near = solid, far = dissolving
  vFade = 1.0 - smoothstep(FADE_START, FADE_END, dist);

  // glass curve (§4.2) — BFS level drives opacity/desaturation
  float level = aBfs;
  float glass = (level < 0.0) ? 1.0 : mix(0.95, 0.12, smoothstep(0.0, 6.0, level));
  if (level == 0.0) glass = 1.0;
  vGlass = glass;
  vDesat = (level < 0.0) ? 0.0 : smoothstep(0.0, 6.0, level) * 0.85;

  // search segment emphasis: hits burn brighter and larger
  vHighlight = aHighlight;
  float highlightBoost = (aHighlight >= 2.0) ? 2.1 : (aHighlight >= 1.0) ? 1.45 : 1.0;
  sizePx *= highlightBoost;

  // importance glow, same mapping family as the 2D starGlow()
  float glow = (aSize <= 0.0) ? 0.3 : 0.2 + clamp((aSize - 1.0) / 4.0, 0.0, 1.0) * 0.8;
  vGlow = glow * highlightBoost;

  // frozen eternal facts read as ice-tinted stars
  vFrozen = step(0.5, mod(aFlags, 2.0));

  // gentle twinkle: slow per-star phase, disabled for reduced motion
  vTwinklePhase = fract(sin(dot(position.xy, vec2(12.9898, 78.233))) * 43758.5453);

  gl_PointSize = clamp(sizePx, 1.5 * uPixelRatio, 64.0 * uPixelRatio);
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

  // hot white-shifted core (35% toward white, as the 2D star shader)
  float core = 1.0 - smoothstep(0.0, 0.24, dist);
  vec3 coreColor = mix(color, vec3(1.0), 0.35 * core);

  // quadratic falloff halo, span tuned to leave breathing room in the point
  float haloSpan = 0.28 + vGlow * 0.62;
  float halo = 0.0;
  if (dist > 0.2) {
    halo = pow(max(0.0, 1.0 - (dist - 0.2) / haloSpan), 2.2) * vGlow;
  }

  float alpha = max(core, halo * 0.55);
  // search hits get a warm rim so they read above their cluster
  float rim = (vHighlight >= 2.0) ? (1.0 - smoothstep(0.55, 1.0, dist)) * 0.35 : 0.0;
  alpha = max(alpha, rim);
  alpha *= vGlass * vFade * twinkle;

  // fog toward the abyss color melts the far plane (§4.4)
  color = mix(color, uFogColor, (1.0 - vFade) * 0.6);

  gl_FragColor = vec4(color * alpha, alpha);
}
`;

export const EDGE_VERTEX = /* glsl */ `
attribute vec3 aColor;    // per-vertex: gradient across the segment
attribute float aWeight;
attribute float aKind;    // 0 route, 1 supersedes, 2 contradicts
attribute float aEnd;     // 0 → source vertex, 1 → target vertex
attribute float aHighlight; // both endpoints in a lit cluster

varying vec3 vColor;
varying float vAlpha;
varying float vKind;
varying float vEnd;

const float FADE_START = 1500.0;
const float FADE_END = 3200.0;

void main() {
  vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
  float dist = -mvPosition.z;

  // per-vertex fade: an edge is as strong as its fainter endpoint (§4.4)
  float fade = 1.0 - smoothstep(FADE_START, FADE_END, dist);
  float base = mix(0.05, 0.16, clamp((aWeight - 1.0) / 2.0, 0.0, 1.0));

  vAlpha = base * fade * (1.0 + aHighlight * 1.6);
  vEnd = aEnd;
  vKind = aKind;
  vColor = aColor;

  gl_Position = projectionMatrix * mvPosition;
}
`;

export const EDGE_FRAGMENT = /* glsl */ `
precision highp float;

uniform vec3 uFogColor;

varying vec3 vColor;
varying float vAlpha;
varying float vKind;
varying float vEnd;

void main() {
  float alpha = vAlpha;

  // dashed jump routes for supersedes: six dim gaps along the gate
  if (vKind > 0.5 && vKind < 1.5) {
    float phase = fract(vEnd * 6.0);
    alpha *= mix(0.15, 1.0, step(phase, 0.55));
  }

  vec3 color = mix(vColor, uFogColor, 0.2);
  gl_FragColor = vec4(color * alpha, alpha);
}
`;
