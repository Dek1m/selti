// Settings API (Ф3 «Конфигурация») — контракт Соны + реестр
// docs/SETTINGS_REGISTRY.md. Чистые помощники (группировка, валидация,
// выбор виджета) покрыты тестами.
//
// Пока бэкенд недоступен локально, вызовы идут в settingsMock (тот же
// контракт, in-memory состояние). Переключение на живой API:
// VITE_SELTI_SETTINGS_MOCK=0 — флаг читается лениво, чтобы тесты
// могли ставить его через vi.stubEnv.

import { apiDelete, apiGet, apiPost, apiPut } from "./client";
import type {
  ApplyProfileResult,
  SettingMeta,
  SettingsPayload,
  SettingsProfile,
  ProfilesPayload,
} from "./types";
import * as mock from "./settingsMock";

function useMock(): boolean {
  return import.meta.env.VITE_SELTI_SETTINGS_MOCK !== "0";
}

/* ── API (контракт) ── */

export function getSettings(): Promise<SettingsPayload> {
  return useMock() ? mock.getSettings() : apiGet<SettingsPayload>("/api/settings");
}

export function updateSetting(key: string, value: unknown, confirm = false): Promise<SettingMeta> {
  if (useMock()) return mock.updateSetting(key, value, confirm);
  return apiPut<SettingMeta>(`/api/settings/${encodeURIComponent(key)}`, { value, confirm });
}

/** Сброс одного ключа к дефолту — тоже принимает {confirm} (dangerous). */
export function resetSetting(key: string, confirm = false): Promise<SettingMeta> {
  if (useMock()) return mock.resetSetting(key, confirm);
  return apiPost<SettingMeta>(`/api/settings/${encodeURIComponent(key)}/reset`, { confirm });
}

/** Сброс всех изменённых ключей к заводским → 200 {reset: [keys]}. */
export function resetAllSettings(): Promise<{ reset: string[] }> {
  if (useMock()) return mock.resetAllSettings();
  return apiPost<{ reset: string[] }>("/api/settings/reset-all");
}

export function getProfiles(): Promise<ProfilesPayload> {
  return useMock() ? mock.getProfiles() : apiGet<ProfilesPayload>("/api/settings/profiles");
}

export function createProfile(name: string, description?: string): Promise<SettingsProfile> {
  if (useMock()) return mock.createProfile(name, description);
  return apiPost<SettingsProfile>("/api/settings/profiles", { name, description });
}

/** Перезаписать снапшот профиля текущими effective-значениями. */
export function updateProfile(id: string): Promise<SettingsProfile> {
  if (useMock()) return mock.updateProfile(id);
  return apiPut<SettingsProfile>(`/api/settings/profiles/${encodeURIComponent(id)}`);
}

/** DELETE → 200 {deleted: id}; builtin («Заводские настройки») → 409. */
export function deleteProfile(id: string): Promise<{ deleted: string }> {
  if (useMock()) return mock.deleteProfile(id);
  return apiDelete<{ deleted: string }>(`/api/settings/profiles/${encodeURIComponent(id)}`);
}

/** Применение профиля: env-ключи пропускаются с отчётом. */
export function applyProfile(id: string, confirm = false): Promise<ApplyProfileResult> {
  if (useMock()) return mock.applyProfile(id, confirm);
  return apiPost<ApplyProfileResult>(`/api/settings/profiles/${encodeURIComponent(id)}/apply`, { confirm });
}

/* ── Группы: заголовки приходят из API (сидинг 027), иконки — фронт ── */

/** Иконки bootstrap по group_key миграции 027; неизвестный ключ → bi-gear. */
export const GROUP_ICONS: Record<string, string> = {
  search: "bi-search",
  dedup: "bi-copy",
  lifecycle: "bi-recycle",
  cluster: "bi-diagram-3",
  linker: "bi-link-45deg",
  edge: "bi-bezier2",
  cloud: "bi-cloud-fill",
  map: "bi-globe",
  celery: "bi-cpu",
  schedule: "bi-clock-history",
  api_caps: "bi-speedometer2",
};

export interface SettingsGroup {
  group: string;
  title: string;
  icon: string;
  items: SettingMeta[];
}

/** Сгруппировать каталог по groups из payload (порядок сидинга);
 * ключи без группы-заголовка фолбэкасят в конец с ключом в качестве заголовка. */
export function groupSettings(payload: SettingsPayload): SettingsGroup[] {
  const byGroup = new Map<string, SettingMeta[]>();
  for (const s of payload.settings) {
    const list = byGroup.get(s.group) ?? [];
    list.push(s);
    byGroup.set(s.group, list);
  }
  const titles = new Map(payload.groups.map((g) => [g.key, g.title_ru]));
  const ordered: string[] = [];
  for (const g of payload.groups) if (byGroup.has(g.key)) ordered.push(g.key);
  for (const key of byGroup.keys()) if (!ordered.includes(key)) ordered.push(key);
  return ordered.map((group) => ({
    group,
    title: titles.get(group) ?? group,
    icon: GROUP_ICONS[group] ?? "bi-gear",
    items: byGroup.get(group)!,
  }));
}

