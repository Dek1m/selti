// GLSL port of the EVE star-map programs (rendering.ts) onto three.js 3D
// (§2.4): Points with glow stars — opaque core + quadratic halo, importance
// driven, extinguished outline for superseded embers; LineSegments as
// gradient gates. Additive blending everywhere, depthWrite off.
//
// Per-star state carried in attributes, refreshed without rebuilding
// buffers: aBfs (glass level from the click BFS), aHighlight (search
// segment: 1 = member of a hit cluster, 2 = the hit itself).
// Distance fade + fog in the fragment stage melt the far field (§4.4).

// Активные импульсы/всполохи на кадр: «единицы» по фидбек Мастера 22.09.
// Единый источник правды для GLSL-массивов и JS-оркестратора (pulses.ts).
export const PULSE_SLOTS = 2;
export const FLASH_SLOTS = 2;

export const STAR_VERTEX = /* glsl */ `
attribute float aSize;       // importance 1..5
attribute float aIndex;      // глобальный индекс звезды (матч всполохов)
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
// всполохи «отдаёт энергию» (v2 импульсов): x — индекс звезды (<0 пусто),
// y — момент старта (сек, шкала uTime); пишутся JS-оркестратором из одного
// события spawn с импульсом ребра
uniform vec2 uFlashes[${FLASH_SLOTS}];

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
varying float vFlash;        // 0..1 затухающий всполох звезды-истока

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

  // всполох звезды-истока в момент старта импульса: мгновенная атака,
  // экспоненциальный спад ~0.5с (TAIL: 1/e за 0.18с, хвост до ~0.5с)
  float flash = 0.0;
  for (int i = 0; i < ${FLASH_SLOTS}; i++) {
    float match = step(abs(aIndex - uFlashes[i].x), 0.5);
    float age = uTime - uFlashes[i].y;
    flash = max(flash, match * exp(-max(age, 0.0) * 5.5));
  }
  vFlash = flash;
  sizePx *= 1.0 + flash * 1.7;

  gl_PointSize = clamp(sizePx, 3.0 * uPixelRatio, 128.0 * uPixelRatio);
  gl_Position = projectionMatrix * mvPosition;

  vColor = aColor;
  float glow = (aSize <= 0.0) ? 0.3 : 0.2 + clamp((aSize - 1.0) / 4.0, 0.0, 1.0) * 0.8;
  vGlow = glow * highlightBoost * (1.0 + flash * 1.4);
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
varying float vFlash;

void main() {
  vec2 uv = gl_PointCoord * 2.0 - 1.0;
  float dist = length(uv);
  if (dist > 1.0) discard;

  float luma = dot(vColor, vec3(0.2126, 0.7152, 0.0722));
  vec3 color = mix(vColor, vec3(luma), vDesat);
  color = mix(color, uIceColor, vFrozen * 0.55);
  // всполох подогревает цвет к white-hot (энергия уходит в импульс)
  color = mix(color, vec3(1.0), vFlash * 0.5);

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

  // хит поиска: аккуратное тонкое кольцо у кромки вместо расплывчатого
  // свечения — тот же приём, что хромосфера 3D-солнц (единый язык)
  float hitRing = (vHighlight >= 2.0)
    ? smoothstep(0.6, 0.7, dist) * (1.0 - smoothstep(0.78, 0.95, dist))
    : 0.0;
  alpha = max(alpha, hitRing * 0.9);

  // заметное перемигивание фоновых звёзд (±35% альфы), выбранная не мигает
  alpha *= 1.0 + vTwinkleAmp * sin(uTime * vTwinkleFreq + vTwinklePhase);

  // всполох добавляет яркость поверх мигания (до glass/fade, чтобы дальние
  // всполохи тоже гасились дистанцией честно)
  alpha *= 1.0 + vFlash * 1.6;

  alpha *= vGlass * vFade;
  // кроссфейд с 3D-солнцем вблизи
  alpha *= vSunCross;

  // тонкий hue-перелив ореола (только вне ядра — «не рэйв»)
  float hueW = (0.15 + 0.15 * sin(uTime * 0.8 + vTwinklePhase)) * haloT;
  vec3 shifted = vec3(color.b, color.r, color.g);
  color = mix(color, shifted, hueW);

  vec3 fogged = mix(color, uFogColor, (1.0 - vFade) * 0.6);
  vec3 outCol = mix(coreColor, fogged, haloT);
  // кольцо хита с тёплым white-hot нагревом — цвет слоя остаётся читаемым
  vec3 ringCol = mix(color, vec3(1.0), 0.45);
  outCol = mix(outCol, ringCol, hitRing);
  gl_FragColor = vec4(outCol * alpha, alpha);
}
`;

