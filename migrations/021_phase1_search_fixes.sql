-- ============================================================
-- 021_phase1_search_fixes.sql — ЗАГОТОВКА Фазы 1: фикс graph_traverse_full + GIN FTS
-- ============================================================
-- Дата: 2026-09-18
-- Исполнитель: Нора (db-architect)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§1.1 Hybrid search, решение D5; §1.5 Traverse)
--
-- ⚠ ЗАГОТОВКА Фазы 1 — применяется Рэем в деплой-окне Фазы 1,
--   из автопрогона ДО кода Ф1 НЕ применять. Алфавитно файл идёт
--   после 020 — это корректно; единственное условие: применяется
--   вместе с кодом Фазы 1, который уже ждёт GIN-индекс и рабочую
--   хранимку (TODO(migration 021) в memory_server/db/queries.py).
--
-- Содержимое:
--   1) graph_traverse_full — пересоздание рабочим телом. Тело из 020
--      (020_stored_procedures_canonical.sql §8) битое с момента создания:
--      CTE graph_walk объявлен внутри ПЕРВОГО SELECT ... INTO v_node_ids,
--      а ВТОРОЙ SELECT (сборка нод) ссылается на graph_walk уже вне зоны
--      видимости CTE → asyncpg.exceptions.UndefinedTableError:
--      relation "graph_walk" does not exist при первом же вызове
--      (memory_traverse на проду падает; CREATE проходит, потому что
--      plpgsql не парсит тело при создании).
--      Фикс: ОДИН statement — рекурсивный обход + DISTINCT ON узлов +
--      сборка jsonb в одном запросе. Сигнатура (UUID, INT, TEXT[]) и
--      контракт JSONB (ключи нод/рёбер, колонки nodes/edges) сохранены
--      1:1 с ожиданиями Python-слоя (pg_repository.traverse,
--      queries.py TRAVERSE_FULL).
--   2) GIN-индекс to_tsvector('russian', content) — канал B гибридного
--      поиска (SEARCH_MEMORIES, queries.py:86-99, выражение в запросе).
--   3) ANALYZE memories — свежая статистика после индекса.
--
-- Поглощает раннюю заготовку 021_fts_russian_index.sql (влита сюда
-- 2026-09-18): двух файлов 021_* в каталоге быть не должно — run.py
-- применяет все .sql по алфавиту, дубль номера плодит лишние записи
-- в _migrations.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. graph_traverse_full — рабочее тело (один statement)
-- ════════════════════════════════════════════════════════════
-- Отличия от битой версии 020:
--   * graph_walk живёт в CTE ОДНОГО запроса — оба потребителя
--     (узлы и рёбра) видят его в одной зоне видимости;
--   * DISTINCT ON (node_id) — узел, достижимый несколькими путями
--     (ромб, fan-in), попадает в выдачу ровно один раз, с
--     минимальной глубиной достижимости;
--   * путь обхода отсекается path-guard'ом NOT target = ANY(path) —
--     циклы в графе не раскручивают рекурсию;
--   * LANGUAGE sql вместо plpgsql: тело парсится при CREATE —
--     битые ссылки на несуществующие CTE/таблицы ловятся миграцией,
--     а не первым вызовом с прода.
--
-- Контракт (без изменений, pg_repository.py:451-458):
--   колонки результата: nodes JSONB, edges JSONB;
--   нода:  {id, content (200 симв.), namespace=uid, importance, depth};
--   ребро: {id, source_id, target_id, link_type, description, weight}.

DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[]);

CREATE OR REPLACE FUNCTION graph_traverse_full(
    p_start_id   UUID,
    p_depth      INT     DEFAULT 3,
    p_link_types TEXT[]  DEFAULT NULL
)
RETURNS TABLE(
    nodes JSONB,
    edges JSONB
)
LANGUAGE sql
STABLE
AS $$
WITH RECURSIVE graph_walk AS (
    -- Якорь обхода: стартовая нода
    SELECT
        p_start_id        AS node_id,
        0                 AS depth,
        ARRAY[p_start_id] AS path
    UNION
    -- Шаг обхода: только вперёд по source → target;
    -- depth-cap + path-guard (NOT target = ANY(path)) режут
    -- и глубину, и циклы
    SELECT
        r.target_id,
        gw.depth + 1,
        gw.path || r.target_id
    FROM graph_walk gw
    JOIN relations r ON r.source_id = gw.node_id
    WHERE gw.depth < p_depth
      AND r.target_id IS NOT NULL
      AND NOT r.target_id = ANY(gw.path)
      AND (p_link_types IS NULL OR r.link_type = ANY(p_link_types))
),
uniq_nodes AS (
    -- Узел с несколькими путями достижимости → одна строка
    -- с минимальной глубиной (DISTINCT ON, сортировка внутри
    -- группы по depth ASC)
    SELECT DISTINCT ON (node_id)
        node_id,
        depth
    FROM graph_walk
    ORDER BY node_id, depth
)
SELECT
    -- Ноды: namespace — uid через JOIN namespaces
    (
        SELECT coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'id',         m.id,
                    'content',    left(m.content, 200),
                    'namespace',  n.uid,
                    'importance', m.importance,
                    'depth',      un.depth
                )
                ORDER BY un.depth, m.id
            ),
            '[]'::jsonb
        )
        FROM uniq_nodes un
        JOIN memories m ON m.id = un.node_id
        JOIN namespaces n ON n.id = m.namespace_id
    ) AS nodes,
    -- Рёбра: обе ноды в пределах обхода
    (
        SELECT coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'id',          rel.id,
                    'source_id',   rel.source_id,
                    'target_id',   rel.target_id,
                    'link_type',   rel.link_type,
                    'description', rel.description,
                    'weight',      rel.weight
                )
                ORDER BY rel.id
            ),
            '[]'::jsonb
        )
        FROM relations rel
        WHERE rel.source_id IN (SELECT node_id FROM uniq_nodes)
          AND rel.target_id IS NOT NULL
          AND rel.target_id IN (SELECT node_id FROM uniq_nodes)
    ) AS edges;
