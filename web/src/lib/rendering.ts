// Custom sigma WebGL programs for the EVE star map (§6).
//
//   StarNodeProgram       — nodes as glowing stars: opaque core + quadratic
//                           halo driven by the `glow` attribute; glow < 0
//                           paints an extinguished outline (superseded).
//   HyperspaceEdgeProgram — edges as gradient ribbons from the source star's
//                           color to the target's; `dash > 0.5` renders the
//                           yellow jump-route dashes (supersedes chains).
//   eveDrawNodeHover      — canvas hover label styled as an EVE HUD tag.
//
// All programs keep the PICKING_MODE branch of the stock sigma programs:
// picking renders plain shapes with id-colors, never halos.

import { NodeProgram, EdgeProgram } from "sigma/rendering";
import type { Attributes } from "graphology-types";
import type { NodeDisplayData, EdgeDisplayData, RenderParams } from "sigma/types";
import { resolveCssColor } from "./colors";

interface ProgramInfoLike {
  gl: WebGLRenderingContext;
  uniformLocations: Record<string, WebGLUniformLocation | null>;
}

// ─── shared color packing (same encoding as sigma's floatColor) ───

const PACK_BUFFER = new ArrayBuffer(4);
const PACK_INT = new Int32Array(PACK_BUFFER);
const PACK_FLOAT = new Float32Array(PACK_BUFFER);

