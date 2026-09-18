-- ============================================================
-- 018b_drop_is_archived.sql — ЗАГОТОВКА: дроп is_archived
-- ============================================================
-- Дата: 2026-09-17
-- Исполнитель: Нора (db-architect)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§0.2, решение D3: is_archived → status)
--
-- ⚠ ПРИМЕНИТЬ ПОСЛЕ ПЕРЕВОДА КОДА (is_archived → status), НЕ вперёд.
--   Сона переводит все внутренние запросы:
--     is_archived = false → status = 'asserted' AND valid_to IS NULL
--     is_archived = true  → (forget_soft) status = 'retracted', valid_to = now()
--   и пересоздаёт хранимки, ссылающиеся на is_archived:
--     list_with_count, memory_search_hnsw, memory_forget_soft,
--     graph_stats_unified, graph_traverse_full,
--     find_similar_pairs_pgvector, project_context_snapshot (019).
--
-- ⚠ НЕ ЗАПУСКАТЬ АВТОМАТИЧЕСКИ в стартовом батче (017/018/019).
--   Порядок Фазы 0: 017 → 018 → 019 → [перевод кода Соной] → 018b → 018c.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Дроп индексов, ссылающихся на is_archived
-- ════════════════════════════════════════════════════════════
DROP INDEX IF EXISTS idx_memories_active;
DROP INDEX IF EXISTS idx_memories_graph_stats;
DROP INDEX IF EXISTS idx_memories_entity_name;
DROP INDEX IF EXISTS idx_memories_project_status;

-- ════════════════════════════════════════════════════════════
-- 2. Дроп колонки-дубля
-- ════════════════════════════════════════════════════════════
ALTER TABLE memories DROP COLUMN IF EXISTS is_archived;

-- ════════════════════════════════════════════════════════════
-- 3. Пересоздание индексов с status-семантикой
--    («актуально» = status='asserted' AND valid_to IS NULL)
-- ════════════════════════════════════════════════════════════

-- Активные записи (для forget/stats)
CREATE INDEX IF NOT EXISTS idx_memories_active
    ON memories (user_id, namespace)
    WHERE status = 'asserted' AND valid_to IS NULL;

-- Уникальный индекс дедупликации перенесён в 020 (namespace_id + status-семантика).
-- Здесь НЕ пересоздаём: 020 уже владеет idx_memories_content_hash_active.

-- Покрывающий индекс graph_stats
CREATE INDEX IF NOT EXISTS idx_memories_graph_stats
    ON memories (status, id, namespace)
    WHERE status = 'asserted' AND valid_to IS NULL;

-- Expression-index по entity_name
CREATE INDEX IF NOT EXISTS idx_memories_entity_name
    ON memories ((metadata->>'entity_name'))
    WHERE status = 'asserted' AND valid_to IS NULL;

-- Композит «проект + статус»
CREATE INDEX IF NOT EXISTS idx_memories_project_status
    ON memories (project_id, status)
    WHERE status = 'asserted' AND valid_to IS NULL;

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции (восстановить колонку и старые индексы)
-- ════════════════════════════════════════════════════════════
-- DROP INDEX IF EXISTS idx_memories_project_status;
-- DROP INDEX IF EXISTS idx_memories_entity_name;
-- DROP INDEX IF EXISTS idx_memories_graph_stats;
-- DROP INDEX IF EXISTS idx_memories_content_hash_active;
-- DROP INDEX IF EXISTS idx_memories_active;
--
-- ALTER TABLE memories ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT false;
--
-- CREATE INDEX IF NOT EXISTS idx_memories_active
--     ON memories (user_id, namespace) WHERE is_archived = false;
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash_active
--     ON memories (namespace, content_hash)
--     WHERE content_hash IS NOT NULL AND is_archived = false;
-- CREATE INDEX IF NOT EXISTS idx_memories_graph_stats
--     ON memories (is_archived, id, namespace) WHERE is_archived = false;
-- CREATE INDEX IF NOT EXISTS idx_memories_entity_name
--     ON memories ((metadata->>'entity_name')) WHERE is_archived = false;
-- CREATE INDEX IF NOT EXISTS idx_memories_project_status
--     ON memories (project_id, status) WHERE is_archived = false;