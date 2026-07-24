-- 003_athene_memory.sql
-- ============================================================
-- Миграция в выделенную БД athene_memory
-- + оптимизация индексов
-- ============================================================
-- ВНИМАНИЕ: ДО этой миграции нужно создать БД:
--   CREATE DATABASE athene_memory;
-- И подключиться к ней. Миграция запускается в контексте athene_memory.

-- ════════════════════════════════════════════════════════════
-- 1. pgvector extension
-- ════════════════════════════════════════════════════════════
CREATE EXTENSION IF NOT EXISTS vector;

-- ════════════════════════════════════════════════════════════
-- 2. Функция автообновления updated_at
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════
-- 3. Таблица memories (оптимизированная)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(4096),
    metadata        JSONB DEFAULT '{}'::jsonb,
    namespace       TEXT NOT NULL DEFAULT 'default',
    content_hash    TEXT,
    source_type     TEXT,
    source_location TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    is_archived     BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_namespace CHECK (
        namespace IN ('default', 'user_facts', 'code_knowledge', 'dialogue_insights', 'project_meta')
    )
);

COMMENT ON TABLE memories IS 'Центральное хранилище памяти для athena-memory. Использует namespace для логического разделения типов сущностей.';
COMMENT ON COLUMN memories.embedding IS 'vector(4096) — эмбеддинг от qwen3-embedding-8b. Точный поиск (без индекса) из-за ограничения pgvector в 2000 dim для HNSW.';
COMMENT ON COLUMN memories.namespace IS 'Тип памяти: default, user_facts, code_knowledge, dialogue_insights, project_meta';
COMMENT ON COLUMN memories.content_hash IS 'SHA256 хеш контента для точной дедупликации';
COMMENT ON COLUMN memories.version IS 'Инкрементится при обновлении записи';
COMMENT ON COLUMN memories.is_archived IS 'Мягкое удаление: true = запись считается удалённой, исключается из поиска';

-- ════════════════════════════════════════════════════════════
-- 4. Индексы
-- ════════════════════════════════════════════════════════════

-- Основной композит: покрывает list (ORDER BY created_at DESC),
-- stats (GROUP BY namespace), forget (DELETE WHERE user_id + namespace)
CREATE INDEX IF NOT EXISTS idx_memories_user_ns_updated
    ON memories (user_id, namespace, updated_at DESC);

-- Уникальный индекс для дедупликации: (namespace, content_hash)
-- Partial: NULL content_hash не попадает в индекс
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_ns_hash
    ON memories (namespace, content_hash)
    WHERE content_hash IS NOT NULL;

-- Partial index для активных записей — ускоряет forget и stats
CREATE INDEX IF NOT EXISTS idx_memories_active
    ON memories (user_id, namespace)
    WHERE is_archived = false;

-- ════════════════════════════════════════════════════════════
-- 5. Триггер автообновления
-- ════════════════════════════════════════════════════════════
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_memories_updated_at'
          AND tgrelid = 'memories'::regclass
    ) THEN
        CREATE TRIGGER trg_memories_updated_at
            BEFORE UPDATE ON memories
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    END IF;
END;
$$;

-- ════════════════════════════════════════════════════════════
-- 6. Функция поиска для семантического поиска
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION search_memories_approx(
    p_user_id TEXT,
    p_embedding vector(4096),
    p_threshold FLOAT DEFAULT 0.7,
    p_limit INT DEFAULT 20,
    p_namespace TEXT DEFAULT NULL
)
RETURNS TABLE(
    id UUID,
    user_id TEXT,
    content TEXT,
    metadata JSONB,
    namespace TEXT,
    score FLOAT
)
LANGUAGE SQL
STABLE
AS $$
    SELECT
        m.id,
        m.user_id,
        m.content,
        m.metadata,
        m.namespace,
        1 - (m.embedding <=> p_embedding) AS score
    FROM memories m
    WHERE m.user_id = p_user_id
      AND m.is_archived = false
      AND (p_namespace IS NULL OR m.namespace = p_namespace)
      AND 1 - (m.embedding <=> p_embedding) >= p_threshold
    ORDER BY m.embedding <=> p_embedding
    LIMIT p_limit;
$$;

-- ════════════════════════════════════════════════════════════
-- DOWN: удаляем всё
-- ════════════════════════════════════════════════════════════
-- DROP TRIGGER IF EXISTS trg_memories_updated_at ON memories;
-- DROP FUNCTION IF EXISTS update_updated_at_column();
-- DROP FUNCTION IF EXISTS search_memories_approx(TEXT, vector(4096), FLOAT, INT, TEXT);
-- DROP TABLE IF EXISTS memories;
-- DROP EXTENSION IF EXISTS vector;
