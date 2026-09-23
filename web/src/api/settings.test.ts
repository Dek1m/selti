import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import {
  GROUP_ICONS,
  groupSettings,
  parseSettingValue,
  sliderStep,
  validateValue,
  valuesEqual,
  widgetKind,
} from "./settings";
import * as mock from "./settingsMock";
import type { SettingMeta } from "./types";

const meta = (over: Partial<SettingMeta>): SettingMeta => ({
  key: "k",
  value: 1,
  db_value: null,
  value_type: "int",
  group: "search",
  title_ru: "Тестовая",
  description_ru: "Описание",
  default_value: 1,
  is_dangerous: false,
  requires_restart: false,
  is_env_locked: false,
  effective_source: "default",
  differs_from_default: false,
  widget: "number",
  updated_at: null,
  updated_by: null,
  ...over,
});

describe("groupSettings", () => {
  it("uses group titles from the API payload (сидинг 027)", () => {
    const groups = groupSettings({
      settings: [
        meta({ key: "a", group: "api_caps" }),
        meta({ key: "b", group: "search" }),
        meta({ key: "c", group: "linker" }),
      ],
      groups: [
        { key: "search", title_ru: "Поиск и ранжирование" },
        { key: "linker", title_ru: "Линкер" },
        { key: "api_caps", title_ru: "Лимиты API" },
      ],
    });
    // Порядок — как в payload.groups
    expect(groups.map((g) => g.group)).toEqual(["search", "linker", "api_caps"]);
    expect(groups[0].title).toBe("Поиск и ранжирование");
    expect(groups[1].icon).toBe(GROUP_ICONS.linker);
    expect(groups[2].title).toBe("Лимиты API");
  });

  it("falls back for keys missing from groups and unknown icons", () => {
    const groups = groupSettings({
      settings: [meta({ key: "x", group: "mystery" })],
      groups: [{ key: "search", title_ru: "Поиск и ранжирование" }],
    });
    expect(groups).toHaveLength(1);
    expect(groups[0].title).toBe("mystery");
    expect(groups[0].icon).toBe("bi-gear");
  });

  it("knows icons for all eleven migration groups", () => {
    for (const key of ["search", "dedup", "lifecycle", "cluster", "linker", "edge", "cloud", "map", "celery", "schedule", "api_caps"]) {
      expect(GROUP_ICONS[key]).toMatch(/^bi-/);
    }
  });
});

describe("widgetKind (server-driven widget)", () => {
  it("maps registry widgets to controls", () => {
    expect(widgetKind(meta({ value_type: "bool", widget: "switch" }))).toBe("switch");
    expect(widgetKind(meta({ value_type: "float", widget: "slider_number", min_value: 0, max_value: 1 }))).toBe("slider");
    expect(widgetKind(meta({ value_type: "int", widget: "number" }))).toBe("number");
    expect(widgetKind(meta({ value_type: "json", widget: "kv_table", value: {} }))).toBe("dict");
    expect(widgetKind(meta({ value_type: "json", widget: "checkboxes", value: [] }))).toBe("checkboxes");
    expect(widgetKind(meta({ value_type: "str", widget: "text" }))).toBe("text");
    // json + text = JSON-редактор (beat-расписания)
    expect(widgetKind(meta({ value_type: "json", widget: "text", value: {} }))).toBe("json_text");
  });

  it("combobox with a small enum degrades to radio dots", () => {
    expect(widgetKind(meta({ value_type: "str", widget: "combobox", value: "a", enum_values: ["a", "b"] }))).toBe("radio");
    expect(widgetKind(meta({ value_type: "str", widget: "combobox", value: "a", enum_values: ["a", "b", "c", "d"] }))).toBe("select");
  });

  it("slider_number without bounds honestly falls back to number", () => {
    expect(widgetKind(meta({ value_type: "float", widget: "slider_number" }))).toBe("number");
  });

  it("falls back by value_type when the widget is unknown", () => {
    expect(widgetKind(meta({ value_type: "bool", widget: "wat" as never }))).toBe("switch");
  });
});

describe("sliderStep", () => {
  it("uses thousandths for narrow float ranges, ones for ints", () => {
    expect(sliderStep(meta({ value_type: "float", min_value: 0, max_value: 0.1 }))).toBe(0.001);
    expect(sliderStep(meta({ value_type: "float", min_value: 0.9, max_value: 1 }))).toBe(0.001);
    expect(sliderStep(meta({ value_type: "float", min_value: 0, max_value: 1 }))).toBe(0.01);
    expect(sliderStep(meta({ value_type: "int", min_value: 1, max_value: 500 }))).toBe(1);
  });
});

