-- ============================================================
-- 011_drop_pgvector.sql — Финальная миграция: полный уход от pgvector
-- ============================================================
-- ПРИМЕНЯТЬ ТОЛЬКО после:
--   1. Успешного переноса ВСЕХ эмбеддингов в Qdrant
--   2. Верификации: verify_qdrant_migration() показывает 100% migrated
--   3. Прогонки тестов с Qdrant-поиском
--   4. Минимум 24 часа наблюдения за стабильностью
-- ============================================================
-- ROLLBACK: см. конец файла
-- ============================================================

BEGIN;

-- ════════════════════════════════════════════════════════════
-- 1. Удаляем векторные индексы (если остались)
-- ════════════════════════════════════════════════════════════
-- В проекте индексы не создавались (4096-dim > limit 2000),
-- но на всякий случай — safety net
DROP INDEX IF EXISTS idx_memories_embedding_ivfflat;
DROP INDEX IF EXISTS idx_memories_embedding_hnsw;

-- ════════════════════════════════════════════════════════════
-- 2. Удаляем колонку embedding
-- ════════════════════════════════════════════════════════════
-- После этого PostgreSQL освобождает ~16 КБ на запись (vector(4096) = 16 КБ)
-- При 1.2M записей: ~19 ГБ диска
ALTER TABLE memories DROP COLUMN IF EXISTS embedding;

-- ════════════════════════════════════════════════════════════
-- 3. Удаляем extension pgvector
-- ════════════════════════════════════════════════════════════
-- Без колонки vector extension не нужен
DROP FUNCTION IF EXISTS memory_upsert(text,text,vector,uuid,jsonb,text,text,integer);
DROP FUNCTION IF EXISTS memory_search_hnsw(vector,text,text,double precision,integer);
DROP FUNCTION IF EXISTS search_memories_approx(text,vector,double precision,integer);
DROP FUNCTION IF EXISTS search_memories_approx(text,vector,double precision,integer,text);
DROP EXTENSION IF EXISTS vector;

-- ════════════════════════════════════════════════════════════
-- 4. Удаляем SQL-функцию search_memories_approx
-- ════════════════════════════════════════════════════════════
-- Она依赖ует на оператор <=> из pgvector
DROP FUNCTION IF EXISTS search_memories_approx(TEXT, vector(4096), FLOAT, INT);

-- ════════════════════════════════════════════════════════════
-- 5. Очищаем журнал миграции (если ещё существует)
-- ════════════════════════════════════════════════════════════
DROP VIEW IF EXISTS qdrant_migration_by_namespace;
DROP FUNCTION IF EXISTS verify_qdrant_migration();
DROP TABLE IF EXISTS qdrant_migration_status;

-- ════════════════════════════════════════════════════════════
-- 6. VACUUM — возврат диска и обновление статистики
-- ════════════════════════════════════════════════════════════
-- VACUUM ANALYZE в транзакции не работает, делаем после COMMIT
-- (см. ниже)

COMMIT;

-- VACUUM ANALYZE — вне транзакции (требует VACUUM FULL для возврата диска)
-- Выполнить отдельно после COMMIT:
-- VACUUM ANALYZE memories;

-- ════════════════════════════════════════════════════════════
-- ROLLBACK (если что-то пошло не так)
-- ════════════════════════════════════════════════════════════
-- Если миграция применена, но нужно откатить:
--
-- 1. Остановить selti сервер
-- 2. Выполнить:
--
--    -- Восстановить extension
--    CREATE EXTENSION IF NOT EXISTS vector;
--
--    -- Восстановить колонку (данные потеряны — нужен импорт из Qdrant)
--    ALTER TABLE memories ADD COLUMN embedding vector(4096);
--
--    -- Восстановить функцию
--    CREATE OR REPLACE FUNCTION search_memories_approx(
--        p_user_id TEXT,
--        p_embedding vector(4096),
--        p_threshold FLOAT DEFAULT 0.7,
--        p_limit INT DEFAULT 20
--    )
--    RETURNS TABLE(
--        id UUID, user_id TEXT, content TEXT, metadata JSONB, score FLOAT
--    )
--    LANGUAGE SQL STABLE
--    AS $$
--        SELECT m.id, m.user_id, m.content, m.metadata,
--               1 - (m.embedding <=> p_embedding) AS score
--        FROM memories m
--        WHERE m.user_id = p_user_id
--          AND 1 - (m.embedding <=> p_embedding) >= p_threshold
--        ORDER BY m.embedding <=> p_embedding
--        LIMIT p_limit;
--    $$;
--
--    -- Запустить импорт из Qdrant (Python скрипт)
--    python -m migrations.migrate_vectors_to_qdrant --rollback
--
-- 3. Откатить Python-код (git revert)
-- 4. Пересобрать Docker образ с pgvector
-- 5. Запустить selti
