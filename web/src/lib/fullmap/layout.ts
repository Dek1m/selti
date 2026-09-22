// Client-side deterministic layout for the full map (разворот Мастера к
// простоте): компактное 3D-облако вместо серверной sphere-раскладки.
// Позиция выводится из хэша uuid — стабильна между сессиями; grid-джиттер
// не даёт точкам слипаться. Один движок — два масштаба: полный граф
// ±450/±180 и созвездие ±220/±90.

/** Габариты облака: горизонтальный радиус и вертикальная полутолщина. */
export interface LayoutBounds {
  radius: number;
  thickness: number;
}

export const FULL_LAYOUT: LayoutBounds = { radius: 450, thickness: 180 };
export const CONSTELLATION_LAYOUT: LayoutBounds = { radius: 220, thickness: 90 };

/** Минимальное расстояние между точками в плане (анти-слипание). */
export const LAYOUT_MIN_DIST = 6;

/** FNV-1a — стабильный хэш строки в 32 бита. */
export function hashUuid(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/**
 * Компактный 3D-объём в плоскости XZ (y — вертикаль): равномерный диск
 * радиуса bounds.radius, вертикальный джиттер ±bounds.thickness,
 * анти-слипание через occupancy-grid (спиральные сдвиги при коллизии).
 * Детерминировано хэшем uuid — та же гранула ложится в ту же точку всегда.
 */
export function ellipseLayout(uuids: string[], output: Float32Array, bounds: LayoutBounds): Float32Array {
  const cell = LAYOUT_MIN_DIST;
  const origin = -bounds.radius - cell;
  const grid = new Map<number, Array<{ x: number; z: number }>>();

  const cellKey = (cx: number, cz: number) => cx * 4096 + cz;
  const tooClose = (x: number, z: number) => {
    const cx = Math.floor((x - origin) / cell);
    const cz = Math.floor((z - origin) / cell);
    for (let gx = cx - 1; gx <= cx + 1; gx++) {
      for (let gz = cz - 1; gz <= cz + 1; gz++) {
        const bucket = grid.get(cellKey(gx, gz));
        if (!bucket) continue;
        for (const p of bucket) {
          const dx = p.x - x;
          const dz = p.z - z;
          if (dx * dx + dz * dz < LAYOUT_MIN_DIST * LAYOUT_MIN_DIST) return true;
        }
      }
    }
    return false;
  };
  const occupy = (x: number, z: number) => {
    const cx = Math.floor((x - origin) / cell);
    const cz = Math.floor((z - origin) / cell);
    const key = cellKey(cx, cz);
    const bucket = grid.get(key);
    if (bucket) bucket.push({ x, z });
    else grid.set(key, [{ x, z }]);
  };

  for (let i = 0; i < uuids.length; i++) {
    const h = hashUuid(uuids[i]);
    const h1 = (h & 0xffff) / 0x10000;
    const h2 = ((h >>> 16) & 0xffff) / 0x10000;
    const h3 = ((h >>> 8) ^ (h >>> 20)) / 0x1000000; // 24 бита → [0, 1]

    // равномерный диск: sqrt-радиус, плотность к краю не спадает
    const radius = bounds.radius * Math.sqrt(h1);
    const angle = h2 * Math.PI * 2;
    let x = Math.cos(angle) * radius;
    let z = Math.sin(angle) * radius;

    // анти-слипание: до 8 спиральных проб сдвига, потом оставляем как есть
    if (tooClose(x, z)) {
      for (let attempt = 1; attempt <= 8; attempt++) {
        const spread = LAYOUT_MIN_DIST * attempt;
        const ja = (h1 * 12.9898 + attempt * 2.399) % (Math.PI * 2);
        const nx = x + Math.cos(ja) * spread;
        const nz = z + Math.sin(ja) * spread;
        if (Math.abs(nx) <= bounds.radius && Math.abs(nz) <= bounds.radius && !tooClose(nx, nz)) {
          x = nx;
          z = nz;
          break;
        }
      }
    }
    occupy(x, z);

    const yJitter = (h3 - 0.5) * 2 * bounds.thickness;
    output[i * 3] = x;
    output[i * 3 + 1] = yJitter;
    output[i * 3 + 2] = z;
  }
  return output;
}


// ── Спиральная раскладка full-карты (разворот Мастера): Archimedean
// spiral по XZ — «галактическая рука», точки вдоль каркаса с джиттером.
// Детерминировано индексом (порядок снапшота стабилен) + хэшем uuid.

export interface SpiralBounds {
  /** внешний радиус спирали */
  radius: number;
  /** вертикальный шум ±thickness */
  thickness: number;
  /** межвиток (расстояние между витками) */
  gap: number;
  /** поперечный джиттер ±spread */
  spread: number;
}

export const FULL_SPIRAL: SpiralBounds = { radius: 560, thickness: 40, gap: 40, spread: 15 };

/**
 * Спираль Архимеда r = r0 + b·θ: точки идут вдоль каркаса равномерно по
 * дуге (шаг = длина спирали / N), поперечный и вертикальный джиттер — из
 * хэша uuid. b = gap / 2π. Детерминировано индексом + uuid.
 */
export function spiralLayout(uuids: string[], output: Float32Array, bounds: SpiralBounds): Float32Array {
  const r0 = 40;
  const b = bounds.gap / (2 * Math.PI);
  const rMax = bounds.radius;
  const totalLength = (rMax * rMax - r0 * r0) / (2 * b);
  const step = totalLength / Math.max(1, uuids.length);

  let theta = 0;
  let arc = 0;
  const gauss = (h: number) => {
    // грубая сумма хэш-дробей — устойчивый квази-гаусс
    const a = (h & 0xffff) / 0x10000;
    const b2 = (((h >>> 8) ^ (h >>> 16)) & 0xffff) / 0x10000; // маска 16 бит!
    return (a + b2 - 1);
  };

  for (let i = 0; i < uuids.length; i++) {
    const h = hashUuid(uuids[i]);
    const r = r0 + b * theta;
    if (arc + step > totalLength) {
      // спираль кончилась — оставшиеся на внешнем кольце с джиттером
      const ringAngle = (h & 0xffff) / 0x10000 * Math.PI * 2;
      output[i * 3] = Math.cos(ringAngle) * rMax * (0.96 + 0.04 * (h2(h)));
      output[i * 3 + 1] = (h3(h) - 0.5) * 2 * bounds.thickness;
      output[i * 3 + 2] = Math.sin(ringAngle) * rMax * (0.96 + 0.04 * (h2(h)));
      continue;
    }
    const sinT = Math.sin(theta);
    const cosT = Math.cos(theta);
    // поперечная нормаль спирали ≈ радиальное направление
    const radial = bounds.spread * gauss(h);
    const along = bounds.spread * 0.4 * gauss(h ^ 0x9e3779b9);
    const x = cosT * (r + radial) - sinT * along;
    const z = sinT * (r + radial) + cosT * along;
    const y = (h3(h) - 0.5) * 2 * bounds.thickness;

    output[i * 3] = x;
    output[i * 3 + 1] = y;
    output[i * 3 + 2] = z;

    arc += step;
    theta += step / Math.max(r, r0);
  }
  return output;
}

function h2(h: number): number {
  return ((h >>> 4) & 0xffff) / 0x10000;
}
function h3(h: number): number {
  return ((h >>> 8) ^ (h >>> 20)) / 0x1000000;
}