describe("parseSettingValue", () => {
  it("parses integers strictly", () => {
    expect(parseSettingValue(meta({ value_type: "int" }), "42")).toEqual({ ok: true, value: 42 });
    expect(parseSettingValue(meta({ value_type: "int" }), "4.5").ok).toBe(false);
  });

  it("parses json (schedule snapshots) with a readable error", () => {
    expect(parseSettingValue(meta({ value_type: "json" }), '{"type":"interval","seconds":30}')).toEqual({
      ok: true,
      value: { type: "interval", seconds: 30 },
    });
    expect(parseSettingValue(meta({ value_type: "json" }), "{oops")).toEqual({ ok: false, error: "Невалидный JSON" });
  });

  it("passes strings through verbatim", () => {
    expect(parseSettingValue(meta({ value_type: "str" }), "  hi  ")).toEqual({ ok: true, value: "  hi  " });
  });
});

describe("validateValue", () => {
  it("enforces min/max", () => {
    expect(validateValue(meta({ min_value: 10, max_value: 20 }), 5)).toBe("Минимум — 10");
    expect(validateValue(meta({ min_value: 10, max_value: 20 }), 25)).toBe("Максимум — 20");
    expect(validateValue(meta({ min_value: 10, max_value: 20 }), 15)).toBeNull();
  });

  it("enforces enum membership", () => {
    expect(validateValue(meta({ value_type: "str", enum_values: ["a", "b"] }), "c")).toContain("Допустимо");
    expect(validateValue(meta({ value_type: "str", enum_values: ["a", "b"] }), "a")).toBeNull();
  });

  it("rejects empty and non-integer values", () => {
    expect(validateValue(meta({}), "")).toBe("Значение не задано");
    expect(validateValue(meta({ value_type: "int" }), 1.5)).toBe("Ожидается целое число");
  });

  it("rejects empty dict keys (kv_table namespaces)", () => {
    expect(validateValue(meta({ value_type: "json", value: { a: 1 } }), { "": 1 })).toBe(
      "Имена namespace не должны быть пустыми",
    );
  });
});

describe("valuesEqual (dirty detection)", () => {
  it("compares numbers, arrays and objects deeply", () => {
    expect(valuesEqual(0.5, 0.5)).toBe(true);
    expect(valuesEqual(["a", "b"], ["a", "b"])).toBe(true);
    expect(valuesEqual({ a: 1, b: 2 }, { b: 2, a: 1 })).toBe(true);
    expect(valuesEqual({ a: 1 }, { a: 2 })).toBe(false);
  });
});

/* ── Мок API: каталог из SETTINGS_REGISTRY.md, поведение по контракту ── */

