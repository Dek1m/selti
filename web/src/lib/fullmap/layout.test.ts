import { describe, expect, it } from "vitest";
import {
  CONSTELLATION_LAYOUT,
  ellipseLayout,
  hashUuid,
  layoutBBox,
  serverLayout,
} from "./layout";

describe("serverLayout — смешанная раскладка созвездия (map_layout + fallback)", () => {
  it("keeps server coordinates untouched — same spot as the full map", () => {
    const uuids = ["aaa", "bbb", "ccc"];
    // серверные позиции из wire-кортежа (scale галактики ±1000, не 220)
    const server: number[] = [
      850.5, -120.25, 900.0,
      -999.0, 0.0, 12.5,
      0.0, 640.75, -430.0,
    ];
    const output = new Float32Array(server);
    serverLayout(uuids, output, CONSTELLATION_LAYOUT);
    expect([...output]).toEqual(server);
  });

  it("falls back to the deterministic ellipse disc for rowless nodes", () => {
    const uuids = ["with-row", "rowless-a", "rowless-b"];
    const output = new Float32Array([
      100.0, -50.0, 700.0, // серверная
      NaN, NaN, NaN, // нет строки в map_layout
      NaN, NaN, NaN,
    ]);
    serverLayout(uuids, output, CONSTELLATION_LAYOUT);

    // серверная — на месте
    expect(output[0]).toBe(100.0);
    expect(output[1]).toBe(-50.0);
    expect(output[2]).toBe(700.0);

    // fallback: конечные координаты внутри диска ±radius / ±thickness
    for (const i of [1, 2]) {
      expect(Number.isFinite(output[i * 3])).toBe(true);
      expect(Number.isFinite(output[i * 3 + 1])).toBe(true);
      expect(Number.isFinite(output[i * 3 + 2])).toBe(true);
      expect(Math.abs(output[i * 3])).toBeLessThanOrEqual(CONSTELLATION_LAYOUT.radius);
      expect(Math.abs(output[i * 3 + 2])).toBeLessThanOrEqual(CONSTELLATION_LAYOUT.radius);
      expect(Math.abs(output[i * 3 + 1])).toBeLessThanOrEqual(CONSTELLATION_LAYOUT.thickness);
    }

    // fallback детерминирован: тот же uuid — та же точка
    const again = new Float32Array([0, 0, 0, NaN, NaN, NaN, NaN, NaN, NaN]);
    serverLayout(uuids, again, CONSTELLATION_LAYOUT);
    expect([...again.slice(3)]).toEqual([...output.slice(3)]);
  });

  it("fallback matches plain ellipseLayout for an all-rowless snapshot", () => {
    const uuids = Array.from({ length: 40 }, (_, i) => `uuid-${i}`);
    const mixed = new Float32Array(uuids.length * 3);
    mixed.fill(NaN);
    mixed[0] = 1;
    mixed[1] = 2;
    mixed[2] = 3; // один серверский узел
    serverLayout(uuids, mixed, CONSTELLATION_LAYOUT);

    const plain = new Float32Array(uuids.length * 3);
    ellipseLayout(uuids, plain, CONSTELLATION_LAYOUT);

    for (let i = 1; i < uuids.length; i++) {
      expect(mixed[i * 3]).toBe(plain[i * 3]);
      expect(mixed[i * 3 + 1]).toBe(plain[i * 3 + 1]);
      expect(mixed[i * 3 + 2]).toBe(plain[i * 3 + 2]);
    }
  });

  it("partial-NaN nodes (wire damage) are treated as rowless", () => {
    const uuids = ["half-broken"];
    const output = new Float32Array([5.0, NaN, 5.0]);
    serverLayout(uuids, output, CONSTELLATION_LAYOUT);
    // все три координаты конечны только у целой тройки
    expect(Number.isFinite(output[0])).toBe(true);
    expect(Number.isFinite(output[1])).toBe(true);
    expect(Number.isFinite(output[2])).toBe(true);
    expect(Math.hypot(output[0], output[2])).toBeLessThanOrEqual(
      CONSTELLATION_LAYOUT.radius * Math.sqrt(2),
    );
  });
});

describe("layoutBBox — габариты фактического облака", () => {
  it("spans server and fallback positions together", () => {
    const output = new Float32Array([
      -800, 10, 400,
      900, -20, -350,
      15, 40, 5,
    ]);
    const bbox = layoutBBox(output, 3);
    expect(bbox).not.toBeNull();
    expect(bbox!.min).toEqual([-800, -20, -350]);
    expect(bbox!.max).toEqual([900, 40, 400]);
  });

  it("returns null for an empty set", () => {
    expect(layoutBBox(new Float32Array(0), 0)).toBeNull();
  });
});

describe("hashUuid — стабильность смешанной раскладки", () => {
  it("same uuid hash feeds the fallback deterministically", () => {
    expect(hashUuid("stable-uuid")).toBe(hashUuid("stable-uuid"));
    expect(hashUuid("stable-uuid")).not.toBe(hashUuid("stable-uuid-2"));
  });
});
