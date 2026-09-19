#!/usr/bin/env node
/**
 * ZCode-хук «облачко знаний» selti (Фаза 6.3, D9; кеш-протокол Мастера 19.09).
 *
 * Node 18+, zero-deps. Два режима (событие приходит в stdin JSON):
 *
 * SessionStart (startup|resume) — полный инжект:
 *   ~/.zcode/state.md + секция агентов (metadata.json запущенных задач)
 *   + облачко проекта (GET /context/{slug}?refresh=1 — свежий пересчёт).
 *   Digest-файл сохраняется — стартовая точка дельт.
 *
 * UserPromptSubmit — кеш-сохраняющая дельта:
 *   GET /context/{slug}/digest (десятки байт) сверяется с digest-файлом.
 *   Не изменился → exit 0 БЕЗ вывода (префикс-кеш GLM дремлет, ноль
 *   токенов). Изменился → инжект только изменившихся секций (append —
 *   ранее выданный текст никогда не переписывается).
 *
 * Graceful: ЛЮБАЯ ошибка (нет env, таймаут 3с, 404, битый JSON) → пустой
 * хук + exit 0 — сессия стартует без задержек и без текста.
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const HTTP_TIMEOUT_MS = 3000;
const AGENTS_DIR = join(homedir(), ".zcode", "cli", "agents");
const DIGEST_DIR = join(homedir(), ".zcode", "cli", "agents");
const RECENT_COMPLETED_MS = 60 * 60 * 1000; // completed засчитан час
const RUNNING_MAX_AGE_MS = 24 * 60 * 60 * 1000; // старше — зомби (metadata не закрылся)
const AGENTS_CAP = 5;

/** Человекочитаемые имена команды (profileId → имя). */
const AGENT_NAMES = {
  programmer: "Сона",
  "memory-granulator": "Тишь",
  architect: "Эна",
  tester: "Катерина",
  "db-architect": "Нора",
  devops: "Рэй",
  learner: "Луна",
  planner: "Момо",
  "ux-ui-designer": "Ирис",
  "team-lead": "Афина",
  security: "Лита",
  "tech-writer": "Тиамат",
  observability: "Мая",
  networks: "Кира",
  hacker: "Лиз",
  krisy: "Кристи",
};

/** Любой сбой — молча пустой хук: ZCode не должен видеть ошибок облачка. */
function emitEmpty(eventName) {
  process.stdout.write(JSON.stringify({ hookEventName: eventName || "SessionStart" }));
  process.exit(0);
}

