-- ============================================================
-- 010_qdrant_vector_store.sql — Миграция: pgvector → Qdrant
-- ============================================================
-- Фаза 1: PostgreSQL — удаляем embedding, добавляем sync-таблицу
-- Фаза 2: Python скрипт мигрирует вектора в Qdrant
-- Фаза 3: Обновляем query-паттерны (repository)
-- ============================================================
-- ROLLBACK: см. комментарии в конце файла
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 0. Safety: отключаем автокоммит, работаем в транзакции
-- ════════════════════════════════════════════════════════════
BEGIN;

-- ════════════════════════════════════════════════════════════
-- 1. Таблица-журнал миграции векторов
-- ════════════════════════════════════════════════════════════
-- Хранит статус миграции каждой записи.
-- Используется для:
--   - Отслеживания прогресса (сколько % мигрировано)
--   - Повторной попытки при сбоях (idempotent)
--   - Rollback (знаем, что было мигрировано)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS qdrant_migration_status (
    memory_id   UUID PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'migrated', 'failed', 'skipped')),
    qdrant_point_id TEXT,  -- ID точки в Qdrant (совпадает с memory_id)
    migrated_at TIMESTAMPTZ,
    error_msg   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индекс для быстрого поиска не-мигрированных
CREATE INDEX IF NOT EXISTS idx_qdrant_status_pending
    ON qdrant_migration_status (status)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_qdrant_status_migrated
    ON qdrant_migration_status (status)
    WHERE status = 'migrated';

-- ════════════════════════════════════════════════════════════
-- 2. Заполняем журнал миграции (все активные записи)
-- ════════════════════════════════════════════════════════════
INSERT INTO qdrant_migration_status (memory_id, status)
SELECT id, 'pending'
FROM memories
WHERE is_archived = false
  AND embedding IS NOT NULL
ON CONFLICT (memory_id) DO NOTHING;

-- ════════════════════════════════════════════════════════════
-- 3. После миграции данных в Qdrant (run Python script):
--    раскомментировать блоки ниже
-- ════════════════════════════════════════════════════════════

-- === ФАЗА 3A: Удаляем колонку embedding (ПОСЛЕ верификации) ===
-- ALTER TABLE memories DROP COLUMN embedding;

-- === ФАЗА 3B: Удаляем pgvector extension (если не нужен) ===
-- DROP EXTENSION IF EXISTS vector;

-- === ФАЗА 3C: Очищаем журнал миграции ===
-- DROP TABLE IF EXISTS qdrant_migration_status;

-- ════════════════════════════════════════════════════════════
-- 4. Функция: верификация миграции
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION verify_qdrant_migration()
RETURNS TABLE(
    total_pending  BIGINT,
    total_migrated BIGINT,
    total_failed   BIGINT,
    total_skipped  BIGINT,
    pct_complete   NUMERIC
)
LANGUAGE SQL STABLE
AS $$
    SELECT
        count(*) FILTER (WHERE status = 'pending')  AS total_pending,
        count(*) FILTER (WHERE status = 'migrated') AS total_migrated,
        count(*) FILTER (WHERE status = 'failed')   AS total_failed,
        count(*) FILTER (WHERE status = 'skipped')  AS total_skipped,
        CASE
            WHEN count(*) = 0 THEN 100.0
            ELSE round(
                count(*) FILTER (WHERE status = 'migrated')::numeric
                / count(*) * 100, 2
            )
        END AS pct_complete
    FROM qdrant_migration_status;
$$;

-- ════════════════════════════════════════════════════════════
-- 5. Представление: быстрый обзор по namespace
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE VIEW qdrant_migration_by_namespace AS
SELECT
    m.namespace,
    count(*) AS total,
    count(*) FILTER (WHERE ms.status = 'migrated') AS migrated,
    count(*) FILTER (WHERE ms.status = 'pending')  AS pending,
    count(*) FILTER (WHERE ms.status = 'failed')   AS failed
FROM memories m
JOIN qdrant_migration_status ms ON ms.memory_id = m.id
GROUP BY m.namespace
ORDER BY m.namespace;

COMMIT;

-- ════════════════════════════════════════════════════════════
-- ROLLBACK (откат миграции)
-- ════════════════════════════════════════════════════════════
-- Если нужно откатить ДО удаления колонки embedding:
--   1. Остановить selti сервер
--   2. Выполнить миграцию данных обратно (Python скрипт с --rollback)
--   3. Выполнить:
--      DROP VIEW IF EXISTS qdrant_migration_by_namespace;
--      DROP FUNCTION IF EXISTS verify_qdrant_migration();
--      DROP TABLE IF EXISTS qdrant_migration_status;
--
-- Если колонка embedding УЖЕ удалена:
--   1. ALTER TABLE memories ADD COLUMN embedding vector(4096);
--   2. Запустить Python скрипт --rollback (импорт из Qdrant → PostgreSQL)
--   3. Выполнить шаги выше
