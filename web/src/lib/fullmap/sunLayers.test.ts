// Регресс-тесты фабрики 3D-солнц: instanceColor обязан существовать ДО
// первой компиляции шейдерной программы (прод-инцидент 23.09: ленивое
// setColorAt оставляло программу без define USE_INSTANCING_COLOR →
// «instanceColor: undeclared identifier» → солнца и гало не рисовались).

import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { createSunLayers } from "./sunLayers";

describe("createSunLayers", () => {
  const CAP = 40;
  const layers = createSunLayers(CAP);

  it("РЕГРЕСС: instanceColor инициализирован у солнц и гало до первого рендера", () => {
    expect(layers.suns.instanceColor).not.toBeNull();
    expect(layers.halo.instanceColor).not.toBeNull();
    const sunColor = layers.suns.instanceColor!.array as Float32Array;
    expect(sunColor.length).toBe(CAP * 3);
    // нейтральный белый: define выставится, цвет не красит инстансы
    expect(sunColor.every((v) => v === 1)).toBe(true);
  });

  it("инстансы стартуют пустыми (count = 0), кап буфера = CAP", () => {
    expect(layers.suns.count).toBe(0);
    expect(layers.halo.count).toBe(0);
    expect(layers.suns.instanceMatrix.count).toBe(CAP);
    expect(layers.seed.array.length).toBe(CAP);
  });

  it("гало: аддитивный billboard поверх сцены, без depth-теста", () => {
    const haloMat = layers.halo.material as THREE.ShaderMaterial;
    expect(layers.halo.renderOrder).toBe(5);
    expect(haloMat.blending).toBe(THREE.AdditiveBlending);
    expect(haloMat.depthTest).toBe(false);
    expect(haloMat.depthWrite).toBe(false);
    expect(haloMat.side).toBe(THREE.DoubleSide);
  });
});
