#!/usr/bin/env node
/**
 * ZCode-плагин selti-sync — автозапись git-воркспейса в реестр проектов
 * selti (ADR-018, решение D: только POST, без GET).
 *
 * Node 18+, zero-deps. Два триггера (hooks/hooks.json):
 *
 *   1. SessionStart (startup|resume) — регистрация на старте сессии.
 *      resume остался осознанно: догоняющая регистрация живой сессии
 *      (кейс alexa 28.09: origin появился ночью, зарегали при утреннем
 *      авто-resume).
 *   2. PostToolUse (matcher "Bash") — ленивая догоняющая регистрация:
 *      git init / git remote add появляются именно через Bash-команды,
 *      ждём не следующего старта, а следующего Bash-вызова.
 *
 * Общий алгоритм: ZCODE_PROJECT_DIR → slug = basename → локальный
 * кеш-маркер ~/.zcode/selti-sync/registered.json ({ "<path>": {repo_url, ts} },
 * TTL 24ч; свежая запись → тихий exit без git-вызова, быстрый путь ~5мс)
 * → git remote get-url origin (таймаут 2с; нет → тихий exit)
 * → POST {selti}/projects/register (X-SELTI-KEY, таймаут 3с).
 *   201 → маркер + (только на SessionStart) строка additionalContext;
 *   200/409 → маркер, тишина; ошибка → тишина и БЕЗ маркера
 *   (повтор на следующем событии).
 *
 * Лог-журнал ~/.zcode/selti-sync/sync.log — по строке на срабатывание:
 * "<ISO> <session_start|post_tool_use> slug=<slug> outcome=<...>";
 * при превышении 512КБ обрезается до последних 100КБ.
 *
 * На PostToolUse — НИКАКОГО stdout (сессию не засоряем); на SessionStart —
 * одна строка контекста при 201. Идемпотентность — на сервере: повторный
 * POST по паре slug+repo_url — no-op (200 matched). Облачко знаний
 * новому проекту приходит со следующей сессии (ADR-018).
 *
 * Graceful: ЛЮБАЯ ошибка (нет env, не-git папка, таймаут, битый JSON) →
 * пустой вывод + exit 0 — сессия стартует без задержек и без текста.
 */

import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const GIT_TIMEOUT_MS = 2000;
const HTTP_TIMEOUT_MS = 3000;
const MARKER_TTL_MS = 24 * 60 * 60 * 1000;
const LOG_MAX_BYTES = 512 * 1024;
const LOG_KEEP_BYTES = 100 * 1024;

/** Базовый URL selti: env SELTI_URL → файл ~/.zcode/selti-url → localhost.
 *  Тот же порядок, что у глобального хука knowledge-cloud — один конфиг на хост. */
function seltiBaseUrl() {
  if (process.env.SELTI_URL) return process.env.SELTI_URL.replace(/\/+$/, "");
  try {
    const fromFile = readFileSync(join(homedir(), ".zcode", "selti-url"), "utf8").trim();
    if (fromFile) return fromFile.replace(/\/+$/, "");
  } catch {
    /* файла нет — дефолт */
  }
  return "http://localhost:8000";
}

/** Ключ X-SELTI-KEY: env SELTI_API_KEY → файл ~/.zcode/selti-key → null (нет). */
function readApiKey() {
  if (process.env.SELTI_API_KEY) return process.env.SELTI_API_KEY.trim();
  try {
    const fromFile = readFileSync(join(homedir(), ".zcode", "selti-key"), "utf8").trim();
    if (fromFile) return fromFile;
  } catch {
    /* файла нет — ключа нет */
  }
  return null;
}

function stateDir() {
  return join(homedir(), ".zcode", "selti-sync");
}

/** Нормализация пути воркспейса в slug-кандидат: basename по win/unix слэшам.
 *  "E:\Projects\Python\albedo" и "E:/Projects/Python/albedo/" → "albedo". */
export function basenameOf(projectDir) {
  const parts = String(projectDir).split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : null;
}

/** Разбор файла маркера в карту workspacePath → {repo_url, ts}.
 *  Битый JSON / не-объект → {}: полный путь вместо быстрого. */