/**
 * Рёбра полного графа: LineSegments + кастомный шейдер. Базовый вид
 * повторяет прежний LineBasicMaterial: градиент цвет(исток) → цвет(цель),
 * сплошная линия с фиксированной непрозрачностью слоя (uBaseAlpha).
 * Поверх — редкие световые импульсы (v2, фидбек Мастера 22.09): расписание
 * ведёт JS-оркестратор (pulses.ts) — на экране единицы импульсов, фазы
 * вразнобой, часть рёбер не импульсирует вовсе. Импульс матчится по
 * ГЛОБАЛЬНОМУ id ребра (aEdgeId), поэтому перезапись слотов буфера куллингом
 * не переносит спайк на чужое ребро. uSpike = 0 (prefers-reduced-motion)
 * выключает импульсы полностью, базовый вид ребра не меняется.
 */
export const EDGE_VERTEX = /* glsl */ `
attribute vec3 aColor;  // цвет конца ребра: namespace-спектр или спец-тип
attribute float aT;     // 0 у истока, 1 у цели — интерполируется в vT
attribute float aEdgeId; // глобальный индекс ребра в снапшоте (матч импульсов)

varying vec3 vColor;
varying float vT;
varying float vEdgeId;

void main() {
  vColor = aColor;
  vT = aT;
  vEdgeId = aEdgeId;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

export const EDGE_FRAGMENT = /* glsl */ `
precision highp float;

uniform float uTime;
uniform float uSpike;     // 1 = импульсы включены, 0 = prefers-reduced-motion
uniform float uBaseAlpha; // базовая непрозрачность слоя (main 0.22 / sup 0.6 / con 0.75)
uniform float uDim;       // расфокус при выбранной звезде: 0 нет, 0.7 = ×0.3
// активные импульсы (пишет оркестратор раз в 1-3с, не каждый кадр):
// x — edgeId (≤ -1.0 — слот пуст), y — старт (сек, шкала uTime),
// z — длительность пробега (сек), w — направление (1 = к цели, 0 = к истоку)
uniform vec4 uPulses[${PULSE_SLOTS}];

varying vec3 vColor;
varying float vT;
varying float vEdgeId;

// ── Калибровка импульса ──
const float PULSE_SIGMA = 0.065;      // ширина гауссова пика (доля длины ребра)
const float PULSE_TAIL_DECAY = 16.0;  // спад хвоста позади пика (на 1/16 длины)
const float PULSE_TAIL_GAIN = 0.55;   // яркость хвоста относительно головы
const float PULSE_ALPHA = 0.55;       // аддитивная добавка альфы в пике
const float PULSE_WHITE_HEAT = 0.6;   // подогрев локального цвета ребра к белому

