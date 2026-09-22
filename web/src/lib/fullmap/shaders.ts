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
attribute float aPhase;      // per-star twinkle phase (hash uuid)

uniform float uPixelRatio;
uniform float uSizeScale;
uniform float uTime;
uniform float uTwinkle;      // 0 when prefers-reduced-motion
uniform float uDepthCap;     // M4: кап уровней BFS (99 = бесконечность)
uniform float uFocusBlur;    // 1 = стеклянный расфокус невыбранных

varying vec3 vColor;
varying float vGlow;
varying float vGlass;
varying float vDesat;
varying float vFrozen;
varying float vDimmed;
varying float vHighlight;
varying float vFade;
varying float vTwinklePhase;
varying float vTwinkleAmp;
varying float vTwinkleFreq;
varying float vBlur;
varying float vSunCross;     // кроссфейд Points → 3D-солнце вблизи

const float FADE_START = 1500.0;
const float FADE_END = 3200.0; // == VIEW_SPHERE_R (стык сферы видимости)

void main() {
  vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
  float dist = -mvPosition.z;

  // эталон EVE: мелкие отчётливые точки; магнификация ×4 вплотную (60..400)
  float sizePx = (4.5 + aSize * 1.9) * uSizeScale * uPixelRatio;
  float mag = mix(1.0, 4.0, 1.0 - smoothstep(60.0, 400.0, dist));
  sizePx *= mag;

  float fade = 1.0 - smoothstep(FADE_START, FADE_END, dist);
  vFade = pow(fade, 1.4);

  // кроссфейд с объёмным солнцем: точка тает ближе 250, на 120 уступает сферу
  vSunCross = smoothstep(120.0, 250.0, dist);

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

  vFrozen = step(0.5, mod(aFlags, 2.0));
  vDimmed = step(1.5, mod(floor(aFlags / 2.0), 2.0));

  // перемигивание (фидбек Мастера): заметная амплитуда ±35%, 0.5-1.5 Гц,
  // фаза/частота уникальны per-star; выбранная не мигает; reduced-motion off
  vTwinklePhase = fract(sin(dot(position.xy, vec2(12.9898, 78.233))) * 43758.5453) + aPhase;
  vTwinkleFreq = 0.8 + fract(aPhase * 0.1591549) * 1.6; // 0.125-0.375 Гц — медленное дыхание
  vTwinkleAmp = uTwinkle * 0.35 * step(0.5, level);

  vBlur = (uFocusBlur > 0.5 && level >= 1.0) ? 1.0 : 0.0;

  gl_PointSize = clamp(sizePx, 3.0 * uPixelRatio, 128.0 * uPixelRatio);
  gl_Position = projectionMatrix * mvPosition;

  vColor = aColor;
  float glow = (aSize <= 0.0) ? 0.3 : 0.2 + clamp((aSize - 1.0) / 4.0, 0.0, 1.0) * 0.8;
  vGlow = glow * highlightBoost;
}
`;

export const STAR_FRAGMENT = /* glsl */ `
precision highp float;

uniform float uTime;
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
varying float vTwinkleAmp;
varying float vTwinkleFreq;
varying float vBlur;
varying float vSunCross;

void main() {
  vec2 uv = gl_PointCoord * 2.0 - 1.0;
  float dist = length(uv);
  if (dist > 1.0) discard;

  float luma = dot(vColor, vec3(0.2126, 0.7152, 0.0722));
  vec3 color = mix(vColor, vec3(luma), vDesat);
  color = mix(color, uIceColor, vFrozen * 0.55);

  // эталон EVE: чёткое яркое ядро ~55% диаметра + ЛЁГКИЙ маленький ореол;
  // при расфокусе ядро исчезает — остаётся мягкое пятно
  float coreR = 0.55;
  float core = 1.0 - smoothstep(coreR * 0.86, coreR * 1.04, dist);
  float hot = 1.0 - smoothstep(0.0, coreR * 0.6, dist);
  vec3 coreColor = mix(color, vec3(1.0), 0.3 * hot);
  core *= 1.0 - vBlur * 0.92;

  float haloT = clamp((dist - coreR) / (1.0 - coreR), 0.0, 1.0);
  float halo = pow(1.0 - haloT, 1.8) * min(vGlow, 1.0);
  halo = mix(halo, halo * 1.15 + 0.06, vBlur);

  float alpha = max(core, halo * mix(0.85, 0.7, vBlur));
  float rim = (vHighlight >= 2.0) ? (1.0 - smoothstep(0.55, 1.0, dist)) * 0.35 : 0.0;
  alpha = max(alpha, rim);

  // заметное перемигивание фоновых звёзд (±35% альфы), выбранная не мигает
  alpha *= 1.0 + vTwinkleAmp * sin(uTime * vTwinkleFreq + vTwinklePhase);

  alpha *= vGlass * vFade;
  // кроссфейд с 3D-солнцем вблизи
  alpha *= vSunCross;

  // тонкий hue-перелив ореола (только вне ядра — «не рэйв»)
  float hueW = (0.15 + 0.15 * sin(uTime * 0.8 + vTwinklePhase)) * haloT;
  vec3 shifted = vec3(color.b, color.r, color.g);
  color = mix(color, shifted, hueW);

  vec3 fogged = mix(color, uFogColor, (1.0 - vFade) * 0.6);
  gl_FragColor = vec4(mix(coreColor, fogged, haloT) * alpha, alpha);
}
`;

/**
 * КОРОНА 3D-солнца: billboard-квад ×2 радиуса сферы, аддитивная,
 * радиальный градиент цвет→прозрачность, медленное мерцание (0.3-0.6 Гц)
 * и лёгкий шифт оттенка по периметру («огонь дышит»).
 */
export const CORONA_VERTEX = /* glsl */ `
// NB: instanceMatrix/instanceColor объявляет сам three (USE_INSTANCING
// prefix для InstancedMesh) — свои объявления ломают компиляцию
attribute float aInstSeed;