export function parseMarkerMap(text) {
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Быстрый путь: запись свежая (TTL 24ч) и с repo_url → git и POST не нужны.
 *  Текущий origin не сверяем осознанно: его узнаёт только git-вызов, который
 *  здесь запрещён (<10мс); смена origin догоняется после истечения TTL.
 *  Отрицательный возраст (битый/подкрученный ts) — miss, а не вечный hit. */
export function isMarkerHit(entry, nowMs, ttlMs = MARKER_TTL_MS) {
  return Boolean(entry) && typeof entry === "object"
    && typeof entry.repo_url === "string" && entry.repo_url !== ""
    && Number.isFinite(entry.ts) && entry.ts <= nowMs && nowMs - entry.ts < ttlMs;
}

/** Чистое обновление карты маркера: чужой repo_url / протухший ts
 *  перезаписываются (mismatch → следующая регистрация замещает запись). */
export function withMarkerEntry(map, workspacePath, repoUrl, nowMs) {
  return { ...map, [workspacePath]: { repo_url: repoUrl, ts: nowMs } };
}

/** Строка журнала: "<ISO> <event> slug=<slug> outcome=<outcome>". */
export function logLine(nowIso, event, slug, outcome) {
  return `${nowIso} ${event} slug=${slug} outcome=${outcome}\n`;
}

/** Обрезка журнала: >maxBytes → последние ~keepBytes, выровненные по границе
 *  строки (срез по символам — не рвём UTF-8 многословных slug'ов). */
export function truncateLogTail(content, maxBytes = LOG_MAX_BYTES, keepBytes = LOG_KEEP_BYTES) {
  if (Buffer.byteLength(content, "utf8") <= maxBytes) return content;
  const tail = content.slice(-keepBytes);
  const lineStart = tail.indexOf("\n");
  return lineStart >= 0 ? tail.slice(lineStart + 1) : tail;
}

function readMarkerMap() {
  try {
    return parseMarkerMap(readFileSync(join(stateDir(), "registered.json"), "utf8"));
  } catch {
    return {};
  }
}

function writeMarkerMap(map) {
  try {
    mkdirSync(stateDir(), { recursive: true });
    writeFileSync(join(stateDir(), "registered.json"), JSON.stringify(map, null, 2));
  } catch {
    /* маркер — оптимизация: сбой записи не ломает хук */
  }
}

/** Журнал срабатываний (append + обрезка перезаписью). Сбой лога — тишина. */
function appendLog(event, slug, outcome) {
  try {
    let content = "";
    try {
      content = readFileSync(join(stateDir(), "sync.log"), "utf8");
    } catch {
      /* первого файла ещё нет */
    }
    mkdirSync(stateDir(), { recursive: true });
    writeFileSync(
      join(stateDir(), "sync.log"),
      truncateLogTail(content) + logLine(new Date().toISOString(), event, slug, outcome),
    );
  } catch {
    /* лог не должен ломать хук */
  }
}

/** Remote origin: не-git папка / нет origin / таймаут git → null (хук молчит —
 *  не-git папки и клоны без origin не замусоривают реестр). */
function readGitOrigin(projectDir) {
  let result;
  try {
    result = spawnSync("git", ["remote", "get-url", "origin"], {
      cwd: projectDir,
      timeout: GIT_TIMEOUT_MS,
      encoding: "utf8",
    });
  } catch {
    return null;
  }
  if (result.status !== 0 || !result.stdout) return null;
  const origin = result.stdout.trim();
  return origin || null;
}

async function main() {
  let raw = "";
  try {
    raw = readFileSync(0, "utf8"); // stdin: ZCode передаёт JSON события и закрывает
  } catch {
    raw = "";
  }
  let event = {};
  try {
    event = raw.trim() ? JSON.parse(raw) : {};
  } catch {
    event = {};
  }
  // hookEventName в выводе ОБЯЗАН совпадать с событием (валидатор ZCode
  // отклоняет чужое имя → hook.run.failed)
  const eventName = event.hook_event_name || "SessionStart";
  const eventKind = eventName === "PostToolUse" ? "post_tool_use" : "session_start";

  const projectDir = process.env.ZCODE_PROJECT_DIR;
  if (!projectDir) process.exit(0);
  const slug = basenameOf(projectDir);

  // Быстрый путь PostToolUse: свежий маркер → ни git, ни POST (~5мс)
  const marker = readMarkerMap();
  if (isMarkerHit(marker[projectDir], Date.now())) {
    appendLog(eventKind, slug || projectDir, "skip-marker");
    process.exit(0);
  }

  const origin = readGitOrigin(projectDir);
  if (!origin) {
    if (slug) appendLog(eventKind, slug, "no-origin");
    process.exit(0);
  }
  if (!slug) process.exit(0);

  const headers = { "content-type": "application/json" };
  const apiKey = readApiKey();
  if (apiKey) headers["x-selti-key"] = apiKey;

  let status = 0;
  try {
    const response = await fetch(`${seltiBaseUrl()}/projects/register`, {
      method: "POST",
      signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
      headers,
      body: JSON.stringify({
        slug,
        name: slug, // ADR: name = slug
        kind: "code", // плагин регистрирует только git-репозитории
        local_path: projectDir,
        repo_url: origin,
      }),
    });
    status = response.status;
  } catch (err) {
    // без маркера: повтор на следующем событии (startup|resume|Bash)
    appendLog(eventKind, slug, `error:${err?.name ?? "fetch"}`);
    process.exit(0);
  }
  if (status !== 201 && status !== 200 && status !== 409) {
    appendLog(eventKind, slug, `error:http-${status}`);
    process.exit(0);
  }

  writeMarkerMap(withMarkerEntry(marker, projectDir, origin, Date.now()));
  appendLog(eventKind, slug, String(status));

  // Строка контекста — только SessionStart и только на freshly-created
  if (status === 201 && eventKind === "session_start") {
    process.stdout.write(JSON.stringify({
      hookEventName: eventName,
      additionalContext: `selti: проект «${slug}» зарегистрирован в реестре (slug ${slug})`,
    }));
  }
}

// main() — только при прямом запуске: чистые функции импортирует node-тест
const invokedDirectly = Boolean(process.argv[1])
  && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  main().catch(() => process.exit(0)); // graceful: сессия стартует всегда
}