void main() {
  float spike = 0.0;
  for (int i = 0; i < ${PULSE_SLOTS}; i++) {
    float match = step(abs(vEdgeId - uPulses[i].x), 0.5);
    float age = uTime - uPulses[i].y;
    float dur = max(uPulses[i].z, 0.0001);
    float isRunning = step(0.0, age) * step(age, dur);
    float p = clamp(age / dur, 0.0, 1.0);

    // направление: к цели (A→B) или к истоку (B→A); один импульс — одна
    // линия, бьёт строго в одну сторону
    float toB = uPulses[i].w;
    float peak = mix(1.0 - p, p, toB);

    // голова — мягкий гауссов пик; хвост — короткий экспоненциальный,
    // тянется строго ПОЗАДИ движения
    float d = vT - peak;
    float head = exp(-d * d / (2.0 * PULSE_SIGMA * PULSE_SIGMA));
    float behind = (peak - vT) * (toB * 2.0 - 1.0);
    float tail = exp(-max(behind, 0.0) * PULSE_TAIL_DECAY) * step(0.0, behind);

    // мягкое появление без вспышки на узле; у конца хвост тает естественно
    float env = smoothstep(0.0, 0.18, p);
    spike += (head + tail * PULSE_TAIL_GAIN) * PULSE_ALPHA * env * isRunning * match;
  }
  spike = min(spike * uSpike, 1.0);

  // самостоятельный аддитивный слой поверх базового вида: цвет подогревается
  // к белому от ЛОКАЛЬНОГО цвета градиента — namespace-палитра не ломается
  vec3 hot = mix(vColor, vec3(1.0), PULSE_WHITE_HEAT);
  vec3 color = mix(vColor, hot, spike);
  float alpha = (uBaseAlpha + spike) * (1.0 - uDim);
  gl_FragColor = vec4(color, min(alpha, 1.0));
}
`;

/**
 * КОЛЬЦЕВОЕ ГАЛО 3D-солнца (эталон «реальное солнце», разворот Мастера
 * 22.09): billboard-квад ×2.2 радиуса сферы, аддитивная, СТАТИЧНОЕ тонкое
 * ровное кольцо-хромосфера, прижатое к кромке диска, с white-hot
 * внутренним краем и коротким мягким радиальным спадом сразу за кольцом.
 * Никаких лепестков, облачной атмосферы и мерцания — живость даёт
 * плазма поверхности (SUN_FRAGMENT), кольцо стабильно.
 */
export const HALO_VERTEX = /* glsl */ `
// NB: instanceMatrix/instanceColor объявляет сам three (USE_INSTANCING
// prefix для InstancedMesh) — свои объявления ломают компиляцию
varying vec3 vLayerColor;
varying vec2 vQuad;

void main() {
  // масштаб инстанса (радиус сферы в юнитах) — из первой колонки матрицы
  float instScale = length(vec3(instanceMatrix[0][0], instanceMatrix[0][1], instanceMatrix[0][2]));
  vec4 mvCenter = modelViewMatrix * instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0);
  // billboard: квад в view-space, радиус квада = 2.2× радиуса сферы (юниты)
  mvCenter.xy += position.xy * instScale * 2.2;
  vLayerColor = instanceColor;
  vQuad = position.xy;
  gl_Position = projectionMatrix * mvCenter;
}
`;

export const HALO_FRAGMENT = /* glsl */ `
precision highp float;

varying vec3 vLayerColor;
varying vec2 vQuad;

void main() {
  // r в единицах квада: 1.0 = 2.2 радиуса сферы, кромка диска ≈ 0.455
  float r = length(vQuad);

  // ── ХРОМОСФЕРА: тонкое яркое кольцо сразу за кромкой диска ──
  // сфера «дышит» ±4% (SUN_VERTEX), поэтому внутренний край кольца
  // стоит за максимумом раздува (0.455 × 1.04 ≈ 0.473)
  float ring = smoothstep(0.475, 0.505, r) * (1.0 - smoothstep(0.55, 0.63, r));

  // короткий мягкий радиальный спад сразу за кольцом; внутри кромки
  // стартует с нуля и подстилает стык «диск ↔ кольцо» при дыхании сферы
  float falloff = smoothstep(0.42, 0.47, r) * (1.0 - smoothstep(0.47, 0.8, r)) * 0.2;

  float alpha = max(ring, falloff);

  // white-hot внутренний край → тёплый цвет слоя наружу (как на эталоне)
  float heat = 1.0 - smoothstep(0.475, 0.63, r);
  vec3 col = mix(vLayerColor, vec3(1.0), 0.55 * heat);

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
  vec3 sp = spin(vObjPos * 2.2, uTime * 0.35 + vSeed * 6.2831);
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