uniform float uTime;

varying vec3 vLayerColor;
varying float vSeed;
varying vec2 vQuad;

void main() {
  // масштаб инстанса (радиус сферы в юнитах) — из первой колонки матрицы
  float instScale = length(vec3(instanceMatrix[0][0], instanceMatrix[0][1], instanceMatrix[0][2]));
  vec4 mvCenter = modelViewMatrix * instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0);
  // billboard: квад в view-space, радиус короны = 2.2× радиуса сферы (юниты)
  mvCenter.xy += position.xy * instScale * 2.2;
  vLayerColor = instanceColor;
  vSeed = aInstSeed;
  vQuad = position.xy;
  gl_Position = projectionMatrix * mvCenter;
}
`;

export const CORONA_FRAGMENT = /* glsl */ `
precision highp float;

uniform float uTime;

varying vec3 vLayerColor;
varying float vSeed;
varying vec2 vQuad;

void main() {
  float ang = atan(vQuad.y, vQuad.x);

  // рандомная форма: уникальные лепестки per-star (угловой шум радиуса)
  float petalFreq = 3.0 + floor(fract(vSeed * 5.17) * 4.0) * 1.7;
  float deform = 1.0
    + 0.13 * sin(ang * petalFreq + vSeed * 6.2831)
    + 0.07 * sin(ang * 7.3 - vSeed * 3.1);
  float r = length(vQuad) / deform;

  // ── АТМОСФЕРА: глоу от кромки диска наружу (§ фидбек Мастера) ──
  // яркая привязка к кромке (alpha 1.0) + «толстый» мягкий спад ^1.2 от 0.55
  float atmoRise = smoothstep(0.42, 0.60, r);
  float atmoFall = pow(1.0 - smoothstep(0.55, 1.0, r), 1.2);
  float atmo = atmoRise * atmoFall;

  // ── ВСПОЛОХИ: угловой+временной шум (3 гармоники k=2,3,5), ±55% —
  // пламя «дышит» несимметрично
  float flare =
    sin(ang * 2.0 + uTime * 0.9 + vSeed * 6.2831) * 0.55 +
    sin(ang * 3.0 - uTime * 0.7 + vSeed * 3.1) * 0.45 +
    sin(ang * 5.0 + uTime * 1.3 + vSeed * 1.7) * 0.30;
  atmo *= 1.0 + 0.55 * flare;

  // ── ЯВНОЕ КРУГЛОЕ КОЛЬЦО-ГАЛО сразу за кромкой диска (диск в кваде до r≈0.455)
  // тонкое яркое: резко загорается за кромкой, мягко гаснет наружу
  float haloRing = smoothstep(0.46, 0.54, r) * (1.0 - smoothstep(0.64, 0.82, r));
  float outer = (1.0 - smoothstep(0.5, 1.02, r)) * 0.3;

  // рандом per-star: интенсивность 0.5-1.0, мерцание 0.3-0.7 Гц
  float intensity = 0.5 + fract(vSeed * 7.13) * 0.5;
  float freq = 1.88 + fract(vSeed * 3.71) * 2.51;
  float flicker = 0.75 + 0.25 * sin(uTime * freq + vSeed * 6.2831 + ang * 2.2);
  float hueShift = 0.5 + 0.5 * sin(uTime * 0.9 + ang * 3.0 + vSeed * 4.0);
  vec3 tint = mix(vLayerColor, vec3(1.0), 0.25 + 0.2 * hueShift);

  // цвет атмосферы: слой с нагревом к белому у кромки (ярче ободок)
  vec3 atmoTint = mix(vLayerColor, vec3(1.0), 0.45 * atmoRise);

  // ── ГАЛО-КОЛЬЦО: отдельный гарантированный слой (фидбек Мастера: не тонет
  // в атмосфере) — тонкая чёткая линия цвета слоя с белым нагревом, alpha ~0.9.
  // max-композиция, а не сумма: кольцо видно всегда.
  vec3 ringTint = mix(vLayerColor, vec3(1.0), 0.55);
  float ringAlpha = haloRing * 0.9 * intensity; // flicker на линии едва заметен
  float glowAlpha = (atmo * 0.33 + outer * 0.08) * intensity * flicker; // приглушено ×1/3
  float alpha = max(glowAlpha, ringAlpha);
  vec3 col = mix(tint, atmoTint, atmoRise);
  col = mix(col, ringTint, clamp(haloRing + ringAlpha * 0.5, 0.0, 1.0));
  gl_FragColor = vec4(col * alpha, alpha);
}
`;

/**
 * 3D-солнце (головная фича): InstancedMesh-сфера с fbm-плазмой.
 * Палитра — из per-instance цвета слоя (dark/bright производятся тут),
 * вращение поверхности по uTime + per-instance seed, лимб-свечение
 * и кроссфейд с Points по дистанции камеры (появляется ближе 250).
 */
export const SUN_VERTEX = /* glsl */ `
// NB: instanceMatrix/instanceColor объявляет сам three (USE_INSTANCING
// prefix для InstancedMesh) — свои объявления ломают компиляцию
attribute float aInstSeed;

