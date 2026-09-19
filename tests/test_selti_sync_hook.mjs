/**
 * Node-тест чистой логики хука selti-sync (ADR-018): basenameOf —
 * нормализация пути воркспейса в slug-кандидат.
 * Запуск из корня репо selti: node --test tests/test_selti_sync_hook.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { basenameOf } from "../zcode-plugin-selti-sync/scripts/selti-sync.mjs";

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
