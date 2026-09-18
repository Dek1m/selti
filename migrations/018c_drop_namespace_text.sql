-- ============================================================
-- 018c_drop_namespace_text.sql — ЗАГОТОВКА: дроп дубликата namespace TEXT
-- ============================================================
-- Дата: 2026-09-17
-- Исполнитель: Нора (db-architect)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§0.2, «дубликат namespace TEXT — дропнуть»)
--
-- Решение (вместо generated column): Postgres не умеет FK-lookup внутри
-- generated column — читаемый строковый namespace даём VIEW'ом v_memories_readable
-- (JOIN namespaces по namespace_id), а не денормализованной колонкой.
-- Канонический источник истины — namespace_id UUID → namespaces.id.
--
-- ⚠ ПРИМЕНИТЬ ПОСЛЕ ПЕРЕВОДА КОДА (namespace TEXT → namespace_id + JOIN/VIEW),
--   НЕ вперёд. Сона переводит queries.py и пересоздаёт хранимки, ссылающиеся
--   на m.namespace TEXT: memory_upsert, memory_insert_batch (ON CONFLICT (namespace,
--   content_hash)), memory_search_hnsw, list_with_count, memory_forget_soft,
--   graph_stats_unified, graph_traverse_full, merge_similar_granules,
--   find_similar_pairs_pgvector; а также INSERT_MEMORY / INSERT_MEMORY_BATCH.
--
-- ⚠ НЕ ЗАПУСКАТЬ АВТОМАТИЧЕСКИ в стартовом батче (017/018/019).
--   Порядок Фазы 0: 017 → 018 → 019 → [перевод кода Соной] → 018b → 018c.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Дроп индексов, ссылающихся на namespace TEXT
--    (часть — уже пересоздана в 018b со status-предикатом; повторно дропаем)
-- ════════════════════════════════════════════════════════════
DROP INDEX IF EXISTS idx_memories_namespace;
DROP INDEX IF EXISTS idx_memories_user_ns_updated;
DROP INDEX IF EXISTS idx_memories_active;
DROP INDEX IF EXISTS idx_memories_graph_stats;

-- ════════════════════════════════════════════════════════════
-- 2. Дроп колонки-дубля namespace TEXT
--    (остаётся канонический namespace_id UUID)
-- ════════════════════════════════════════════════════════════
ALTER TABLE memories DROP COLUMN IF EXISTS namespace;

-- ════════════════════════════════════════════════════════════
-- 3. Индексы на namespace_id (вместо namespace TEXT)
-- ════════════════════════════════════════════════════════════

-- Канонический индекс уже существует: idx_memories_namespace_id (006) — не трогаем.

-- list/stats/forget (ORDER BY updated_at)
CREATE INDEX IF NOT EXISTS idx_memories_user_ns_updated
    ON memories (user_id, namespace_id, updated_at DESC);

-- Активные записи (forget/stats)
CREATE INDEX IF NOT EXISTS idx_memories_active
    ON memories (user_id, namespace_id)
    WHERE status = 'asserted' AND valid_to IS NULL;

-- Уникальный индекс дедупликации уже на (namespace_id, content_hash) — создан
-- миграцией 020. Здесь НЕ пересоздаём (иначе лишний DROP/CREATE и гонка).

-- Покрывающий индекс graph_stats
CREATE INDEX IF NOT EXISTS idx_memories_graph_stats
    ON memories (status, id, namespace_id)
    WHERE status = 'asserted' AND valid_to IS NULL;

-- ════════════════════════════════════════════════════════════
-- 4. VIEW для читаемости: namespace_name TEXT через JOIN namespaces
--    (замена отклонённого generated column)
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE VIEW v_memories_readable AS
SELECT
    m.id,
    m.user_id,
    m.content,
    m.metadata,
    m.namespace_id,
    n.uid  AS namespace_name,           -- читаемый строковый id namespace (бывш. namespace TEXT)
    n.name AS namespace_display_name,
    m.importance,
    m.version,
    m.project_id,
    m.status,
    m.valid_from,
    m.valid_to,
    m.ingested_at,
    m.confidence,
    m.supersedes,
    m.superseded_by,
    m.frozen,
    m.last_accessed_at,
    m.access_count,
    m.content_hash,
    m.source_type,
    m.source_location,
    m.created_at,
    m.updated_at
FROM memories m
LEFT JOIN namespaces n ON n.id = m.namespace_id;

COMMENT ON VIEW v_memories_readable IS 'Читаемое представление memories: namespace_name/display_name через JOIN namespaces (заменяет денормализованную колонку namespace TEXT).';

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции (восстановить namespace TEXT + индексы)
-- ════════════════════════════════════════════════════════════
-- DROP VIEW IF EXISTS v_memories_readable;
--
-- DROP INDEX IF EXISTS idx_memories_graph_stats;
-- DROP INDEX IF EXISTS idx_memories_content_hash_active;
-- DROP INDEX IF EXISTS idx_memories_active;
-- DROP INDEX IF EXISTS idx_memories_user_ns_updated;
--
-- ALTER TABLE memories ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';
--
-- UPDATE memories m
-- SET namespace = n.uid
-- FROM namespaces n
-- WHERE m.namespace_id = n.id
--   AND m.namespace = 'default';
--
-- CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories (namespace);
-- CREATE INDEX IF NOT EXISTS idx_memories_user_ns_updated
--     ON memories (user_id, namespace, updated_at DESC);
-- CREATE INDEX IF NOT EXISTS idx_memories_active
--     ON memories (user_id, namespace) WHERE status = 'asserted' AND valid_to IS NULL;
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash_active
--     ON memories (namespace, content_hash)
--     WHERE content_hash IS NOT NULL AND status = 'asserted' AND valid_to IS NULL;
-- CREATE INDEX IF NOT EXISTS idx_memories_graph_stats
--     ON memories (status, id, namespace) WHERE status = 'asserted' AND valid_to IS NULL;