-- ============================================================
-- 001_initial.sql — Initial schema for Memory MCP Server
-- ============================================================
-- pgvector extension, таблица memories, индексы, триггеры
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ════════════════════════════════════════════════════════════
-- Хелпер: автообновление updated_at
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════
-- Таблица memories
-- ════════════════════════════════════════════════════════════
-- embedding: vector(4096) — под qwen3-embedding-8b
-- metadata: JSONB — гибкие метаданные, индексируется через GIN при необходимости
-- namespace: логическая изоляция данных (multi-tenant)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS memories (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(4096),
    metadata    JSONB DEFAULT '{}'::jsonb,
    namespace   TEXT NOT NULL DEFAULT 'default',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ════════════════════════════════════════════════════════════
-- B-tree индексы для фильтрации
-- ════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_memories_user_id    ON memories (user_id);
CREATE INDEX IF NOT EXISTS idx_memories_namespace   ON memories (namespace);
CREATE INDEX IF NOT EXISTS idx_memories_created_at  ON memories (created_at DESC);

-- ════════════════════════════════════════════════════════════
-- Векторный индекс (пропущен — используем точный поиск)
-- ════════════════════════════════════════════════════════════
-- pgvector ограничивает индексы 2000 измерениями (из-за страницы 8KB).
-- У нас 4096-dim, поэтому используем точный поиск (sequential scan).
-- Оператор <=> корректно работает без индекса на любом количестве измерений.
--
-- Для датасета <100K записей точный поиск даёт latency ~50-500ms,
-- что приемлемо для памяти-сервера.
--
-- КОГДА ДОБАВИТЬ ИНДЕКС:
-- Если датасет вырастет >100K и latency станет критичной:
--   Вариант A: Использовать halfvec(4096) + HNSW (потеря точности fp16)
--     CREATE INDEX ON memories USING hnsw ((embedding::halfvec(4096)) halfvec_cosine_ops);
--   Вариант B: Использовать binary_quantize + HNSW (битовый, до 64000 dim)
--     CREATE INDEX ON memories USING hnsw ((binary_quantize(embedding)::bit(4096)) bit_hamming_ops);
--     + точный re-rank на top-K результатах
--   Вариант C: pgvectorscale (DiskANN) — без ограничения dim, но нужен extension

-- ════════════════════════════════════════════════════════════
-- Триггер автообновления updated_at
-- ════════════════════════════════════════════════════════════
CREATE TRIGGER trg_memories_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ════════════════════════════════════════════════════════════
-- GIN индекс на metadata (по необходимости)
-- ════════════════════════════════════════════════════════════
-- Раскомментировать, если планируются частые фильтры по metadata->>'key'
-- CREATE INDEX IF NOT EXISTS idx_memories_metadata ON memories USING gin (metadata jsonb_path_ops);

-- ════════════════════════════════════════════════════════════
-- Функция для приближённого поиска (через IVFFlat или без индекса)
-- ════════════════════════════════════════════════════════════
-- Использование:
-- SELECT * FROM search_memories_approx('user_1', embedding, 0.7, 100);
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION search_memories_approx(
    p_user_id TEXT,
    p_embedding vector(4096),
    p_threshold FLOAT DEFAULT 0.7,
    p_limit INT DEFAULT 20
)
RETURNS TABLE(
    id UUID,
    user_id TEXT,
    content TEXT,
    metadata JSONB,
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
        1 - (m.embedding <=> p_embedding) AS score
    FROM memories m
    WHERE m.user_id = p_user_id
      AND 1 - (m.embedding <=> p_embedding) >= p_threshold
    ORDER BY m.embedding <=> p_embedding
    LIMIT p_limit;
$$;

-- ════════════════════════════════════════════════════════════
-- DOWN migration
-- ════════════════════════════════════════════════════════════
-- DROP TRIGGER IF EXISTS trg_memories_updated_at ON memories;
-- DROP TABLE IF EXISTS memories CASCADE;
-- DROP FUNCTION IF EXISTS update_updated_at_column();
-- DROP FUNCTION IF EXISTS search_memories_approx(vector(4096), float, int);
