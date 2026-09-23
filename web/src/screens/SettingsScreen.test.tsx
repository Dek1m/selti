// @vitest-environment jsdom
// Компонентные тесты экрана «Конфигурация»: мок-слой отключается
// (VITE_SELTI_SETTINGS_MOCK=0), сеть стабится на фикстуре контракта Соны.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsScreen } from "./SettingsScreen";
import type { SettingMeta, SettingsGroupInfo } from "../api/types";

vi.stubEnv("VITE_SELTI_SETTINGS_MOCK", "0");

/* ── Фикстура: типы виджетов реестра + env-блокировка + dangerous ── */

const F = (over: Partial<SettingMeta>): SettingMeta => ({
  key: "k",
  value: 1,
  db_value: null,
  value_type: "int",
  group: "search",
  title_ru: "Тестовая настройка",
  description_ru: "Что даёт эта настройка",
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

const FIXTURE: SettingMeta[] = [
  F({ key: "search_default_threshold", title_ru: "Порог релевантности поиска", value_type: "float", value: 0.6, default_value: 0.7, min_value: 0, max_value: 1, widget: "slider_number", differs_from_default: true }),
  F({ key: "hybrid_search_enabled", title_ru: "Гибридный поиск", value_type: "bool", value: true, widget: "switch" }),
  F({ key: "hybrid_prefetch", title_ru: "Предвыборка кандидатов", value_type: "int", value: 100, default_value: 100, min_value: 10, max_value: 1000, widget: "number" }),
  F({ key: "search_strategy", title_ru: "Стратегия поиска", value_type: "str", value: "hybrid", enum_values: ["hybrid", "dense", "fts", "activation"], widget: "combobox" }),
  F({ key: "gc_mode", group: "lifecycle", title_ru: "Режим GC", value_type: "str", value: "disabled", enum_values: ["disabled", "soft", "hard"], widget: "combobox", is_dangerous: true }),
  F({ key: "dedup_enabled", group: "dedup", title_ru: "Дедупликация записей", value_type: "bool", value: true, widget: "switch", is_dangerous: true }),
  F({ key: "dedup_scope_namespaces", group: "dedup", title_ru: "Пространства дедупликации", value_type: "json", value: ["user_facts", "code_knowledge"], default_value: ["user_facts", "code_knowledge", "infrastructure"], enum_values: ["user_facts", "project_meta", "code_knowledge", "infrastructure"], widget: "checkboxes" }),
  F({ key: "dedup_threshold", group: "dedup", title_ru: "Порог дедупликации", value_type: "float", value: 0.93, db_value: null, effective_source: "env", is_env_locked: true, widget: "slider_number", min_value: 0.5, max_value: 1 }),
  F({ key: "linker_thresholds", group: "linker", title_ru: "Пороги по неймспейсам", value_type: "json", value: { user_facts: 0.85 }, widget: "kv_table" }),
  F({ key: "schedule.mark_stale", group: "schedule", title_ru: "Пометка устаревших", value_type: "json", value: { type: "crontab", minute: "0", hour: "4", day_of_week: null }, widget: "text", requires_restart: true }),
  F({ key: "gc_purge_enabled", group: "lifecycle", title_ru: "Мастер-кран физического удаления", value_type: "bool", value: false, widget: "switch", is_dangerous: true }),
];

const GROUPS_FIXTURE: SettingsGroupInfo[] = [
  { key: "search", title_ru: "Поиск и ранжирование" },
  { key: "dedup", title_ru: "Дедупликация" },
  { key: "lifecycle", title_ru: "Жизненный цикл и GC" },
  { key: "linker", title_ru: "Линкер" },
  { key: "schedule", title_ru: "Планировщик — расписания" },
];

const PROFILES = [
  { id: "builtin-default", name: "default", description: "Заводские настройки", is_builtin: true, created_at: "2026-09-01T00:00:00Z" },
];

interface Rec { path: string; body: unknown }
const PUTCalls: Rec[] = [];
const POSTCalls: Rec[] = [];

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function fetchMock(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  const method = init?.method ?? "GET";
  const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined;
  if (method === "PUT") PUTCalls.push({ path: url, body });
  if (method === "POST") POSTCalls.push({ path: url, body });

  if (url === "/api/settings" && method === "GET") return jsonResponse({ settings: FIXTURE, groups: GROUPS_FIXTURE });
  if (url === "/api/settings/profiles" && method === "GET") return jsonResponse({ profiles: PROFILES });
  if (url.startsWith("/api/settings/") && method === "PUT") {
    const key = url.split("/").pop()!;
    const m = FIXTURE.find((s) => s.key === key)!;
    return jsonResponse({ ...m, value: body!.value, db_value: body!.value, differs_from_default: true, effective_source: "db", updated_at: "2026-09-23T12:00:00Z", updated_by: "web-ui" });
  }
  if (url.endsWith("/reset")) {
    const key = url.split("/")[3];
    const m = FIXTURE.find((s) => s.key === key)!;
    return jsonResponse({ ...m, value: m.default_value, db_value: null, differs_from_default: false, effective_source: "default" });
  }
  if (url === "/api/settings/reset-all") return jsonResponse({ reset: FIXTURE.map((s) => s.key) });
  if (url === "/api/settings/profiles" && method === "POST") return jsonResponse({ id: "p_1", name: (body as { name: string }).name, is_builtin: false, created_at: "2026-09-23T00:00:00Z" });
  if (url.endsWith("/apply")) {
    if (body?.confirm !== true) {
      // Снапшот профиля всегда содержит dangerous-ключи → confirm
      return jsonResponse({ message: "Профиль затрагивает опасные настройки", keys: ["gc_purge_enabled", "dedup_enabled"] }, 409);
    }
    return jsonResponse({ applied: ["gc_purge_enabled", "hybrid_search_enabled"], skipped_env: ["dedup_threshold"] });
  }
  return new Response("not found", { status: 404 });
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SettingsScreen />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  PUTCalls.length = 0;
  POSTCalls.length = 0;
  vi.stubGlobal("fetch", vi.fn(fetchMock));
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/* ── Тесты ── */

describe("SettingsScreen", () => {
  it("renders groups with russian titles from the API payload", async () => {
    renderScreen();
    expect(await screen.findByText("Поиск и ранжирование")).toBeTruthy();
    expect(screen.getByText("Планировщик — расписания")).toBeTruthy();
    expect(screen.getByText("Жизненный цикл и GC")).toBeTruthy();
  });

  it("shows the tooltip on focus with aria-describedby", async () => {
    renderScreen();
    const trigger = await screen.findByRole("button", { name: "Подсказка: Порог релевантности поиска" });
    fireEvent.focus(trigger);
    const tip = await screen.findByRole("tooltip");
    expect(tip.textContent).toContain("Что даёт эта настройка");
    expect(trigger.getAttribute("aria-describedby")).toBeTruthy();
  });

  it("locks env/compose keys by is_env_locked: disabled widgets + badge, no reset/save", async () => {
    renderScreen();
    await screen.findByText("Порог дедупликации");
    const lockBadge = screen.getByTitle(/Управляется через \.env/);
    expect(lockBadge.textContent).toContain("env");
    expect(screen.queryByRole("button", { name: /Сбросить «Порог дедупликации»/ })).toBeNull();
    await waitFor(() => expect(document.querySelector("input:disabled")).not.toBeNull());
  });

  it("renders registry widgets: slider, radio, select, checkboxes, kv_table, json-text", async () => {
    renderScreen();
    await screen.findByText("Дедупликация записей");
    expect((await screen.findAllByRole("slider")).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("radio").length).toBe(3); // gc_mode — combobox с ≤3 вариантами
    const combos = screen.getAllByRole("combobox") as HTMLSelectElement[];
    const strategy = combos.find((c) => [...c.options].some((o) => o.value === "activation"));
    expect(strategy?.options.length).toBe(4); // search_strategy
    expect(screen.getAllByRole("checkbox").length).toBeGreaterThanOrEqual(4);
    expect(screen.getByLabelText("Ключ (namespace)")).toBeTruthy(); // kv_table
    expect(screen.getByRole("textbox", { name: "Пометка устаревших" }).tagName).toBe("TEXTAREA");
  });

  it("dirty row + save sends PUT with the typed value", async () => {
    renderScreen();
    await screen.findByText("Гибридный поиск");
    const checkboxes = (await screen.findAllByRole("checkbox")) as HTMLInputElement[];
    const sw = checkboxes.find((c) => (c.closest(".setting-row")?.textContent ?? "").includes("Гибридный поиск"));
    expect(sw?.checked).toBe(true);
    fireEvent.click(sw!);
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(PUTCalls.length).toBe(1));
    expect(PUTCalls[0].path).toBe("/api/settings/hybrid_search_enabled");
    expect(PUTCalls[0].body).toEqual({ value: false, confirm: false });
  });

  it("client validation: bad range blocks saving, bad json shows a readable error", async () => {
    renderScreen();
    await screen.findByText("Предвыборка кандидатов");
    const row = screen.getByText("Предвыборка кандидатов").closest(".setting-row")!;
    fireEvent.change(row.querySelector('input[inputmode="numeric"]') as HTMLInputElement, { target: { value: "99999" } });
    expect(screen.getByText("Максимум — 1000")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Сохранить" }) as HTMLButtonElement).disabled).toBe(true);

    const jsonRow = screen.getByText("Пометка устаревших").closest(".setting-row")!;
    fireEvent.change(jsonRow.querySelector("textarea") as HTMLTextAreaElement, { target: { value: "{oops" } });
    expect(screen.getByText("Невалидный JSON")).toBeTruthy();
  });

  it("dangerous toggle opens the confirm modal (gc text) and saves with confirm", async () => {
    renderScreen();
    const checkboxes = (await screen.findAllByRole("checkbox")) as HTMLInputElement[];
    const gc = checkboxes.find((c) => !c.checked && (c.closest(".setting-row")?.textContent ?? "").includes("Мастер-кран"));
    fireEvent.click(gc!);
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByRole("alertdialog")).toBeTruthy();
    expect(screen.getByText("Открыть физическое удаление знаний?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Подтверждаю" }));
    await waitFor(() => expect(PUTCalls.length).toBe(1));
    expect(PUTCalls[0].body).toEqual({ value: true, confirm: true });
  });

  it("reset-all asks for confirmation and posts reset-all", async () => {
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: /Сбросить всё к заводским/ }));
    expect(await screen.findByRole("alertdialog")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Сбросить всё" }));
    await waitFor(() => expect(POSTCalls.some((c) => c.path === "/api/settings/reset-all")).toBe(true));
  });

  it("profile apply: 409 keys → confirm modal lists them → applied with skipped_env notice", async () => {
    renderScreen();
    const apply = await screen.findByRole("button", { name: /Применить/ });
    fireEvent.click(apply);

    // первый вызов без confirm → 409 {message, keys}
    await waitFor(() => expect(POSTCalls.filter((c) => c.path.endsWith("/apply")).length).toBe(1));
    expect(await screen.findByRole("alertdialog")).toBeTruthy();
    const details = document.querySelector(".modal-details")!;
    expect(details.textContent).toContain("gc_purge_enabled");
    expect(details.textContent).toContain("dedup_enabled");

    fireEvent.click(screen.getByRole("button", { name: "Применяю с опасными" }));
    await waitFor(() => expect(POSTCalls.filter((c) => c.path.endsWith("/apply")).length).toBe(2));
    expect(POSTCalls[1].body).toEqual({ confirm: true });
    // skipped_env показывается уведомлением
    const status = await screen.findByRole("status");
    expect(status.textContent).toContain("Пропущены env-ключи");
    expect(status.textContent).toContain("dedup_threshold");
  });

  it("profiles: create flow opens a modal and posts the new profile", async () => {
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: /Сохранить текущее как профиль/ }));
    const nameInput = await screen.findByPlaceholderText(/Агрессивный GC/);
    fireEvent.change(nameInput, { target: { value: "Ночной режим" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить профиль" }));
    await waitFor(() =>
      expect(POSTCalls.some((c) => c.path === "/api/settings/profiles" && (c.body as { name: string }).name === "Ночной режим")).toBe(true),
    );
  });

  it("groups are collapsible via the sticky header", async () => {
    renderScreen();
    const head = (await screen.findByText("Линкер")).closest("button")!;
    expect(head.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(head);
    await waitFor(() => expect(head.getAttribute("aria-expanded")).toBe("false"));
  });
});