/* ── Виджеты: сервер присылает желаемый widget, фронт мапит ── */

export type WidgetKind = "switch" | "slider" | "number" | "radio" | "select" | "checkboxes" | "dict" | "text" | "json_text";

/** Радио-точечки — UI-политика для маленьких enum (≤3 вариантов). */
const RADIO_MAX = 3;

export function widgetKind(meta: SettingMeta): WidgetKind {
  switch (meta.widget) {
    case "switch":
      return "switch";
    case "slider_number":
      // слайдер обязан знать границы; без них честный фолбэк
      return meta.min_value !== undefined && meta.max_value !== undefined ? "slider" : "number";
    case "number":
      return "number";
    case "combobox":
      if (meta.enum_values && meta.enum_values.length > 0 && meta.enum_values.length <= RADIO_MAX) return "radio";
      return "select";
    case "checkboxes":
      return "checkboxes";
    case "kv_table":
      return "dict";
    case "text":
      return meta.value_type === "json" ? "json_text" : "text";
    default:
      return fallbackKind(meta);
  }
}

/** Фолбэк по типу значения — для старых ответов без widget. */
function fallbackKind(meta: SettingMeta): WidgetKind {
  if (meta.value_type === "bool") return "switch";
  if (meta.value_type === "json") return Array.isArray(meta.value) ? "checkboxes" : "dict";
  if (meta.enum_values && meta.enum_values.length > 0) {
    return meta.enum_values.length <= RADIO_MAX ? "radio" : "select";
  }
  if (meta.value_type === "int" || meta.value_type === "float") {
    return meta.min_value !== undefined && meta.max_value !== undefined ? "slider" : "number";
  }
  return "text";
}

/** Шаг слайдера: у мелких float-диапазонов — тысячные. */
export function sliderStep(meta: SettingMeta): number {
  if (meta.value_type === "int") return 1;
  const span = (meta.max_value ?? 1) - (meta.min_value ?? 0);
  return span <= 0.1 ? 0.001 : 0.01;
}

/* ── Парсинг ввода и валидация (сообщения — на русском) ── */

export type ParseResult = { ok: true; value: unknown } | { ok: false; error: string };

/** Строка из инпута → типизированное значение по value_type. */
export function parseSettingValue(meta: SettingMeta, raw: string): ParseResult {
  const trimmed = raw.trim();
  if (meta.value_type === "int") {
    if (!/^-?\d+$/.test(trimmed)) return { ok: false, error: "Ожидается целое число" };
    return { ok: true, value: Number(trimmed) };
  }
  if (meta.value_type === "float") {
    const n = Number(trimmed);
    if (trimmed === "" || Number.isNaN(n)) return { ok: false, error: "Ожидается число" };
    return { ok: true, value: n };
  }
  if (meta.value_type === "json") {
    try {
      return { ok: true, value: JSON.parse(trimmed) as unknown };
    } catch {
      return { ok: false, error: "Невалидный JSON" };
    }
  }
  return { ok: true, value: raw };
}

/** Клиентская проверка типизированного значения: тип/границы/enum/ключи.
 * null — значение валидно (схемы json досматривает сервер). */
export function validateValue(meta: SettingMeta, value: unknown): string | null {
  if (value === undefined || value === null || (typeof value === "string" && value.trim() === "")) {
    return "Значение не задано";
  }
  if (meta.value_type === "int") {
    if (!Number.isInteger(value)) return "Ожидается целое число";
  }
  if (meta.value_type === "float" || meta.value_type === "int") {
    const n = value as number;
    if (meta.min_value !== undefined && n < meta.min_value) return `Минимум — ${meta.min_value}`;
    if (meta.max_value !== undefined && n > meta.max_value) return `Максимум — ${meta.max_value}`;
  }
  if (meta.enum_values && meta.enum_values.length > 0) {
    const str = String(value);
    if (!meta.enum_values.includes(str)) return `Допустимо: ${meta.enum_values.join(", ")}`;
  }
  // kv_table (пороги по namespace): имена ключей не могут быть пустыми
  if (meta.value_type === "json" && typeof value === "object" && value !== null && !Array.isArray(value)) {
    const keys = Object.keys(value as Record<string, unknown>);
    if (keys.some((k) => k.trim() === "")) return "Имена namespace не должны быть пустыми";
  }
  return null;
}

/* ── Dirty-детект и отображение ── */

/** Глубокое сравнение (json-массивы/словари) с числовой терпимостью. */
export function valuesEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) < 1e-9;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => valuesEqual(v, b[i]));
  }
  if (typeof a === "object" && a !== null && typeof b === "object" && b !== null) {
    const ka = Object.keys(a as object).sort();
    const kb = Object.keys(b as object).sort();
    if (ka.length !== kb.length || !ka.every((k, i) => k === kb[i])) return false;
    return ka.every((k) => valuesEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
  }
  return false;
}

/** Компактное строкальное представление значения (подсказки, confirm). */
export function formatValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "вкл" : "выкл";
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
