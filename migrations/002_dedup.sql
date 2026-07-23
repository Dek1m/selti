-- ============================================================
-- 002_dedup.sql — Дедупликация, версионирование, мягкое удаление
-- ============================================================
-- Добавление полей для точной дедупликации (content_hash),
-- типизации источника (source_type, source_location),
-- версионирования (version) и мягкого удаления (is_archived)
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Новые колонки в memories
-- ════════════════════════════════════════════════════════════
-- content_hash: SHA256 хеш содержимого (вычисляется на стороне приложения)
--   NULL для старых записей, которые ещё не прошли хеширование
-- source_type: откуда пришла запись
--   manual | dialogue | code | fact | project_meta
-- source_location: контекст — путь к файлу, session_id и т.п.
-- version: номер версии записи, инкрементится при обновлении факта
-- is_archived: флаг мягкого удаления (true — запись считается удалённой)
-- ════════════════════════════════════════════════════════════

ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_hash      TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_type       TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_location   TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS version           INTEGER NOT NULL DEFAULT 1;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS is_archived       BOOLEAN NOT NULL DEFAULT false;

-- ════════════════════════════════════════════════════════════
-- 2. Уникальный индекс на (namespace, content_hash)
-- ════════════════════════════════════════════════════════════
-- Блокирует точные дубликаты в рамках одного namespace.
-- PostgreSQL пропускает NULL-значения в уникальных индексах,
-- поэтому записи без хеша не участвуют в проверке.
-- Это корректно: без хеша — дедупликация на стороне приложения.
-- ════════════════════════════════════════════════════════════
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash_namespace
    ON memories (namespace, content_hash)
    WHERE content_hash IS NOT NULL;

-- ════════════════════════════════════════════════════════════
-- 3. B-tree индексы для фильтрации по источнику
-- ════════════════════════════════════════════════════════════
-- idx_memories_source_type      — быстрая фильтрация по типу (code, dialogue, ...)
-- idx_memories_source_location  — поиск всех записей из конкретного файла/диалога
-- ════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_memories_source_type
    ON memories (source_type);

CREATE INDEX IF NOT EXISTS idx_memories_source_location
    ON memories (source_location);

-- ════════════════════════════════════════════════════════════
-- DOWN migration — полный откат
-- ════════════════════════════════════════════════════════════
-- DROP INDEX IF EXISTS idx_memories_content_hash_namespace;
-- DROP INDEX IF EXISTS idx_memories_source_type;
-- DROP INDEX IF EXISTS idx_memories_source_location;
-- ALTER TABLE memories DROP COLUMN IF EXISTS content_hash;
-- ALTER TABLE memories DROP COLUMN IF EXISTS source_type;
-- ALTER TABLE memories DROP COLUMN IF EXISTS source_location;
-- ALTER TABLE memories DROP COLUMN IF EXISTS version;
-- ALTER TABLE memories DROP COLUMN IF EXISTS is_archived;