/** fetch с таймаутом 3с (AbortController); не-2xx → бросок. */
async function getJson(url) {
  const response = await fetch(url, {
    signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${url}`);
  }
  return await response.json();
}

function sha256(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** Нормализация пути для матча win/unix: слэши, хвостовой /, регистр. */
function normalizePath(p) {
  return String(p).replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

/** ── Секция агентов ────────────────────────────────────────────────────
 *  Сессионные: ~/.zcode/cli/agents/sess_{ZCODE_SESSION_ID}/agent_<id>/metadata.json.
 *  Фоновые (🌙): running-агенты чужих sess_* (ежечасная Тишь и т.п.).
 *  Формат строки: "▶ Сона (programmer) — {description} [running, 25 мин]".
 */
function collectAgents(sessionId) {
  if (!sessionId) return [];
  const now = Date.now();
  const sessionAgents = [];
  const backgroundAgents = [];
  const seenSession = new Set();
  const seenBackground = new Set();

  const readMeta = (metaPath) => {
    try {
      return JSON.parse(readFileSync(metaPath, "utf8"));
    } catch {
      return null;
    }
  };

  const collectFrom = (sessDir, background) => {
    let agentDirs = [];
    try {
      agentDirs = readdirSync(sessDir).filter((d) => d.startsWith("agent_"));
    } catch {
      return;
    }
    for (const dir of agentDirs) {
      const meta = readMeta(join(sessDir, dir, "metadata.json"));
      if (!meta || !meta.profileId || !meta.status) continue;

      // Фоновые: только running. Сессионные: running + completed за час.
      if (meta.status !== "running") {
        if (background || meta.status !== "completed") continue;
        const doneAt = Date.parse(meta.completedAt || "");
        if (!doneAt || now - doneAt > RECENT_COMPLETED_MS) continue;
      }

      const name = AGENT_NAMES[meta.profileId] || meta.profileId;
      const desc = (meta.description || "").slice(0, 80);
      const createdAt = Date.parse(meta.createdAt || "");
      const ageMin = createdAt ? Math.max(1, Math.round((now - createdAt) / 60000)) : null;

      // Зомби-фильтр: running старше суток — metadata не закрылся при смерти
      if (meta.status === "running" && createdAt && now - createdAt > RUNNING_MAX_AGE_MS) {
        return;
      }
      // Дедуп: фоновые циклы плодят одинаковые строки (перезапуски)
      const line = `${background ? "🌙 " : ""}${meta.status === "running" ? "▶" : "✓"} ${name} (${meta.profileId}) — ${desc} [${meta.status}${meta.status === "running" && ageMin ? `, ${ageMin} мин` : ""}]`;
      const seen = background ? seenBackground : seenSession;
      if (seen.has(line)) return;
      seen.add(line);
      (background ? backgroundAgents : sessionAgents).push(line);
    }
  };

  const ownDir = join(AGENTS_DIR, `sess_${sessionId}`);
  collectFrom(ownDir, false);
  if (existsSync(AGENTS_DIR)) {
    for (const sess of readdirSync(AGENTS_DIR)) {
      if (sess === `sess_${sessionId}` || !sess.startsWith("sess_")) continue;
      collectFrom(join(AGENTS_DIR, sess), true);
    }
  }

  const all = [...sessionAgents, ...backgroundAgents].slice(0, AGENTS_CAP);
  return all;
}

function agentsBlock(agents) {
  if (!agents.length) return "";
  return `## Агенты Argenta\n${agents.join("\n")}\n\n`;
}

/** ~/.zcode/state.md → блок «Текущее состояние» (файл ведёт Тишь, ~2 строки). */
function readStateBlock() {
  const statePath = join(homedir(), ".zcode", "state.md");
  try {
    if (!existsSync(statePath)) return "";
    const text = readFileSync(statePath, "utf8").trim();
    if (!text) return "";
    return `## Текущее состояние\n${text}\n\n`;
  } catch {
    return "";
  }
}

/** Digest-файл: точка сравнения дельт (content-addressed). */
function digestPath(slug) {
  return join(DIGEST_DIR, `.cloud-digest-${slug}`);
}

function readDigestFile(slug) {
  try {
    return JSON.parse(readFileSync(digestPath(slug), "utf8"));
  } catch {
    return null;
  }
}

function writeDigestFile(slug, digest) {
  try {
    mkdirSync(DIGEST_DIR, { recursive: true });
    writeFileSync(digestPath(slug), JSON.stringify(digest));
  } catch {
    /* не критично: следующий раунд пересчитает дельту целиком */
  }
}

/** Изменившиеся секции: prev.sections vs next.sections по sha256 строк. */
function changedSections(prev, next) {
  const prevSections = prev?.sections || {};
  const nextSections = next?.sections || {};
  const changed = [];
  for (const [key, lines] of Object.entries(nextSections)) {
    if (!Array.isArray(lines) || !lines.length) continue;
    const hash = sha256(lines.join("\n"));
    if (prevSections[key] !== hash) changed.push({ key, lines });
  }
  return changed;
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
  const eventName = event.hook_event_name || "SessionStart";

  const projectDir = process.env.ZCODE_PROJECT_DIR;
  if (!projectDir) emitEmpty(eventName);

  const baseUrl = (process.env.SELTI_URL || "http://localhost:8000").replace(/\/+$/, "");

  // Хук не имеет доступа к БД selti — только HTTP к реестру
  const registry = await getJson(`${baseUrl}/projects`);
  const project = (registry.projects || []).find(
    (p) => p.local_path && normalizePath(p.local_path) === normalizePath(projectDir),
  );
  if (!project) emitEmpty(eventName);
  const slug = encodeURIComponent(project.slug);

  if (eventName === "UserPromptSubmit") {
    // ── Дельта: digest изменился → только новые/изменённые секции ──
    const digest = await getJson(`${baseUrl}/context/${slug}/digest`);
    const prev = readDigestFile(project.slug);
    if (prev && prev.digest === digest.digest) {
      process.exit(0); // ничего не изменилось: ноль токенов, кеш GLM дремлет
    }
    const context = await getJson(`${baseUrl}/context/${slug}`);
    const changed = changedSections(prev, context.sections);
    if (!changed.length) {
      writeDigestFile(project.slug, { digest: digest.digest, sections: digest.sections });
      process.exit(0);
    }
    const delta = changed
      .map(({ key, lines }) => `### ${key}\n${lines.join("\n")}`)
      .join("\n\n");
    writeDigestFile(project.slug, { digest: digest.digest, sections: digest.sections });
    process.stdout.write(JSON.stringify({
      hookEventName: "UserPromptSubmit",
      additionalContext: `## ☁ Обновление облачка: ${project.name}\n${delta}`,
    }));
    return;
  }

  // ── SessionStart: полный инжект ──
  const agents = collectAgents(process.env.ZCODE_SESSION_ID);
  const context = await getJson(`${baseUrl}/context/${slug}?refresh=1`);
  writeDigestFile(project.slug, {
    digest: sha256(context.content || ""),
    sections: Object.fromEntries(
      Object.entries(context.sections || {}).map(([key, lines]) => [
        key,
        sha256(Array.isArray(lines) ? lines.join("\n") : String(lines)),
      ]),
    ),
  });

  const cloud = [
    `## Облачко знаний: ${project.name}${context.computed_at ? ` (${context.computed_at})` : ""}`,
    context.content || "",
    context.stale ? "_(снапшот устарел: были записи после пересборки)_" : "",
  ].filter(Boolean).join("\n");

  process.stdout.write(JSON.stringify({
    hookEventName: "SessionStart",
    additionalContext: `${readStateBlock()}${agentsBlock(agents)}${cloud}`,
  }));
}

main().catch(() => emitEmpty());
