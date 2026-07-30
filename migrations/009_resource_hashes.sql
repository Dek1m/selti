-- ============================================================
-- 009_resource_hashes.sql — Хеши ресурсов для дедупликации
-- ============================================================
-- Таблица для хранения SHA256-хешей источников (сессии, файлы,
-- проекты). Используется для определения stale-записей —
-- если хеш не изменился, данные можно пропустить.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- Хелпер: автообновление updated_at (только при смене хеша)
-- ════════════════════════════════════════════════════════════
-- Отличается от update_updated_at_column() из 001_initial.sql:
-- updated_at обновляется ТОЛЬКО при изменении content_hash,
-- что экономит запись при обновлении метаданных без изменения
-- фактического контента.
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION update_resource_hashes_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
        NEW.updated_at = now();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════
-- Таблица resource_hashes
-- ════════════════════════════════════════════════════════════
-- source_type: тип источника (session/file/project)
-- source_id:   идентификатор источника в рамках типа
-- content_hash: SHA256, 64 hex-символа
-- metadata:    JSONB — дополнительные атрибуты (project, path и т.д.)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS resource_hashes (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type   TEXT NOT NULL CHECK (source_type IN ('session', 'file', 'project')),
    source_id     TEXT NOT NULL,
    content_hash  TEXT NOT NULL,  -- SHA256, 64 hex chars
    size_bytes    BIGINT,
    metadata      JSONB DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(source_type, source_id)
);

-- ════════════════════════════════════════════════════════════
-- Индексы
-- ════════════════════════════════════════════════════════════

-- Быстрый поиск stale-записей: фильтр по типу + сортировка по дате
CREATE INDEX IF NOT EXISTS idx_resource_hashes_source_type_updated
    ON resource_hashes (source_type, updated_at DESC);

-- Точный поиск по source_id (если тип неизвестен)
CREATE INDEX IF NOT EXISTS idx_resource_hashes_source_id
    ON resource_hashes (source_id);

-- ════════════════════════════════════════════════════════════
-- Триггер автообновления updated_at
-- ════════════════════════════════════════════════════════════
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_resource_hashes_updated_at'
          AND tgrelid = 'resource_hashes'::regclass
    ) THEN
        CREATE TRIGGER trg_resource_hashes_updated_at
            BEFORE UPDATE ON resource_hashes
            FOR EACH ROW
            EXECUTE FUNCTION update_resource_hashes_updated_at();
    END IF;
END;
$$;

-- ════════════════════════════════════════════════════════════
-- DOWN migration (выполнить вручную при откате)
-- ════════════════════════════════════════════════════════════
-- DROP TRIGGER IF EXISTS trg_resource_hashes_updated_at ON resource_hashes;
-- DROP FUNCTION IF EXISTS update_resource_hashes_updated_at();
-- DROP TABLE IF EXISTS resource_hashes;
