// Decorative deep-space backdrop for the graph screen: a static 2D canvas
// layer BEHIND the sigma WebGL canvas. Never animates on its own — it is
// redrawn only on resize and on camera-parallax updates, and each pass is
// ~250 cheap dots. This layer is pure atmosphere: graph data never lives here.

/** Deterministic PRNG so the sky is identical on every visit. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Star {
  x: number; // 0..1 relative to canvas width
  y: number;
  r: number;
  alpha: number;
  color: string;
  halo: boolean;
  /** parallax depth factor (far stars barely drift) */
  depth: number;
}

const STAR_COLORS: [string, number][] = [
  ["#BFD4F2", 0.76], // pale blue-white, the dominant sky
  ["#8FD8FF", 0.12], // cyan drift
  ["#FFE3B8", 0.08], // warm far giants
  ["#FFAEBE", 0.04], // faint rose, rare
];

function pickColor(rnd: () => number): string {
  let roll = rnd();
  for (const [color, share] of STAR_COLORS) {
    if (roll < share) return color;
    roll -= share;
  }
  return STAR_COLORS[0][0];
}

/** Soft radial halo sprite — drawn once, stamped per near star (cheap). */
function makeHaloSprite(): HTMLCanvasElement | null {
  if (typeof document === "undefined") return null;
  const size = 64;
  const sprite = document.createElement("canvas");
  sprite.width = size;
  sprite.height = size;
  const ctx = sprite.getContext("2d");
  if (!ctx) return null;
  const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, "rgba(191, 212, 242, 0.55)");
  grad.addColorStop(0.4, "rgba(143, 216, 255, 0.18)");
  grad.addColorStop(1, "rgba(143, 216, 255, 0)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  return sprite;
}

/** Build the fixed sky: three depth bands, denser and dimmer far away. */
function buildSky(): Star[] {
  const rnd = mulberry32(20260920);
  const stars: Star[] = [];

  const band = (
    count: number,
    radius: [number, number],
    alpha: [number, number],
    depth: number,
    haloChance = 0,
  ) => {
    for (let i = 0; i < count; i++) {
      stars.push({
        x: rnd(),
        y: rnd(),
        r: radius[0] + rnd() * (radius[1] - radius[0]),
        alpha: alpha[0] + rnd() * (alpha[1] - alpha[0]),
        color: pickColor(rnd),
        halo: rnd() < haloChance,
        depth,
      });
    }
  };

  band(150, [0.35, 0.8], [0.10, 0.28], 0.25);
  band(64, [0.6, 1.2], [0.18, 0.4], 0.55);
  band(26, [0.9, 1.7], [0.28, 0.55], 1.0, 0.3);
  return stars;
}

export interface StarfieldOptions {
  /** camera offset in normalized units (−0.5..0.5) for the drift effect */
  parallax?: { x: number; y: number };
}

/** Paint the sky onto a canvas sized to its CSS box (device-pixel aware). */
export function drawStarfield(canvas: HTMLCanvasElement, options: StarfieldOptions = {}): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (width === 0 || height === 0) return;
  const pixelWidth = Math.round(width * dpr);
  const pixelHeight = Math.round(height * dpr);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const px = (options.parallax?.x ?? 0) * -26;
  const py = (options.parallax?.y ?? 0) * -26;
  const halo = makeHaloSprite();

  for (const star of buildSky()) {
    const x = star.x * width + px * star.depth;
    const y = star.y * height + py * star.depth;
    if (x < -8 || x > width + 8 || y < -8 || y > height + 8) continue;
    if (star.halo && halo) {
      const haloSize = star.r * 14;
      ctx.globalAlpha = star.alpha;
      ctx.drawImage(halo, x - haloSize / 2, y - haloSize / 2, haloSize, haloSize);
    }
    ctx.globalAlpha = star.alpha;
    ctx.fillStyle = star.color;
    ctx.beginPath();
    ctx.arc(x, y, star.r, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
}
