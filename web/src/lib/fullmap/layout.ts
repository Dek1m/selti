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