$$;

COMMENT ON FUNCTION graph_traverse_full IS 'Обход графа от start_id. Возвращает ноды (namespace=uid) и рёбра одним запросом. Узел с несколькими путями — один раз (минимальная глубина).';

-- ════════════════════════════════════════════════════════════
-- 2. GIN-индекс FTS 'russian'
-- ════════════════════════════════════════════════════════════
-- Канал B гибридного поиска (Фаза 1.1) переведён с 'simple' на
-- 'russian' (стемминг кириллицы — основной корпус памяти). Без GIN
-- каждый FTS-запрос — seq scan с разбором tsvector на лету; на ~14.5К
-- строк терпимо, но растущий корпус делает канал B узким местом.
--
-- Почему индекс полный, а не partial (status='asserted' AND valid_to
-- IS NULL): запрос SEARCH_MEMORIES содержит фильтр актуальности под
-- OR-обёрткой ($6::bool OR (...)) для time-travel (include_historical) —
-- планировщик не доказывает импликацию предиката для partial-индекса и
-- уходит в seq scan. Полный индекс обслуживает оба режима.
--
-- Почему без CONCURRENTLY: migrations/run.py применяет миграции в
-- транзакции, CREATE INDEX CONCURRENTLY вне транзакции невозможен.
-- Таблица ~14.5К строк — обычный CREATE INDEX занимает миллисекунды.

CREATE INDEX IF NOT EXISTS idx_memories_fts_russian
    ON memories USING gin (to_tsvector('russian', content));

COMMENT ON INDEX idx_memories_fts_russian IS 'FTS канал B гибридного поиска (Фаза 1.1): стемминг русского корпуса памяти.';

-- ════════════════════════════════════════════════════════════
-- 3. ANALYZE — свежая статистика после индекса
-- ════════════════════════════════════════════════════════════
-- Допустим внутри транзакции run.py (в отличие от CONCURRENTLY).

ANALYZE memories;

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- ВНИМАНИЕ: восстановление тела из 020 возвращает memory_traverse в
-- падающее состояние (bug 42P01) — делайте только при полном откате
-- Фазы 1, осознанно.
--
-- DROP INDEX IF EXISTS idx_memories_fts_russian;
--
-- -- Прежнее (битое) тело graph_traverse_full из 020 §8, дословно:
-- DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[]);
--
-- CREATE OR REPLACE FUNCTION graph_traverse_full(
--     p_start_id  UUID,
--     p_depth     INT     DEFAULT 3,
--     p_link_types TEXT[] DEFAULT NULL
-- )
-- RETURNS TABLE(
--     nodes JSONB,
--     edges JSONB
-- )
-- LANGUAGE plpgsql
-- AS $$
-- DECLARE
--     v_node_ids UUID[];
--     v_nodes    JSONB;
--     v_edges    JSONB;
-- BEGIN
--     WITH RECURSIVE graph_walk AS (
--         SELECT
--             p_start_id AS node_id,
--             0          AS depth,
--             ARRAY[p_start_id] AS path
--         UNION
--         SELECT
--             r.target_id,
--             gw.depth + 1,
--             gw.path || r.target_id
--         FROM graph_walk gw
--         JOIN relations r ON r.source_id = gw.node_id
--         WHERE gw.depth < p_depth
--           AND r.target_id IS NOT NULL
--           AND NOT r.target_id = ANY(gw.path)
--           AND (p_link_types IS NULL OR r.link_type = ANY(p_link_types))
--     )
--     SELECT array_agg(DISTINCT node_id)
--     INTO v_node_ids
--     FROM graph_walk;
--
--     IF v_node_ids IS NULL THEN
--         nodes := '[]'::jsonb;
--         edges := '[]'::jsonb;
--         RETURN NEXT;
--         RETURN;
--     END IF;
--
--     -- Ноды: namespace — uid через JOIN namespaces
--     SELECT coalesce(jsonb_agg(
--         jsonb_build_object(
--             'id',          m.id,
--             'content',     left(m.content, 200),
--             'namespace',   n.uid,
--             'importance',  m.importance,
--             'depth',       gw.depth
--         )
--     ), '[]'::jsonb)
--     INTO v_nodes
--     FROM graph_walk gw
--     JOIN memories m ON m.id = gw.node_id
--     JOIN namespaces n ON n.id = m.namespace_id;
--
--     -- Рёбра (обе ноды в пределах обхода)
--     SELECT coalesce(jsonb_agg(
--         jsonb_build_object(
--             'id',          rel.id,
--             'source_id',   rel.source_id,
--             'target_id',   rel.target_id,
--             'link_type',   rel.link_type,
--             'description', rel.description,
--             'weight',      rel.weight
--         )
--     ), '[]'::jsonb)
--     INTO v_edges
--     FROM relations rel
--     WHERE rel.source_id = ANY(v_node_ids)
--       AND rel.target_id IS NOT NULL
--       AND rel.target_id = ANY(v_node_ids);
--
--     nodes := v_nodes;
--     edges := v_edges;
--     RETURN NEXT;
-- END;
-- $$;
