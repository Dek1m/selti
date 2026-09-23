// Регресс-тесты чистых фабрик слоёв рёбер: инварианты сборки геометрии
// (aT-ось не перемешивается, пустые слоты помечены) и контракты материала.

import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { EDGE_VISIBLE_CAP } from "./edges";
import { createEdgeBuffers, createEdgeGeometry, createEdgeMaterial } from "./edgeLayers";
import { PULSE_SLOTS } from "./shaders";

describe("createEdgeGeometry", () => {
  const geo = createEdgeGeometry(EDGE_VISIBLE_CAP);

  it("буферы позиций/цвета — по 6 float на ребро (2 вершины × vec3)", () => {
    const pos = geo.getAttribute("position");
    const col = geo.getAttribute("aColor");
    expect(pos.count).toBe(EDGE_VISIBLE_CAP * 2);
    expect(pos.itemSize).toBe(3);
    expect(col.count).toBe(EDGE_VISIBLE_CAP * 2);
  });

  it("ось aT задана один раз: чётная вершина = исток (0), нечётная = цель (1)", () => {
    const t = geo.getAttribute("aT").array as Float32Array;
    expect(t.length).toBe(EDGE_VISIBLE_CAP * 2);
    for (let i = 0; i < EDGE_VISIBLE_CAP; i++) {
      expect(t[i * 2]).toBe(0);
      expect(t[i * 2 + 1]).toBe(1);
    }
  });

  it("пустые слоты aEdgeId помечены -1 — импульсы не подсвечивают мусор", () => {
    const ids = geo.getAttribute("aEdgeId").array as Float32Array;
    expect(ids.length).toBe(EDGE_VISIBLE_CAP * 2);
    expect([...ids]).toEqual(new Array(ids.length).fill(-1));
  });

  it("до первого куллинга drawRange пуст — нулевые атрибуты не рисуются", () => {
    expect(geo.drawRange.count).toBe(0);
  });
});

describe("createEdgeMaterial", () => {
  it("базовый вид: uBaseAlpha слоя, uDim=0", () => {
    const mat = createEdgeMaterial(0.22, THREE.NormalBlending, false);
    expect(mat.uniforms.uBaseAlpha.value).toBe(0.22);
    expect(mat.uniforms.uDim.value).toBe(0);
    expect(mat.blending).toBe(THREE.NormalBlending);
    expect(mat.transparent).toBe(true);
    expect(mat.depthTest).toBe(false);
  });

  it("prefers-reduced-motion → uSpike = 0 (импульсы выключены целиком)", () => {
    expect(createEdgeMaterial(0.6, THREE.AdditiveBlending, true).uniforms.uSpike.value).toBe(0);
    expect(createEdgeMaterial(0.6, THREE.AdditiveBlending, false).uniforms.uSpike.value).toBe(1);
  });

  it("uPulses — PULSE_SLOTS пустых слотов (edgeId = -1)", () => {
    const mat = createEdgeMaterial(0.22, THREE.NormalBlending, false);
    const pulses = mat.uniforms.uPulses.value as THREE.Vector4[];
    expect(pulses.length).toBe(PULSE_SLOTS);
    for (const p of pulses) expect(p.x).toBe(-1);
  });
});

describe("createEdgeBuffers", () => {
  it("пустой JS-срез: count = 0, ids = -1", () => {
    const buf = createEdgeBuffers(EDGE_VISIBLE_CAP);
    expect(buf.count).toBe(0);
    expect(buf.ids.length).toBe(EDGE_VISIBLE_CAP);
    expect(buf.nodes.length).toBe(EDGE_VISIBLE_CAP * 2);
    expect([...buf.ids]).toEqual(new Array(EDGE_VISIBLE_CAP).fill(-1));
  });
});
