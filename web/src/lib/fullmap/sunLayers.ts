// Pure factories for the 3D suns + chromosphere halo (no DOM) —
// unit-testable.
//
// РЕГРЕСС-ФИКС (рёбра-класс): instanceColor обязан существовать ДО первой
// компиляции шейдерной программы. Three объявляет атрибут instanceColor
// только под define USE_INSTANCING_COLOR, который выставляется при первом
// рендере по факту наличия mesh.instanceColor. Ленивое setColorAt() из
// updateSuns оставляло программу без объявления → «instanceColor:
// undeclared identifier» → солнца и гало молча не рисовались.

import * as THREE from "three";
import { HALO_FRAGMENT, HALO_VERTEX, SUN_FRAGMENT, SUN_VERTEX } from "./shaders";

export interface SunLayers {
  suns: THREE.InstancedMesh;
  halo: THREE.InstancedMesh;
  seed: THREE.InstancedBufferAttribute;
}

export function createSunLayers(cap: number): SunLayers {
  const sphereGeo = new THREE.SphereGeometry(1, 20, 14);
  const seed = new THREE.InstancedBufferAttribute(new Float32Array(cap), 1);
  sphereGeo.setAttribute("aInstSeed", seed);

  const sphereMaterial = new THREE.ShaderMaterial({
    vertexShader: SUN_VERTEX,
    fragmentShader: SUN_FRAGMENT,
    uniforms: { uTime: { value: 0 } },
    transparent: true,
    depthWrite: true,
  });
  const suns = new THREE.InstancedMesh(sphereGeo, sphereMaterial, cap);
  suns.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  // белый по умолчанию → USE_INSTANCING_COLOR определён при первой компиляции
  suns.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(cap * 3).fill(1), 3);
  suns.count = 0;
  suns.frustumCulled = false;

  // кольцевое гало: billboard-квад ×2.2 радиуса, аддитивная, статичное —
  // тонкая хромосфера у кромки диска (диск в кваде до r≈0.455)
  const haloGeo = new THREE.PlaneGeometry(2, 2);
  const haloMaterial = new THREE.ShaderMaterial({
    vertexShader: HALO_VERTEX,
    fragmentShader: HALO_FRAGMENT,
    uniforms: {},
    transparent: true,
    depthWrite: false,
    depthTest: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
  });
  const halo = new THREE.InstancedMesh(haloGeo, haloMaterial, cap);
  halo.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  halo.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(cap * 3).fill(1), 3);
  halo.count = 0;
  halo.frustumCulled = false;
  halo.renderOrder = 5;

  return { suns, halo, seed };
}
