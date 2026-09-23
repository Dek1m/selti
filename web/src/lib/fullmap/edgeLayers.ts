// Pure factories for the three edge LineSegments layers (no DOM) —
// unit-testable. Регресс-инварианты сборки геометрии проверяются тестами:
// aT задаётся один раз (чётная вершина = исток 0, нечётная = цель 1) и
// куллинг пары вершин не перемешивает.

import * as THREE from "three";
import { EDGE_FRAGMENT, EDGE_VERTEX, PULSE_SLOTS } from "./shaders";

/** Живой срез буфера рёбер одного слоя — заполняется в scene.cullEdges. */
export interface EdgeLayerBuffers {
  /** глобальный id ребра по слоту (матч aEdgeId в шейдере + валидация) */
  ids: Float32Array;
  /** глобальные индексы узлов [a, b] по слоту — звезда-исток всполоха */
  nodes: Int32Array;
  /** сколько рёбер сейчас реально в буфере */
  count: number;
}

export function createEdgeGeometry(cap: number): THREE.BufferGeometry {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(cap * 6), 3));
  geometry.setAttribute("aColor", new THREE.BufferAttribute(new Float32Array(cap * 6), 3));
  // ось импульса/градиента: чётная вершина — исток (0), нечётная — цель (1);
  // пары не перемешиваются куллингом, буфер заполняется один раз
  const t = new Float32Array(cap * 2);
  for (let i = 0; i < cap; i++) {
    t[i * 2] = 0;
    t[i * 2 + 1] = 1;
  }
  geometry.setAttribute("aT", new THREE.BufferAttribute(t, 1));
  // глобальный id ребра на обе вершины; -1 = слот пуст (не рисуется)
  geometry.setAttribute("aEdgeId", new THREE.BufferAttribute(new Float32Array(cap * 2).fill(-1), 1));
  geometry.setDrawRange(0, 0);
  return geometry;
}

export function createEdgeMaterial(
  baseAlpha: number,
  blending: THREE.Blending,
  reducedMotion: boolean,
): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    vertexShader: EDGE_VERTEX,
    fragmentShader: EDGE_FRAGMENT,
    uniforms: {
      uTime: { value: 0 },
      uSpike: { value: reducedMotion ? 0 : 1 },
      uBaseAlpha: { value: baseAlpha },
      uDim: { value: 0 },
      // (edgeId ≤ -1 = пусто, start, duration, toB) — пишет оркестратор
      uPulses: {
        value: Array.from({ length: PULSE_SLOTS }, () => new THREE.Vector4(-1, 0, 1, 1)),
      },
    },
    transparent: true,
    blending,
    depthTest: false,
    depthWrite: false,
  });
}

export function createEdgeBuffers(cap: number): EdgeLayerBuffers {
  return {
    ids: new Float32Array(cap).fill(-1),
    nodes: new Int32Array(cap * 2).fill(-1),
    count: 0,
  };
}
