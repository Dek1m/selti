-- ============================================================
-- 014_stored_procedures_optimizations.sql
-- ============================================================
-- Три оптимизирующие хранимки:
--   1) get_relations_unified  — UNION ALL вместо 2 запросов
--   2) list_with_count        — COUNT(*) OVER() вместо 2 запросов
--   3) memory_forget_soft     — soft delete вместо hard DELETE
--
-- + Исправление partial unique index для soft delete
-- ============================================================


-- ════════════════════════════════════════════════════════════
-- 1. get_relations_unified — все связи гранулы одним запросом
-- ════════════════════════════════════════════════════════════
-- Заменяет паттерн: get_relations_by_source() + get_relations_by_target()
-- Один round-trip вместо двух.
--
-- Возвращает объединённый набор outgoing + incoming связей
-- с колонкой direction ('outgoing' / 'incoming').
--
-- Rationale:
--   UNION ALL (не UNION) — нет дубликатов, т.к. outgoing и incoming
--   по определению не пересекаются для одного source_id/target_id.
--   UNION ALL дешевле: без сортировки и дедупликации на уровне БД.
--
-- LANGUAGE sql — простой двухтабличный запрос, без условной логики.
-- STABLE — не меняет данные, детерминированна для одного входа.
-- ════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION get_relations_unified(
    p_memory_id UUID,
    p_link_type  TEXT DEFAULT NULL
)
RETURNS TABLE(
    id          UUID,
    source_id   UUID,
    target_id   UUID,
    target_name TEXT,
    link_type   TEXT,
    description TEXT,
    weight      FLOAT,
    metadata    JSONB,
    created_at  TIMESTAMPTZ,
    direction   TEXT
)
LANGUAGE sql
STABLE
AS $$
    -- Исходящие связи: source = p_memory_id
    SELECT
        r.id, r.source_id, r.target_id, r.target_name,
        r.link_type, r.description, r.weight, r.metadata,
        r.created_at,
        'outgoing'::text AS direction
    FROM relations r
    WHERE r.source_id = p_memory_id
      AND (p_link_type IS NULL OR r.link_type = p_link_type)

    UNION ALL

    -- Входящие связи: target = p_memory_id
    SELECT
        r.id, r.source_id, r.target_id, r.target_name,
        r.link_type, r.description, r.weight, r.metadata,
        r.created_at,
        'incoming'::text AS direction
    FROM relations r
    WHERE r.target_id = p_memory_id
      AND (p_link_type IS NULL OR r.link_type = p_link_type)

    ORDER BY created_at DESC;
$$;

COMMENT ON FUNCTION get_relations_unified IS 'Все связи гранулы (incoming + outgoing) одним запросом через UNION ALL. Заменяет два отдельных SELECT.';


-- ════════════════════════════════════════════════════════════
-- 2. list_with_count — список с общим счётчиком одним запросом
-- ════════════════════════════════════════════════════════════
-- Заменяет паттерн: LIST_MEMORIES + COUNT_MEMORIES
-- Один round-trip вместо двух.
--
-- COUNT(*) OVER() — window function, считает общее кол-во строк
-- без дополнительного запроса. PostgreSQL вычисляет window function
-- после WHERE, но до LIMIT — это стандартное поведение.
--
-- Rationale:
--   Window function работает на уровне планировщика: COUNT считается
--   по всем строкам, удовлетворяющим WHERE, а LIMIT обрезает только
--   результат. Это ровно то же самое, что отдельный COUNT(*), но
--   без дополнительного round-trip.
--
-- LANGUAGE sql — простой запрос.
-- STABLE — не меняет данные.
-- ════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION list_with_count(
    p_user_id   TEXT DEFAULT NULL,
    p_namespace TEXT DEFAULT NULL,
    p_limit     INT  DEFAULT 50,
    p_offset    INT  DEFAULT 0
)
RETURNS TABLE(
    id           UUID,
    user_id      TEXT,
    content      TEXT,
    metadata     JSONB,
    namespace    TEXT,
    importance   INT,
    created_at   TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ,
    content_hash TEXT,
    total_count  BIGINT
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        m.id, m.user_id, m.content, m.metadata,
        m.namespace, m.importance, m.created_at, m.updated_at,
        m.content_hash,
        COUNT(*) OVER() AS total_count
    FROM memories m
    WHERE (p_user_id   IS NULL OR m.user_id   = p_user_id)
      AND (p_namespace IS NULL OR m.namespace = p_namespace)
      AND m.is_archived = false
    ORDER BY m.created_at DESC
    LIMIT p_limit OFFSET p_offset;
$$;

COMMENT ON FUNCTION list_with_count IS 'Список memories с общим счётчиком через COUNT(*) OVER(). Один запрос вместо SELECT + COUNT.';


-- ════════════════════════════════════════════════════════════
-- 3. memory_forget_soft — мягкое удаление вместо hard DELETE
-- ════════════════════════════════════════════════════════════
-- Заменяет FORGET_MEMORIES (DELETE FROM memories).
-- Вместо удаления — установка is_archived = true.
--
-- Преимущества:
--   - Данные не теряются навсегда (можно восстановить)
--   - Сохраняется целостность графа (relations ссылается на memories)
--   - Обратимость: UPDATE SET is_archived = false
--
-- LANGUAGE sql — простой UPDATE.
-- Не STABLE/IMMUTABLE — меняет данные (UPDATE).
-- ════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION memory_forget_soft(
    p_user_id   TEXT,
    p_namespace TEXT DEFAULT NULL
)
RETURNS BIGINT
LANGUAGE sql
AS $$
    WITH updated AS (
        UPDATE memories
        SET is_archived = true
        WHERE user_id = p_user_id
          AND is_archived = false
          AND (p_namespace IS NULL OR namespace = p_namespace)
        RETURNING id
    )
    SELECT count(*)::bigint FROM updated;
$$;

COMMENT ON FUNCTION memory_forget_soft IS 'Мягкое удаление: UPDATE SET is_archived = true. Возвращает количество обновлённых записей.';


-- ════════════════════════════════════════════════════════════
-- 4. Исправление partial unique index для soft delete
-- ════════════════════════════════════════════════════════════
-- Проблема: текущий индекс idx_memories_content_hash_namespace
-- не учитывает is_archived. Если запись soft-deleted (is_archived = true),
-- её content_hash всё ещё блокирует вставку нового record
-- с тем же namespace + content_hash.
--
-- Решение: пересоздать индекс с условием is_archived = false.
-- Это позволяет "переродиться" — после soft delete можно вставить
-- новый record с тем же content_hash.
-- ════════════════════════════════════════════════════════════

DROP INDEX IF EXISTS idx_memories_content_hash_namespace;

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash_active
    ON memories (namespace, content_hash)
    WHERE content_hash IS NOT NULL AND is_archived = false;


-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- DROP FUNCTION IF EXISTS get_relations_unified(UUID, TEXT);
-- DROP FUNCTION IF EXISTS list_with_count(TEXT, TEXT, INT, INT);
-- DROP FUNCTION IF EXISTS memory_forget_soft(TEXT, TEXT);
-- DROP INDEX IF EXISTS idx_memories_content_hash_active;
-- -- Восстановить оригинальный индекс:
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash_namespace
--     ON memories (namespace, content_hash)
--     WHERE content_hash IS NOT NULL;
