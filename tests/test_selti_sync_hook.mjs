/**
 * Node-тест чистой логики плагина selti-sync (ADR-018): basenameOf,
 * маркер-кеш (чтение с TTL, mismatch repo_url, no-marker), журнал (строка,
 * обрезка). Запуск из корня репо selti: node --test tests/test_selti_sync_hook.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  basenameOf,
  parseMarkerMap,
  isMarkerHit,
  withMarkerEntry,
  logLine,
  truncateLogTail,
} from "../zcode-plugin-selti-sync/scripts/selti-sync.mjs";

const TTL_MS = 24 * 60 * 60 * 1000;

// --- basenameOf -----------------------------------------------------------

test("win-путь с backslash → basename", () => {
  assert.equal(basenameOf("E:\\Projects\\Python\\albedo"), "albedo");
});

test("unix-путь → basename", () => {
  assert.equal(basenameOf("/home/sergey/projects/selti"), "selti");
});

test("trailing slash (win и unix) не портит basename", () => {
  assert.equal(basenameOf("E:\\Projects\\Python\\selti\\"), "selti");
  assert.equal(basenameOf("/home/sergey/projects/selti/"), "selti");
});

test("пустая строка → null (хук молчит)", () => {
  assert.equal(basenameOf(""), null);
});

// --- parseMarkerMap -------------------------------------------------------

test("валидный JSON-объект → карта маркера", () => {
  const map = parseMarkerMap('{"E:\\\\p":{"repo_url":"https://x.git","ts":1}}');
  assert.deepEqual(map, { "E:\\p": { repo_url: "https://x.git", ts: 1 } });
});

test("битый JSON → пустая карта (полный путь вместо быстрого)", () => {
  assert.deepEqual(parseMarkerMap("{not json"), {});
});

test("массив/примитив → пустая карта", () => {
  assert.deepEqual(parseMarkerMap("[1,2]"), {});
  assert.deepEqual(parseMarkerMap('"str"'), {});
});

// --- isMarkerHit: TTL -----------------------------------------------------

test("свежая запись (<24ч) → marker-hit, git не нужен", () => {
  const now = Date.now();
  assert.equal(isMarkerHit({ repo_url: "https://x.git", ts: now - TTL_MS + 1000 }, now), true);
});

test("TTL истёк (>=24ч) → miss: перерегистрация обновит маркер", () => {
  const now = Date.now();
  assert.equal(isMarkerHit({ repo_url: "https://x.git", ts: now - TTL_MS }, now), false);
  assert.equal(isMarkerHit({ repo_url: "https://x.git", ts: now - TTL_MS - 1 }, now), false);
});

test("ts из будущего (грубый битый маркер) → hit не срабатывает вечно", () => {
  const now = Date.now();
  assert.equal(isMarkerHit({ repo_url: "https://x.git", ts: now + 10 * TTL_MS }, now), false);
});

// --- isMarkerHit: некорректные записи --------------------------------------

test("no-marker (нет записи / null) → miss", () => {
  assert.equal(isMarkerHit(undefined, Date.now()), false);
  assert.equal(isMarkerHit(null, Date.now()), false);
});

test("запись без repo_url / ts не число → miss", () => {
  const now = Date.now();
  assert.equal(isMarkerHit({ ts: now }, now), false);
  assert.equal(isMarkerHit({ repo_url: "https://x.git" }, now), false);
  assert.equal(isMarkerHit({ repo_url: "https://x.git", ts: NaN }, now), false);
});

// --- withMarkerEntry: mismatch repo_url ------------------------------------

test("mismatch repo_url: чужая запись замещается, ключ один", () => {
  const now = Date.now();
  const map = withMarkerEntry({ "E:\\w": { repo_url: "https://old.git", ts: now - 10 } }, "E:\\w", "https://new.git", now);
  assert.deepEqual(map["E:\\w"], { repo_url: "https://new.git", ts: now });
  assert.equal(Object.keys(map).length, 1);
});

test("запись нового воркспейса не трогает соседние", () => {
  const now = Date.now();
  const old = { "E:\\a": { repo_url: "https://a.git", ts: 1 } };
  const map = withMarkerEntry(old, "E:\\b", "https://b.git", now);
  assert.deepEqual(map["E:\\a"], old["E:\\a"]);
  assert.deepEqual(map["E:\\b"], { repo_url: "https://b.git", ts: now });
});

// --- logLine ---------------------------------------------------------------

test("строка журнала: ISO, событие, slug, outcome, перенос", () => {
  const line = logLine("2026-09-28T00:30:00.000Z", "post_tool_use", "alexa", "201");
  assert.equal(line, "2026-09-28T00:30:00.000Z post_tool_use slug=alexa outcome=201\n");
});

test("строка журнала: error-исход однострочный", () => {
  const line = logLine("2026-09-28T00:30:00.000Z", "session_start", "alexa", "error:TimeoutError");
  assert.equal(line.match(/\n/g).length, 1);
});

// --- truncateLogTail --------------------------------------------------------

test("короткий журнал → без изменений", () => {
  const content = "a\nb\nc\n";
  assert.equal(truncateLogTail(content), content);
});

test("длинный журнал → ~последние keepBytes, срез по границе строки", () => {
  const line = "x".repeat(99) + "\n"; // 100 байт на строку
  const content = line.repeat(1200); // 120КБ > max 512? нет — зададим max меньше
  const tail = truncateLogTail(content, 5000, 1000);
  assert.ok(Buffer.byteLength(tail, "utf8") <= 1100, `tail=${tail.length}`);
  assert.ok(tail.endsWith("\n"));
  assert.ok(tail.startsWith("x"), "срез выровнен по началу строки, не посередине");
  assert.ok(tail.length <= 1000 + 100);
});

test("truncate не рвёт кириллический slug (UTF-8 многобайтовость)", () => {
  const line = "слаг-строка-журнала-длинная\n"; // кириллица: байт > символов
  const content = line.repeat(4000);
  const tail = truncateLogTail(content, 10000, 2000);
  // первый символ — начало валидной строки (не разорванный кодпоинт)
  assert.ok(tail.startsWith("слаг"), tail.slice(0, 20));
});
