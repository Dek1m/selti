// Тесты оркестратора импульсов v2 (фидбек Мастера 22.09): единицы
// одновременных импульсов, фазы вразнобой, часть рёбер без импульса,
// всполох у звезды-истока, смерть импульса при перезаписи слота.

import { describe, expect, it } from "vitest";
import {
  PULSE_COOLDOWN_MIN_MS,
  PULSE_DURATION_MAX_S,
  PULSE_DURATION_MIN_S,
  PULSE_SPAWN_CHANCE,
  PulseOrchestrator,
  mulberry32,
  type PulseEdgeSource,
} from "./pulses";
import { PULSE_SLOTS } from "./shaders";

/** Детерминированный срез буфера: 4 ребра, ids 10..13, цепочка узлов. */
function source(ids = [10, 11, 12, 13]): PulseEdgeSource {
  return {
    ids: Float32Array.from(ids),
    nodes: Int32Array.from(ids.flatMap((_id, i) => [i * 2, i * 2 + 1])),
    count: ids.length,
  };
}

describe("PulseOrchestrator: лимит одновременности", () => {
  it("активных импульсов никогда больше PULSE_SLOTS (единицы на экране)", () => {
    const orch = new PulseOrchestrator(mulberry32(42));
    const src = source();
    for (let t = 0; t < 60; t++) {
      orch.tick(t * 5, src); // шаг 5с: cooldown точно истёк
      expect(orch.active.length).toBeLessThanOrEqual(PULSE_SLOTS);
    }
  });
});

describe("PulseOrchestrator: рандом и фазировка", () => {
  it("не каждый тик спавнит (часть рёбер вообще без импульса)", () => {
    const orch = new PulseOrchestrator(mulberry32(7));
    const src = source();
    let spawnTicks = 0;
    for (let t = 0; t < 200; t++) {
      orch.tick(t * 5, src);
      // тик со спавном: появился импульс с моментом старта == this тик
      if (orch.active.some((p) => p.start === t * 5)) spawnTicks++;
    }
    // шанс 0.8 за тик: при 200 тиках спавнов заметно меньше 200,
    // но существенно больше нуля — «у какой-то не было вовсе»
    expect(PULSE_SPAWN_CHANCE).toBeLessThan(1);
    expect(spawnTicks).toBeGreaterThan(50);
    expect(spawnTicks).toBeLessThan(200);
  });

  it("случайная пауза между спавнами ≥ PULSE_COOLDOWN_MIN_MS — фазы вразнобой", () => {
    const orch = new PulseOrchestrator(mulberry32(123));
    const src = source();
    let lastSpawnMs = -Infinity;
    for (let t = 0; t < 40; t++) {
      const now = t * 5; // сек
      orch.tick(now, src);
      for (const p of orch.active) {
        if (p.start === now) {
          expect(now * 1000 - lastSpawnMs).toBeGreaterThanOrEqual(PULSE_COOLDOWN_MIN_MS);
          lastSpawnMs = now * 1000;
        }
      }
    }
  });

  it("длительность пробега случайна и в калиброванном диапазоне", () => {
    const orch = new PulseOrchestrator(mulberry32(99));
    const src = source();
    const durations = new Set<number>();
    for (let t = 0; t < 100; t++) {
      orch.tick(t * 5, src);
      for (const p of orch.active) durations.add(Math.round(p.duration * 1000));
    }
    expect(durations.size).toBeGreaterThan(3); // не фиксированная длительность
    for (const d of durations) {
      expect(d).toBeGreaterThanOrEqual(PULSE_DURATION_MIN_S * 1000);
      expect(d).toBeLessThanOrEqual(PULSE_DURATION_MAX_S * 1000);
    }
  });
});

describe("PulseOrchestrator: направление и всполох истока", () => {
  it("один импульс — одна линия, одна сторона; всполох строго у истока", () => {
    // 0.1 проходит шанс спавна (< 0.8) и даёт toB = 1 (A→B: 0.1 < 0.5)
    const orchB = new PulseOrchestrator(() => 0.1);
    orchB.tick(0, source());
    expect(orchB.active.length).toBe(1);
    const toB = orchB.active[0];
    expect(toB.toB).toBe(1);
    expect(toB.starIndex).toBe(source().nodes[toB.slot * 2]); // узел A

    // 0.7 спавнит и даёт toB = 0 (B→A: 0.7 < 0.5 — false)
    const orchA = new PulseOrchestrator(() => 0.7);
    orchA.tick(0, source());
    expect(orchA.active.length).toBe(1);
    const toApulse = orchA.active[0];
    expect(toApulse.toB).toBe(0);
    expect(toApulse.starIndex).toBe(source().nodes[toApulse.slot * 2 + 1]); // узел B
  });
});

describe("PulseOrchestrator: жизненный цикл", () => {
  it("импульс умирает по истечении длительности", () => {
    const orch = new PulseOrchestrator(mulberry32(1));
    const src = source();
    orch.tick(0, src);
    expect(orch.active.length).toBe(1);
    const { start, duration } = orch.active[0];
    orch.tick(start + duration - 0.01, src);
    expect(orch.active.length).toBe(1); // ещё бежит
    orch.tick(start + duration + 0.01, src);
    expect(orch.active.length).toBe(0); // пробежал
  });

  it("импульс умирает, если куллинг перезаписал его слот другим ребром", () => {
    const orch = new PulseOrchestrator(mulberry32(5));
    const src = source();
    orch.tick(0, src);
    expect(orch.active.length).toBe(1);
    const { slot, edgeId } = orch.active[0];
    expect(src.ids[slot]).toBe(edgeId);
    src.ids[slot] = 999; // слот перезаписан другим ребром
    orch.tick(0.5, src);
    expect(orch.active.length).toBe(0);
  });

  it("пустой срез и отсутствие сцены не роняют тик", () => {
    const orch = new PulseOrchestrator(mulberry32(3));
    orch.tick(0, null);
    orch.tick(1, { ids: new Float32Array(0), nodes: new Int32Array(0), count: 0 });
    expect(orch.active.length).toBe(0);
  });
});

describe("PulseOrchestrator: uniform-данные", () => {
  it("пустой оркестратор: edgeId = -1 (не совпадает ни с чем), всполохов нет", () => {
    const orch = new PulseOrchestrator(mulberry32(11));
    for (const [edgeId, start, duration, toB] of orch.edgeUniformData()) {
      expect(edgeId).toBe(-1);
      expect(start).toBe(0);
      expect(duration).toBe(1);
      expect(toB).toBe(1);
    }
    for (const [starIndex, start] of orch.flashUniformData()) {
      expect(starIndex).toBe(-1);
      expect(start).toBe(-1000);
    }
  });

  it("после спавна юниформы соответствуют активному импульсу 1:1", () => {
    const orch = new PulseOrchestrator(mulberry32(21));
    orch.tick(0, source());
    expect(orch.active.length).toBe(1);
    const p = orch.active[0];
    const [edgeId, start, duration, toB] = orch.edgeUniformData()[0];
    expect(edgeId).toBe(p.edgeId);
    expect(start).toBe(p.start);
    expect(duration).toBe(p.duration);
    expect(toB).toBe(p.toB);
    const [starIndex, flashStart] = orch.flashUniformData()[0];
    expect(starIndex).toBe(p.starIndex);
    expect(flashStart).toBe(p.start); // тот же spawn — синхронный всполох
  });
});
