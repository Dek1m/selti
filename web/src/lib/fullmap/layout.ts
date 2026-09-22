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


// ── Смешанная раскладка full-карты (эталон EVE): объёмные «руки» —
// 60% узлов гауссианой вокруг центров своих кластеров (центры сами
// равномерно-случайно по объёму, sigma своя на кластер), 40% равномерно
// по всему объёму. Детерминировано хэшем uuid; вертикаль площе горизонтали.

export interface VolumeBounds {
  /** полуширина по x/z */
  span: number;
  /** полувысота по y (вертикаль площе) */
  height: number;
}

export const FULL_VOLUME: VolumeBounds = { span: 700, height: 420 };

/**
 * Объёмная раскладка полного графа: 60% вокруг центроидов кластеров
 * (гауссиана, sigma своя на кластер), 40% равномерно по объёму.
 * Детерминировано хэшем uuid.
 */
export function mixedLayout(
  uuids: string[],
  clusterSlotOf: (i: number) => number,
  clusterCount: number,
  output: Float32Array,
  bounds: VolumeBounds,
): Float32Array {
  // центры кластеров: равномерно-случайно по объёму, sigma на кластер
  const centers = new Float32Array(clusterCount * 3);
  const sigmas = new Float32Array(clusterCount);
  for (let c = 0; c < clusterCount; c++) {
    const h = hashUuid(`cluster-${c}`);
    centers[c * 3] = ((h & 0xffff) / 0x10000 * 2 - 1) * bounds.span;
    centers[c * 3 + 1] = (((h >>> 8) ^ (h >>> 16)) & 0xffff) / 0x10000 * 2 * bounds.height - bounds.height;
    centers[c * 3 + 2] = (((h >>> 4) & 0xffff) / 0x10000 * 2 - 1) * bounds.span;
    sigmas[c] = 65 + ((h >>> 12) & 0xff) / 255 * 155; // 65..220 — сильный разброс
  }

  const gauss = (h: number) => {
    // сумма хэш-дробей — устойчивый квази-гаусс (маски 16 бит!)
    const a = (h & 0xffff) / 0x10000;
    const b = (((h >>> 8) ^ (h >>> 16)) & 0xffff) / 0x10000;
    const c = (((h >>> 4) ^ (h >>> 20)) & 0xffff) / 0x10000;
    return a + b + c - 1.5; // [-1.5, 1.5], пик в нуле
  };

  for (let i = 0; i < uuids.length; i++) {
    const h = hashUuid(uuids[i]);
    const slot = clusterSlotOf(i);
    if (slot >= 0 && slot < clusterCount && i % 5 < 3) {
      // 60%: гауссиана вокруг центра своего кластера
      const g1 = gauss(h);
      const g2 = gauss(h ^ 0x9e3779b9);
      const g3 = gauss(h ^ 0x85ebca6b);
      const sigma = sigmas[slot];
      output[i * 3] = centers[slot * 3] + g1 * sigma;
      output[i * 3 + 1] = centers[slot * 3 + 1] + g2 * sigma * 0.6; // вертикаль площе
      output[i * 3 + 2] = centers[slot * 3 + 2] + g3 * sigma;
    } else {
      // 40%: равномерно по всему объёму (фоновое звёздное поле)
      const h1 = (h & 0xffff) / 0x10000;
      const h2 = ((h >>> 16) & 0xffff) / 0x10000;
      const h3v = (((h >>> 4) ^ (h >>> 20)) & 0xffff) / 0x10000;
      output[i * 3] = (h1 * 2 - 1) * bounds.span;
      output[i * 3 + 1] = (h2 * 2 - 1) * bounds.height;
      output[i * 3 + 2] = (h3v * 2 - 1) * bounds.span;
    }
  }
  return output;
}
