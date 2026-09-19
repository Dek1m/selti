#!/usr/bin/env node
/**
 * ZCode-плагин selti-sync — автозапись git-воркспейса в реестр проектов
 * selti (ADR-018, решение D: только POST, без GET).
 *
 * Node 18+, zero-deps. SessionStart (startup|resume):
 *   stdin JSON → ZCODE_PROJECT_DIR → git remote get-url origin (таймаут 2с)
 *   → POST {selti}/projects/register (X-SELTI-KEY, таймаут 3с).
 *   201 → одна строка additionalContext (проект зарегистрирован);
 *   200/409/любая ошибка → тишина, exit 0.
 *
 * Идемпотентность живёт на сервере: повторный POST по паре slug+repo_url —
 * no-op (200 matched), поэтому GET /projects не нужен (плагин stateless).
 * Известное следствие ADR: облачко знаний новому проекту приходит со
 * следующей сессии (knowledge-cloud успевает прочитать реестр до нас).
 *
 * Graceful: ЛЮБАЯ ошибка (нет env, не-git папка, таймаут, битый JSON) →
 * пустой вывод + exit 0 — сессия стартует без задержек и без текста.
 */

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const GIT_TIMEOUT_MS = 2000;
const HTTP_TIMEOUT_MS = 3000;

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

/** Нормализация пути воркспейса в slug-кандидат: basename по win/unix слэшам.
 *  "E:\Projects\Python\albedo" и "E:/Projects/Python/albedo/" → "albedo". */
export function basenameOf(projectDir) {
  const parts = String(projectDir).split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : null;
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

  const projectDir = process.env.ZCODE_PROJECT_DIR;
  if (!projectDir) process.exit(0);
  const origin = readGitOrigin(projectDir);
  if (!origin) process.exit(0);
  const slug = basenameOf(projectDir);
  if (!slug) process.exit(0);

  const headers = { "content-type": "application/json" };
  const apiKey = readApiKey();
  if (apiKey) headers["x-selti-key"] = apiKey;

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
  if (response.status !== 201) process.exit(0); // 200/409/ошибки — молча

  process.stdout.write(JSON.stringify({
    hookEventName: eventName,
    additionalContext: `selti: проект «${slug}» зарегистрирован в реестре (slug ${slug})`,
  }));
}

// main() — только при прямом запуске: чистые функции импортирует node-тест
const invokedDirectly = Boolean(process.argv[1])
  && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  main().catch(() => process.exit(0)); // graceful: сессия стартует всегда
}