uniform float uTime; // пульс масштаба («солнце дышит»)

varying vec3 vObjPos;
varying vec3 vNormal;
varying vec3 vLayerColor;
varying float vSeed;
varying float vAlpha;

void main() {
  // ПРИГОВОР: vAlpha инвертирована — полная вблизи (подлёт в 50 юнитов),
  // плавное растворение к 250, где Points уже берут своё
  vec4 local = vec4(position * (1.0 + 0.04 * sin(uTime * 1.3 + aInstSeed * 6.2831)), 1.0);
  vec4 world = instanceMatrix * local;
  vec4 mv = modelViewMatrix * world;
  float dist = length(mv.xyz);
  vAlpha = 1.0 - smoothstep(130.0, 250.0, dist);
  vObjPos = position;
  vNormal = normalize(mat3(instanceMatrix) * normal);
  vLayerColor = instanceColor;
  vSeed = aInstSeed;
  gl_Position = projectionMatrix * mv;
}
`;

export const SUN_FRAGMENT = /* glsl */ `
precision highp float;

uniform float uTime;

varying vec3 vObjPos;
varying vec3 vNormal;
varying vec3 vLayerColor;
varying float vSeed;
varying float vAlpha;

float hash13(vec3 p) {
  p = fract(p * 0.1031);
  p += dot(p, p.zyx + 31.32);
  return fract((p.x + p.y) * p.z);
}

float vnoise(vec3 p) {
  vec3 i = floor(p);
  vec3 f = fract(p);
  vec3 u = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(mix(hash13(i), hash13(i + vec3(1.0, 0.0, 0.0)), u.x),
        mix(hash13(i + vec3(0.0, 1.0, 0.0)), hash13(i + vec3(1.0, 1.0, 0.0)), u.x), u.y),
    mix(mix(hash13(i + vec3(0.0, 0.0, 1.0)), hash13(i + vec3(1.0, 0.0, 1.0)), u.x),
        mix(hash13(i + vec3(0.0, 1.0, 1.0)), hash13(i + vec3(1.0, 1.0, 1.0)), u.x), u.y),
    u.z);
}

float fbm(vec3 p) {
  float v = 0.0;
  float a = 0.5;
  for (int i = 0; i < 4; i++) {
    v += a * vnoise(p);
    p = p * 2.1 + vec3(11.7);
    a *= 0.5;
  }
  return v;
}

// медленное вращение поверхности вокруг Y (по uTime + seed)
vec3 spin(vec3 p, float ang) {
  float c = cos(ang);
  float s = sin(ang);
  return vec3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z);
}

void main() {
  // вращающаяся турбулентная поверхность (плазма)
  vec3 sp = spin(vObjPos * 2.2, uTime * 0.12 + vSeed * 6.2831);
  float n = fbm(sp + vec3(uTime * 0.04));
  float n2 = fbm(sp * 3.1 - vec3(uTime * 0.06));

  vec3 dark = vLayerColor * 0.22;
  vec3 mid = vLayerColor;
  vec3 bright = mix(vLayerColor, vec3(1.0), 0.62);

  vec3 surface = mix(dark, mid, smoothstep(0.28, 0.55, n));
  surface = mix(surface, bright, smoothstep(0.55, 0.85, n));
  // яркие прожилки плазмы
  surface += bright * pow(max(0.0, n2 - 0.45), 2.0) * 2.4;

  vec3 N = normalize(vNormal);
  vec3 V = vec3(0.0, 0.0, 1.0); // к камере в view-space
  float facing = max(dot(N, V), 0.0);
  // лимб-свечение: край солнца ярче центра (как на эталоне)
  float limb = pow(1.0 - facing, 2.0);
  surface += bright * limb * 0.85;
  // лёгкая тень центра для объёма
  surface *= 0.55 + 0.45 * facing;

  gl_FragColor = vec4(surface, vAlpha);
}
`;
