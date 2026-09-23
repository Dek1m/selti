// Регресс-тесты шейдеров: класс «шейдерная программа не компилится →
// слой молча исчезает» (прод-инцидент 23.09: рёбра невидимы, потому что
// переменная `active` — зарезервированное слово GLSL ES).
// Полной WebGL-компиляции в node нет, поэтому инвариант статический:
// ни один идентификатор в исходнике не входит в список зарезервированных
// слов GLSL ES 1.00 / 3.00 (reserved for future use) — такие имена
// отвергаются драйвером (ANGLE) на этапе компиляции.

import { describe, expect, it } from "vitest";
import {
  EDGE_FRAGMENT,
  EDGE_VERTEX,
  FLASH_SLOTS,
  HALO_FRAGMENT,
  HALO_VERTEX,
  PULSE_SLOTS,
  STAR_FRAGMENT,
  STAR_VERTEX,
  SUN_FRAGMENT,
  SUN_VERTEX,
} from "./shaders";

// GLSL ES 1.00 §3.6 + GLSL ES 3.00 §3.6 «reserved for future use»
// (объединение; ANGLE применяет его и к ES 1.00 шейдерам)
const RESERVED = new Set([
  "active", "asm", "cast", "class", "common", "default", "double", "dvec2",
  "dvec3", "dvec4", "enum", "extern", "external", "filter", "fixed", "flat",
  "fvec2", "fvec3", "fvec4", "goto", "half", "hvec2", "hvec3", "hvec4",
  "inline", "input", "interface", "long", "namespace", "noinline", "output",
  "packed", "partition", "public", "resource", "sampler1D",
  "sampler1DShadow", "sampler2DRect", "sampler2DRectShadow", "sampler3D",
  "sampler3DRect", "short", "sizeof", "static", "superp", "template",
  "this", "typedef", "union", "unsigned", "using", "volatile",
]);

function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ");
}

function identifiers(source: string): Set<string> {
  const tokens = stripComments(source).match(/[A-Za-z_]\w*/g) ?? [];
  return new Set(tokens);
}

const SHADERS: Array<[string, string]> = [
  ["STAR_VERTEX", STAR_VERTEX],
  ["STAR_FRAGMENT", STAR_FRAGMENT],
  ["EDGE_VERTEX", EDGE_VERTEX],
  ["EDGE_FRAGMENT", EDGE_FRAGMENT],
  ["HALO_VERTEX", HALO_VERTEX],
  ["HALO_FRAGMENT", HALO_FRAGMENT],
  ["SUN_VERTEX", SUN_VERTEX],
  ["SUN_FRAGMENT", SUN_FRAGMENT],
];

describe("shaders: GLSL reserved words", () => {
  it.each(SHADERS)("%s не использует зарезервированные слова как идентификаторы", (_name, src) => {
    const bad = [...identifiers(src)].filter((id) => RESERVED.has(id));
    expect(bad, `зарезервированные слова GLSL ломают компиляцию программы → слой исчезает молча`).toEqual([]);
  });
});

describe("shaders: контракты uniform/attribute с JS-оркестратором", () => {
  it("EDGE_FRAGMENT читает uPulses размером PULSE_SLOTS", () => {
    expect(EDGE_FRAGMENT).toContain(`uniform vec4 uPulses[${PULSE_SLOTS}]`);
    expect(EDGE_VERTEX).toContain("attribute float aEdgeId");
  });

  it("STAR_VERTEX читает uFlashes размером FLASH_SLOTS", () => {
    expect(STAR_VERTEX).toContain(`uniform vec2 uFlashes[${FLASH_SLOTS}]`);
    expect(STAR_VERTEX).toContain("attribute float aIndex");
  });

  it("в GLSL больше нет GLSL-расписания импульсов (хэш-фазы изъяты в оркестратор)", () => {
    // инцидент-хвост: детерминированное расписание из vSeed удаляло смысл
    // оркестратора; спайк обязан считаться только от uPulses
    expect(EDGE_FRAGMENT).not.toContain("hash11");
    expect(EDGE_FRAGMENT).not.toContain("vSeed");
  });
});