const RGBA_TEST = /^\s*rgba?\s*\(/;
const RGBA_EXTRACT = /^\s*rgba?\s*\(\s*([0-9]*)\s*,\s*([0-9]*)\s*,\s*([0-9]*)(?:\s*,\s*(.*)?)?\)\s*$/;

/**
 * Pack a CSS color into sigma's float encoding. `premultiply` scales rgb by
 * alpha: sigma blends with ONE, ONE_MINUS_SRC_ALPHA, so translucent colors
 * must carry their dimming inside rgb or they render at full brightness.
 */
function floatColor(value: string, premultiply = false): number {
  let r = 0;
  let g = 0;
  let b = 0;
  let a = 1;
  if (value[0] === "#") {
    r = parseInt(value.slice(1, 3), 16);
    g = parseInt(value.slice(3, 5), 16);
    b = parseInt(value.slice(5, 7), 16);
    if (value.length === 9) a = parseInt(value.slice(7, 9), 16) / 255;
  } else if (RGBA_TEST.test(value)) {
    const match = value.match(RGBA_EXTRACT);
    if (match) {
      r = +match[1];
      g = +match[2];
      b = +match[3];
      if (match[4]) a = +match[4];
    }
  }
  if (premultiply && a < 1) {
    r = Math.round(r * a);
    g = Math.round(g * a);
    b = Math.round(b * a);
  }
  PACK_INT[0] = ((a * 255) | 0) << 24 | (b & 255) << 16 | (g & 255) << 8 | (r & 255);
  // mask the top bit the same way sigma does, to dodge float-sign artifacts
  PACK_INT[0] = PACK_INT[0] & 0xfeffffff;
  return PACK_FLOAT[0];
}

const GL_FLOAT = 5126;
const GL_UNSIGNED_BYTE = 5121;

// ─── StarNodeProgram ───

interface StarNodeDisplayData extends NodeDisplayData, Attributes {
  /** halo intensity 0..1; negative → extinguished outline */
  glow?: number;
  /** 0..1 focus dimming applied by the screen */
  dim?: number;
}

const STAR_NODE_UNIFORMS = ["u_sizeRatio", "u_correctionRatio", "u_matrix"] as const;

const STAR_NODE_VERTEX_SHADER = /*glsl*/ `
attribute vec4 a_id;
attribute vec4 a_color;
attribute vec2 a_position;
attribute float a_size;
attribute float a_glow;
attribute float a_dim;
attribute float a_angle;

uniform mat3 u_matrix;
uniform float u_sizeRatio;
uniform float u_correctionRatio;

varying vec4 v_color;
varying vec2 v_diffVector;
varying float v_radius;
varying float v_glow;
varying float v_dim;

const float bias = 255.0 / 254.0;

void main() {
  // Core keeps the stock circle sizing; the halo only widens the cover
  // triangle, never the picking footprint.
  float core = a_size * u_correctionRatio / u_sizeRatio * 4.0;
  float haloBoost = 1.0 + max(0.0, a_glow) * 2.2;
  float total = core * haloBoost;

  vec2 diffVector = total * vec2(cos(a_angle), sin(a_angle));
  vec2 position = a_position + diffVector;
  gl_Position = vec4((u_matrix * vec3(position, 1)).xy, 0, 1);

  v_diffVector = diffVector;
  v_radius = core * 0.5;
  v_glow = a_glow;
  v_dim = a_dim;

  #ifdef PICKING_MODE
  v_color = a_id;
  #else
  v_color = a_color;
  #endif

  v_color.a *= bias;
}
`;

const STAR_NODE_FRAGMENT_SHADER = /*glsl*/ `
// highp to match the vertex stage default: u_correctionRatio is shared by
// both shaders, and a precision mismatch kills program linking (WebGL2).
precision highp float;

varying vec4 v_color;
varying vec2 v_diffVector;
varying float v_radius;
varying float v_glow;
varying float v_dim;

uniform float u_correctionRatio;

const vec4 transparent = vec4(0.0, 0.0, 0.0, 0.0);

void main(void) {
  float border = u_correctionRatio * 2.0;
  float dist = length(v_diffVector);

  #ifdef PICKING_MODE
  // only the core star is clickable — the halo is decoration
  if (dist < v_radius)
    gl_FragColor = v_color;
  else
    gl_FragColor = transparent;
  #else
  float coreT = 1.0 - smoothstep(v_radius - border, v_radius + border, dist);

  if (v_glow < 0.0) {
    // Extinguished star: thin outline ring + faint ember inside
    float ringDist = abs(dist - v_radius);
    float ring = 1.0 - smoothstep(border * 1.2, border * 2.6, ringDist);
    float ember = max(0.0, 1.0 - dist / v_radius) * 0.22;
    float alpha = max(ring * 0.7, ember) * 0.85;
    // sigma blends with ONE, ONE_MINUS_SRC_ALPHA — rgb must be premultiplied
    gl_FragColor = vec4(v_color.rgb * alpha, alpha);
  } else {
    // Living star: hot white-shifted core + quadratic falloff halo.
    // Halo span matches the vertex halo boost (core/2 * glow * 2.2),
    // so the falloff reaches zero exactly at the cover-triangle edge.
    float haloSpan = v_radius * max(0.001, v_glow) * 2.2;
    float beyond = max(0.0, dist - v_radius);
    float halo = pow(max(0.0, 1.0 - beyond / haloSpan), 2.2) * v_glow;

    float hot = coreT * max(0.0, 1.0 - dist / max(v_radius, 0.001));
    vec3 rgb = mix(v_color.rgb, vec3(1.0), 0.35 * hot);
    float alpha = max(coreT, halo * 0.55);
    alpha *= 1.0 - v_dim * 0.82;
    gl_FragColor = vec4(rgb * alpha, alpha);
  }
  #endif
}
`;

export class StarNodeProgram<
  N extends Attributes = Attributes,
  E extends Attributes = Attributes,
  G extends Attributes = Attributes,
> extends NodeProgram<(typeof STAR_NODE_UNIFORMS)[number], N, E, G> {
  static ANGLE_1 = 0;
  static ANGLE_2 = (2 * Math.PI) / 3;
  static ANGLE_3 = (4 * Math.PI) / 3;

  getDefinition() {
    return {
      VERTICES: 3,
      VERTEX_SHADER_SOURCE: STAR_NODE_VERTEX_SHADER,
      FRAGMENT_SHADER_SOURCE: STAR_NODE_FRAGMENT_SHADER,
      METHOD: WebGLRenderingContext.TRIANGLES,
      UNIFORMS: STAR_NODE_UNIFORMS,
      ATTRIBUTES: [
        { name: "a_position", size: 2, type: GL_FLOAT },
        { name: "a_size", size: 1, type: GL_FLOAT },
        { name: "a_color", size: 4, type: GL_UNSIGNED_BYTE, normalized: true },
        { name: "a_glow", size: 1, type: GL_FLOAT },
        { name: "a_dim", size: 1, type: GL_FLOAT },
        { name: "a_id", size: 4, type: GL_UNSIGNED_BYTE, normalized: true },
      ],
      CONSTANT_ATTRIBUTES: [{ name: "a_angle", size: 1, type: GL_FLOAT }],
      CONSTANT_DATA: [
        [StarNodeProgram.ANGLE_1],
        [StarNodeProgram.ANGLE_2],
        [StarNodeProgram.ANGLE_3],
      ],
    };
  }

  processVisibleItem(nodeIndex: number, startIndex: number, data: StarNodeDisplayData): void {
    const array = this.array;
    array[startIndex++] = data.x;
    array[startIndex++] = data.y;
    array[startIndex++] = data.size;
    array[startIndex++] = floatColor(data.color);
    array[startIndex++] = data.glow ?? 0.3;
    array[startIndex++] = data.dim ?? 0;
    array[startIndex++] = nodeIndex;
  }

  setUniforms({ sizeRatio, correctionRatio, matrix }: RenderParams, { gl, uniformLocations }: ProgramInfoLike): void {
    gl.uniform1f(uniformLocations.u_sizeRatio, sizeRatio);
    gl.uniform1f(uniformLocations.u_correctionRatio, correctionRatio);
    gl.uniformMatrix3fv(uniformLocations.u_matrix, false, matrix);
  }
}

// ─── HyperspaceEdgeProgram ───

interface HyperspaceEdgeDisplayData extends EdgeDisplayData, Attributes {
  /** rgba() literal at the source end */
  colorFrom?: string;
  /** rgba() literal at the target end */
  colorTo?: string;
  /** >0.5 → dashed jump route */
  dash?: number;
  /** 0..1 focus/LOD dimming applied by the screen */
  dim?: number;
}

const HYPERSPACE_EDGE_UNIFORMS = [
  "u_matrix",
  "u_zoomRatio",
  "u_sizeRatio",
  "u_correctionRatio",
  "u_pixelRatio",
  "u_feather",
  "u_minEdgeThickness",
] as const;

const HYPERSPACE_EDGE_VERTEX_SHADER = /*glsl*/ `
attribute vec4 a_id;
attribute vec4 a_colorFrom;
attribute vec4 a_colorTo;
attribute float a_dash;
attribute float a_dim;
attribute vec2 a_normal;
attribute float a_normalCoef;
attribute vec2 a_positionStart;
attribute vec2 a_positionEnd;
attribute float a_positionCoef;

uniform mat3 u_matrix;
uniform float u_zoomRatio;
uniform float u_sizeRatio;
uniform float u_pixelRatio;
uniform float u_correctionRatio;
uniform float u_minEdgeThickness;
uniform float u_feather;

varying vec4 v_colorFrom;
varying vec4 v_colorTo;
varying float v_coef;
varying float v_dash;
varying float v_dim;
varying vec2 v_normal;
varying float v_thickness;
varying float v_feather;
varying float v_dashCount;

const float bias = 255.0 / 254.0;

void main() {
  vec2 normal = a_normal * a_normalCoef;
  vec2 position = a_positionStart * (1.0 - a_positionCoef) + a_positionEnd * a_positionCoef;

  float normalLength = length(normal);
  vec2 unitNormal = normal / normalLength;

  float pixelsThickness = max(normalLength, u_minEdgeThickness * u_sizeRatio);
  float webGLThickness = pixelsThickness * u_correctionRatio / u_sizeRatio;

  gl_Position = vec4((u_matrix * vec3(position + unitNormal * webGLThickness, 1)).xy, 0, 1);

  v_thickness = webGLThickness / u_zoomRatio;
  v_normal = unitNormal;
  v_feather = u_feather * u_correctionRatio / u_zoomRatio / u_pixelRatio * 2.0;

  // dash pattern lives in graph units, so jump routes scale with the map
  float edgeLength = length(a_positionEnd - a_positionStart);
  v_dashCount = max(2.0, floor(edgeLength / 6.0));

  v_coef = a_positionCoef;
  v_dash = a_dash;
  v_dim = a_dim;

  #ifdef PICKING_MODE
  v_colorFrom = a_id;
  v_colorTo = a_id;
  #else
  v_colorFrom = a_colorFrom;
  v_colorTo = a_colorTo;
  #endif

  v_colorFrom.a *= bias;
  v_colorTo.a *= bias;
}
`;

const HYPERSPACE_EDGE_FRAGMENT_SHADER = /*glsl*/ `
precision mediump float;

varying vec4 v_colorFrom;
varying vec4 v_colorTo;
varying float v_coef;
varying float v_dash;
varying float v_dim;
varying vec2 v_normal;
varying float v_thickness;
varying float v_feather;
varying float v_dashCount;

const vec4 transparent = vec4(0.0, 0.0, 0.0, 0.0);

void main(void) {
  #ifdef PICKING_MODE
  gl_FragColor = v_colorFrom;
  #else
  float dist = length(v_normal) * v_thickness;
  float t = smoothstep(v_thickness - v_feather, v_thickness, dist);

  // gradient gate: source color → target color
  vec4 color = mix(v_colorFrom, v_colorTo, v_coef);

  // jump route dashes (55% duty cycle, dimmed gaps)
  if (v_dash > 0.5) {
    float phase = fract(v_coef * v_dashCount);
    color *= mix(0.18, 0.95, step(phase, 0.55));
  }

  color *= 1.0 - v_dim * 0.85;
  gl_FragColor = mix(color, transparent, t);
  #endif
}
`;

export class HyperspaceEdgeProgram<
  N extends Attributes = Attributes,
  E extends Attributes = Attributes,
  G extends Attributes = Attributes,
> extends EdgeProgram<(typeof HYPERSPACE_EDGE_UNIFORMS)[number], N, E, G> {
  getDefinition() {
    return {
      VERTICES: 6,
      VERTEX_SHADER_SOURCE: HYPERSPACE_EDGE_VERTEX_SHADER,
      FRAGMENT_SHADER_SOURCE: HYPERSPACE_EDGE_FRAGMENT_SHADER,
      METHOD: WebGLRenderingContext.TRIANGLES,
      UNIFORMS: HYPERSPACE_EDGE_UNIFORMS,
      ATTRIBUTES: [
        { name: "a_positionStart", size: 2, type: GL_FLOAT },
        { name: "a_positionEnd", size: 2, type: GL_FLOAT },
        { name: "a_normal", size: 2, type: GL_FLOAT },
        { name: "a_colorFrom", size: 4, type: GL_UNSIGNED_BYTE, normalized: true },
        { name: "a_colorTo", size: 4, type: GL_UNSIGNED_BYTE, normalized: true },
        { name: "a_dash", size: 1, type: GL_FLOAT },
        { name: "a_dim", size: 1, type: GL_FLOAT },
        { name: "a_id", size: 4, type: GL_UNSIGNED_BYTE, normalized: true },
      ],
      CONSTANT_ATTRIBUTES: [
        { name: "a_positionCoef", size: 1, type: GL_FLOAT },
        { name: "a_normalCoef", size: 1, type: GL_FLOAT },
      ],
      CONSTANT_DATA: [
        [0, 1],
        [0, -1],
        [1, 1],
        [1, 1],
        [0, -1],
        [1, -1],
      ],
    };
  }

  processVisibleItem(
    edgeIndex: number,
    startIndex: number,
    sourceData: NodeDisplayData,
    targetData: NodeDisplayData,
    data: HyperspaceEdgeDisplayData,
  ): void {
    const array = this.array;
    const thickness = data.size || 1;
    const x1 = sourceData.x;
    const y1 = sourceData.y;
    const x2 = targetData.x;
    const y2 = targetData.y;
    const colorFrom = floatColor(data.colorFrom ?? data.color, true);
    const colorTo = floatColor(data.colorTo ?? data.color, true);

    // ribbon normal, same trick as the stock rectangle program
    const dx = x2 - x1;
    const dy = y2 - y1;
    let n1 = 0;
    let n2 = 0;
    const len = dx * dx + dy * dy;
    if (len) {
      const inv = thickness / Math.sqrt(len);
      n1 = -dy * inv;
      n2 = dx * inv;
    }

    array[startIndex++] = x1;
    array[startIndex++] = y1;
    array[startIndex++] = x2;
    array[startIndex++] = y2;
    array[startIndex++] = n1;
    array[startIndex++] = n2;
    array[startIndex++] = colorFrom;
    array[startIndex++] = colorTo;
    array[startIndex++] = data.dash ?? 0;
    array[startIndex++] = data.dim ?? 0;
    array[startIndex++] = edgeIndex;
  }

  setUniforms(params: RenderParams, { gl, uniformLocations }: ProgramInfoLike): void {
    gl.uniformMatrix3fv(uniformLocations.u_matrix, false, params.matrix);
    gl.uniform1f(uniformLocations.u_zoomRatio, params.zoomRatio);
    gl.uniform1f(uniformLocations.u_sizeRatio, params.sizeRatio);
    gl.uniform1f(uniformLocations.u_correctionRatio, params.correctionRatio);
    gl.uniform1f(uniformLocations.u_pixelRatio, params.pixelRatio);
    gl.uniform1f(uniformLocations.u_feather, params.antiAliasingFeather);
    gl.uniform1f(uniformLocations.u_minEdgeThickness, params.minEdgeThickness);
  }
}

// ─── hover renderer (canvas 2D) ───

const HOVER_TAG_MAX_WIDTH = 360;
const HOVER_TAG_PAD_X = 8;
const HOVER_TAG_PAD_Y = 5;
const HOVER_TAG_MARGIN = 10;

/** Greedy word wrap with hard breaks for unbroken entity names (snake_case…). */
function wrapHoverLabel(context: CanvasRenderingContext2D, label: string, maxWidth: number): string[] {
  const breakWord = (word: string): string[] => {
    const chunks: string[] = [];
    let chunk = "";
    for (const ch of word) {
      if (chunk && context.measureText(chunk + ch).width > maxWidth) {
        chunks.push(chunk);
        chunk = ch;
      } else {
        chunk += ch;
      }
    }
    if (chunk) chunks.push(chunk);
    return chunks;
  };

  const lines: string[] = [];
  let line = "";
  for (const word of label.split(/\s+/).filter(Boolean)) {
    for (const piece of context.measureText(word).width > maxWidth ? breakWord(word) : [word]) {
      const candidate = line ? `${line} ${piece}` : piece;
      if (line && context.measureText(candidate).width > maxWidth) {
        lines.push(line);
        line = piece;
      } else {
        line = candidate;
      }
    }
  }
  if (line) lines.push(line);
  return lines.length > 0 ? lines : [label];
}

/**
 * EVE-style hover: no stock white bubble — a slim HUD tag next to the star.
 * Sizes to its content (max 360px, wrapped), flips to the left of the star
 * near the right edge and stays clamped inside the viewport. The pulsing
 * focus ring is drawn by the ping overlay canvas, not here.
 */
export function eveDrawNodeHover(
  context: CanvasRenderingContext2D,
  data: { x?: number | null; y?: number | null; size?: number | null; label?: string | null },
  settings: { labelSize: number; labelFont: string; labelWeight: string | number },
): void {
  const label = data.label;
  if (!label || data.x === undefined || data.x === null || data.y === undefined || data.y === null) return;
  const size = data.size ?? settings.labelSize;

  context.font = `${settings.labelWeight} ${settings.labelSize}px ${settings.labelFont}`;

  // viewport bounds in the same (CSS px) space the hover layer draws in
  const transform = context.getTransform();
  const viewW = context.canvas.width / (transform.a || 1);
  const viewH = context.canvas.height / (transform.d || 1);

  const maxTextWidth = HOVER_TAG_MAX_WIDTH - HOVER_TAG_PAD_X * 2;
  const lines = wrapHoverLabel(context, label, maxTextWidth);
  const lineHeight = Math.ceil(settings.labelSize * 1.4);
  const textWidth = Math.max(...lines.map((line) => context.measureText(line).width));

  // Real glyph metrics: canvas fills text from the baseline, and the em-box
  // (labelSize) has nothing to do with how tall the glyphs actually are.
  // Centering on actualBoundingBox keeps the frame symmetric around the ink.
  let ascent = settings.labelSize * 0.82; // fallback if metrics are missing
  let descent = settings.labelSize * 0.28;
  for (const line of lines) {
    const metrics = context.measureText(line);
    ascent = Math.max(ascent, metrics.actualBoundingBoxAscent || 0);
    descent = Math.max(descent, metrics.actualBoundingBoxDescent || 0);
  }

  const boxW = Math.ceil(Math.min(textWidth + HOVER_TAG_PAD_X * 2, HOVER_TAG_MAX_WIDTH));
  const boxH = Math.ceil(ascent + descent + (lines.length - 1) * lineHeight + HOVER_TAG_PAD_Y * 2);

  // левый край рамки = центр звезды + экранный радиус + 10px (фидбек Мастера:
  // тег НЕ на середине звезды), вертикальный центр звезды; флип у правого края
  let x = data.x + (data.size ?? size) + 10;
  if (x + boxW > viewW - HOVER_TAG_MARGIN) {
    x = Math.max(HOVER_TAG_MARGIN, data.x - size - HOVER_TAG_MARGIN - boxW);
  }
  let y = data.y - boxH / 2; // вертикальный центр звезды
  y = Math.min(Math.max(HOVER_TAG_MARGIN, y), Math.max(HOVER_TAG_MARGIN, viewH - boxH - HOVER_TAG_MARGIN));

  context.shadowColor = "rgba(2, 6, 14, 0.8)";
  context.shadowBlur = 10;
  context.fillStyle = resolveCssColor("var(--sl-hud-panel)");
  context.fillRect(x, y, boxW, boxH);
  context.shadowBlur = 0;

  context.strokeStyle = resolveCssColor("var(--sl-hud-frame)");
  context.lineWidth = 1;
  context.strokeRect(x + 0.5, y + 0.5, boxW - 1, boxH - 1);

  // corner tick — the only decoration a working tool may afford
  context.strokeStyle = resolveCssColor("var(--sl-hud-cyan)");
  context.beginPath();
  context.moveTo(x + 0.5, y + 0.5 + 6);
  context.lineTo(x + 0.5, y + 0.5);
  context.lineTo(x + 0.5 + 6, y + 0.5);
  context.stroke();

  context.fillStyle = resolveCssColor("var(--sl-text)");
  const firstBaseline = y + HOVER_TAG_PAD_Y + ascent;
  lines.forEach((line, i) => {
    context.fillText(line, x + HOVER_TAG_PAD_X, firstBaseline + i * lineHeight);
  });
}
