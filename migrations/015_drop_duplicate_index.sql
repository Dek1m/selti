-- ============================================================
-- 015_drop_duplicate_index.sql
-- ============================================================
-- Удаление дублирующего уникального индекса idx_memories_ns_hash.
--
-- Контекст:
--   - 002_dedup.sql создал idx_memories_content_hash_namespace
--   - 003_athene_memory.sql создал idx_memories_ns_hash
--   - Оба на одних колонках (namespace, content_hash) WHERE content_hash IS NOT NULL
--   - 014 удалил idx_memories_content_hash_namespace и заменил
--     на idx_memories_content_hash_active (с учётом is_archived)
--   - idx_memories_ns_hash остался — дубль, мёртвый вес
-- ============================================================

DROP INDEX IF EXISTS idx_memories_ns_hash;
