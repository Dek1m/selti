// Оркестратор синаптических импульсов v2 (фидбек Мастера 22.09):
// «импульсы бьют только в одну сторону, по одной линии, рандомно —
// где-то начались, у какой-то на середине, а у какой-то вообще не было;
// в момент начала — всполох у звезды-истока, отдаёт энергию».
//
// Дизайн: лёгкий таймер (тик 1.5-2.5с, НЕ каждый кадр) выбирает случайное
// ВИДИМОЕ ребро (из живого JS-среза буфера) с вероятностью < 1 и пишет
// uniform-массивы материалов рёбер и звёзд. Per-frame CPU-работы нет:
// GLSL анимирует пробег и затухание по uTime сам. prefers-reduced-motion:
// оркестратор не создаётся вовсе (uSpike = 0, всполохов нет).

import { FLASH_SLOTS, PULSE_SLOTS } from "./shaders";

export const PULSE_DURATION_MIN_S = 1.0;
export const PULSE_DURATION_MAX_S = 1.8;
/** вероятность спавна за тик — часть тиков сознательно пустая */
export const PULSE_SPAWN_CHANCE = 1.0; // было 0.8 — частота втрое (Мастер 23.09)
export const PULSE_COOLDOWN_MIN_MS = 400; // было 1200 — частота втрое (Мастер 23.09)
export const PULSE_COOLDOWN_MAX_MS = 870; // было 2600 — частота втрое (Мастер 23.09)

/** Живой срез буфера рёбер основного слоя (заполняет scene.cullEdges). */
export interface PulseEdgeSource {
  /** глобальный id ребра по слоту */
  ids: Float32Array;
  /** [a, b] глобальные индексы узлов по слоту */
  nodes: Int32Array;
  /** сколько рёбер сейчас реально в буфере */
  count: number;
}

export interface ActivePulse {
  /** слот в буфере слоя */
  slot: number;
  /** глобальный id (валидация: слот не перезаписан куллингом) */
  edgeId: number;
  /** звезда-исток для всполоха («отдаёт энергию») */
  starIndex: number;
  /** момент старта, сек — шкала uTime шейдеров */
  start: number;
  /** длительность пробега, сек */
  duration: number;
  /** 1 = A→B, 0 = B→A: один импульс — одна линия, в одну сторону */
  toB: number;
}

type Rng = () => number;

/**
 * Детерминированный PRNG mulberry32 (тот же, что в starfield):
 * фиксированный seed даёт воспроизводимую последовательность — тесты
 * фазировки не флакают.
 */
export function mulberry32(seed: number): Rng {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class PulseOrchestrator {
  private pulses: ActivePulse[] = [];
  private rng: Rng;
  private cooldownUntil = 0;

  constructor(rng: Rng = Math.random) {
    this.rng = rng;
  }

  get active(): readonly ActivePulse[] {
    return this.pulses;
  }

  /**
   * Тик: чистит протухшие/перезаписанные импульсы и при выпавшем шансе
   * спавнит новый. now — в секундах, та же шкала, что uTime шейдеров.
   */
  tick(now: number, source: PulseEdgeSource | null): void {
    // смерть импульса: пробежал целиком ИЛИ его слот перезаписал куллинг
    this.pulses = this.pulses.filter((p) => {
      const alive = now < p.start + p.duration;
      const slotIntact = source != null && p.slot < source.count && source.ids[p.slot] === p.edgeId;
      return alive && slotIntact;
    });

    if (now * 1000 < this.cooldownUntil) return;
    if (this.pulses.length >= PULSE_SLOTS) return;
    if (!source || source.count <= 0) return;
    if (this.rng() > PULSE_SPAWN_CHANCE) {
      // пустой тик — тоже пауза: у части рёбер импульса не было вовсе
      this.cooldownUntil = now * 1000 + PULSE_COOLDOWN_MIN_MS;
      return;
    }

    const slot = Math.floor(this.rng() * source.count);
    const edgeId = source.ids[slot];
    if (edgeId < 0) return;
    const toB = this.rng() < 0.5 ? 1 : 0;
    const duration =
      PULSE_DURATION_MIN_S + this.rng() * (PULSE_DURATION_MAX_S - PULSE_DURATION_MIN_S);
    // всполох у ИСТОКА: откуда импульс уходит — там звезда и вспыхивает
    const starIndex = source.nodes[slot * 2 + (toB === 1 ? 0 : 1)];
    this.pulses.push({ slot, edgeId, starIndex, start: now, duration, toB });
    // случайная пауза до следующего спавна → фазы всегда вразнобой:
    // одна стартует, другая уже на середине, третья спит
    this.cooldownUntil =
      now * 1000 + PULSE_COOLDOWN_MIN_MS + this.rng() * (PULSE_COOLDOWN_MAX_MS - PULSE_COOLDOWN_MIN_MS);
  }

  /**
   * Данные для uniform vec4 uPulses[K] (edgeId, start, duration, toB);
   * пустой слот — edgeId = -1 (не совпадает ни с каким ребром).
   */
  edgeUniformData(): Array<[number, number, number, number]> {
    const out: Array<[number, number, number, number]> = [];
    for (let i = 0; i < PULSE_SLOTS; i++) {
      const p = this.pulses[i];
      out.push(p ? [p.edgeId, p.start, p.duration, p.toB] : [-1, 0, 1, 1]);
    }
    return out;
  }

  /**
   * Данные для uniform vec2 uFlashes[K] (starIndex, start) — всполохи
   * синхронны импульсам 1:1: тот же спавн, тот же момент старта.
   */
  flashUniformData(): Array<[number, number]> {
    const out: Array<[number, number]> = [];
    for (let i = 0; i < FLASH_SLOTS; i++) {
      const p = this.pulses[i];
      out.push(p ? [p.starIndex, p.start] : [-1, -1000]);
    }
    return out;
  }
}