describe("settingsMock (contract)", () => {
  beforeEach(() => mock.mockResetState());

  it("returns the full registry catalog with API group titles", async () => {
    const { settings, groups } = await mock.getSettings();
    expect(settings).toHaveLength(97);
    expect(groups.map((g) => g.key)).toEqual([
      "search", "dedup", "lifecycle", "cluster", "linker",
      "edge", "cloud", "map", "celery", "schedule", "api_caps",
    ]);
    expect(new Set(settings.map((s) => s.group))).toEqual(new Set(groups.map((g) => g.key)));
  });

  it("marks env/compose overrides as locked and refuses to update them (409 + keys)", async () => {
    const { settings } = await mock.getSettings();
    const locked = settings.filter((s) => s.is_env_locked);
    expect(locked.length).toBeGreaterThanOrEqual(2);
    const err = await mock.updateSetting(locked[0]!.key, 1).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).keys).toEqual([locked[0]!.key]);
  });

  it("carries db_value and update stamps for db-overridden keys", async () => {
    const { settings } = await mock.getSettings();
    const stale = settings.find((s) => s.key === "stale_threshold")!;
    expect(stale.db_value).toBe(0.35);
    expect(stale.differs_from_default).toBe(true);
    expect(stale.effective_source).toBe("db");
    expect(stale.updated_by).toBeTruthy();
  });

  it("rejects dangerous updates without confirm (409 + keys) and accepts with it", async () => {
    const err = await mock.updateSetting("gc_purge_enabled", true).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).keys).toEqual(["gc_purge_enabled"]);
    const updated = await mock.updateSetting("gc_purge_enabled", true, true);
    expect(updated.value).toBe(true);
    expect(updated.db_value).toBe(true);
    expect(updated.differs_from_default).toBe(true);
  });

  it("validates the value server-side (400 + errors)", async () => {
    const err = await mock.updateSetting("hybrid_prefetch", 5).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(400);
    expect((err as ApiError).errors).toHaveProperty("hybrid_prefetch");
  });

  it("linker invariant: verdict == dedup threshold passes, above it is 400", async () => {
    // Равенство verdict (0.85) и dedup-порога dialogue_insights (0.85) —
    // пустая L2-зона, контракт считает его валидным
    await expect(mock.updateSetting("linker_verdict_threshold", 0.85)).resolves.toBeTruthy();
    // 0.86 всё ещё в границах поля (0.7..0.99), но выше min(dedup_thresholds)
    const err = await mock.updateSetting("linker_verdict_threshold", 0.86).catch((e) => e);
    expect((err as ApiError).status).toBe(400);
    expect((err as ApiError).message).toContain("Инвариант");
  });

  it("validates schedule snapshots (type must be interval|crontab)", async () => {
    const err = await mock.updateSetting("schedule.mark_stale", { type: "lunar", seconds: 1 }, false).catch((e) => e);
    expect((err as ApiError).status).toBe(400);
  });

  it("resets one setting to default; dangerous reset also needs confirm", async () => {
    await mock.updateSetting("hybrid_prefetch", 500, false);
    const reset = await mock.resetSetting("hybrid_prefetch");
    expect(reset.value).toBe(100);
    expect(reset.db_value).toBeNull();
    expect(reset.effective_source).toBe("default");

    const err = await mock.resetSetting("gc_retention_days").catch((e) => e);
    expect((err as ApiError).status).toBe(409);
    const ok = await mock.resetSetting("gc_retention_days", true);
    expect(ok.db_value).toBeNull();
  });

  it("reset-all returns {reset: [keys]} and keeps env-locked settings intact", async () => {
    await mock.updateSetting("hybrid_prefetch", 500, false);
    const res = await mock.resetAllSettings();
    expect(res.reset.length).toBeGreaterThan(90);
    expect(res.reset).not.toContain("dedup_threshold");
    const { settings } = await mock.getSettings();
    expect(settings.find((s) => s.key === "hybrid_prefetch")!.value).toBe(100);
    expect(settings.find((s) => s.key === "dedup_threshold")!.is_env_locked).toBe(true);
  });

  it("profiles: apply of a dangerous profile returns 409 with the keys array", async () => {
    // профиль фиксирует заводское состояние, затем опасный ключ двигают
    const profile = await mock.createProfile("До включения GC");
    await mock.updateSetting("gc_purge_enabled", true, true);

    const err = await mock.applyProfile(profile.id).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).keys).toContain("gc_purge_enabled");

    const res = await mock.applyProfile(profile.id, true);
    expect(res.skipped_env).toEqual(expect.arrayContaining(["dedup_threshold", "traverse_max_nodes"]));
    const { settings } = await mock.getSettings();
    expect(settings.find((s) => s.key === "gc_purge_enabled")!.value).toBe(false);
  });

  it("builtin profile applies defaults, cannot be updated or deleted (409)", async () => {
    await mock.updateSetting("hybrid_prefetch", 500, false);
    const res = await mock.applyProfile("builtin-default");
    expect(res.applied.length).toBeGreaterThan(90);
    const { settings } = await mock.getSettings();
    expect(settings.find((s) => s.key === "hybrid_prefetch")!.value).toBe(100);

    await expect(mock.deleteProfile("builtin-default")).rejects.toMatchObject({ status: 409 });
    await expect(mock.updateProfile("builtin-default")).rejects.toMatchObject({ status: 409 });
  });

  it("delete returns {deleted: id}", async () => {
    const profile = await mock.createProfile("Временный");
    await expect(mock.deleteProfile(profile.id)).resolves.toEqual({ deleted: profile.id });
  });

  it("unknown keys and profiles produce 404", async () => {
    await expect(mock.updateSetting("nope", 1)).rejects.toMatchObject({ status: 404 });
    await expect(mock.deleteProfile("nope")).rejects.toMatchObject({ status: 404 });
  });
});

/* Прод-путь: свежий импорт модуля без mockResetState — оверрайды
 * (env-замки и db-записи) должны быть в каталоге сразу. Регрессия:
 * live клонировался до применения оверрайдов к BUILT. */
describe("settingsMock (fresh import, production path)", () => {
  it("serves env/db overrides on first render", async () => {
    vi.resetModules();
    const fresh = await import("./settingsMock");
    const { settings } = await fresh.getSettings();

    const dedup = settings.find((s) => s.key === "dedup_threshold")!;
    expect(dedup.is_env_locked).toBe(true);
    expect(dedup.value).toBe(0.93);
    expect(dedup.effective_source).toBe("env");

    const traverse = settings.find((s) => s.key === "traverse_max_nodes")!;
    expect(traverse.is_env_locked).toBe(true);
    expect(traverse.value).toBe(800);

    const stale = settings.find((s) => s.key === "stale_threshold")!;
    expect(stale.value).toBe(0.35);
    expect(stale.db_value).toBe(0.35);
    expect(stale.differs_from_default).toBe(true);
    expect(stale.updated_by).toBe("Серёжа");
  });
});
