-- ============================================================
-- 020_stored_procedures_canonical.sql — хранимки под каноническую схему
-- ============================================================
-- Дата: 2026-09-17
-- Исполнитель: Нора (db-architect)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§0.2/§0.3, решения D3/D9)
--
-- Задача: привести ВСЕ хранимки к канонической модели memories
-- (финальное состояние после 018b/018c):
--   * Актуальность гранулы = status='asserted' AND valid_to IS NULL
--     (is_archived больше НЕ используется — дроп 018b).
--   * namespace — ТОЛЬКО namespace_id UUID + JOIN namespaces (uid для
--     читаемого внешнего контракта); m.namespace TEXT дропается 018c.
--   * pgvector выпилен (миграция 011): dense-поиск по embedding ушёл в
--     Qdrant, колонки embedding и типа vector в PG больше нет.
--
-- Как применяется: в СТАРТОВОМ батче Фазы 0 (017→018→019→020), ДО
-- дропа is_archived/namespace (018b/018c). Хранимки здесь написаны так,
-- что НЕ ссылаются на дропаемые колонки — после 018b/018c они остаются
-- валидными без изменений.
--
-- Судьба старых хранимок:
--   * memory_upsert (009, vector-версия)   — удалена миграцией 011.
--     Пересоздаём БЕЗ embedding (чистый текстовый upsert).
--   * memory_insert_batch (009, vector)     — пересоздаём БЕЗ embeddings.
--   * memory_search_hnsw (009, dense)       — удалена 011, НЕ воссоздаём:
--     dense-поиск живёт в Qdrant; PG-путь — FTS (SEARCH_MEMORIES, queries.py).
--   * search_memories_approx (003, dense)   — удалена 011, НЕ воссоздаём.
--   * find_similar_pairs_pgvector (016)     — МЁРТВА (pgvector fallback,
--     колонки embedding нет) → DROP здесь.
--   * get_relations_unified (014)           — не ссылается на дропаемое,
--     не трогаем.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Мёртвые pgvector-хранимки — чистка
-- ════════════════════════════════════════════════════════════
-- find_similar_pairs_pgvector — fallback на pgvector-колонку embedding,
-- которой нет с 011. ANN-поиск кандидатов для merge_similar_granules
-- даёт Qdrant (scripts/merge_similar_granules.py). Функция всегда
-- возвращала 0 (guard на information_schema.columns). Удаляем.
DROP FUNCTION IF EXISTS find_similar_pairs_pgvector(FLOAT, INT, INT);

-- Примечание: memory_search_hnsw / search_memories_approx были дропнуты
-- миграцией 011 (DROP EXTENSION vector забирает оператор <=>). Их dense-
-- семантику заменяет Qdrant + FTS. НЕ воссоздаём.

-- ════════════════════════════════════════════════════════════
-- 2. Канонический уникальный индекс дедупликации
-- ════════════════════════════════════════════════════════════
-- Переводим дедуп с (namespace, content_hash) на (namespace_id, content_hash)
-- и с is_archived на status-семантику. Единая точка истины для:
--   * ON CONFLICT (namespace_id, content_hash) в memory_upsert/batch;
--   * exact-dedup при store (SELECT_MEMORY_BY_CONTENT_HASH, queries.py).
DROP INDEX IF EXISTS idx_memories_content_hash_active;

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash_active
    ON memories (namespace_id, content_hash)
    WHERE content_hash IS NOT NULL
      AND status = 'asserted'
      AND valid_to IS NULL;

-- ════════════════════════════════════════════════════════════
-- 3. memory_upsert — текстовый upsert (без embedding)
-- ════════════════════════════════════════════════════════════
-- Контракт: дедуп по (namespace_id, content_hash) с partial-индексом.
-- Новые колонки (status/valid_from/ingested_at/confidence/frozen) —
-- покрываются DEFAULT'ами БД (как INSERT_MEMORY в queries.py).
-- metadata — shallow-merge поверх существующего.

-- Старая vector-версия memory_upsert(TEXT, TEXT, vector, UUID, JSONB, TEXT, TEXT, INT)
-- уже удалена миграцией 011 (DROP FUNCTION ... vector ...). Тип vector в БД больше
-- не существует, поэтому DROP с vector-сигнатурой здесь НЕ вызываем — только
-- CREATE OR REPLACE новой бессортовой сигнатуры ниже.

CREATE OR REPLACE FUNCTION memory_upsert(
    p_user_id         TEXT,
    p_content         TEXT,
    p_namespace_id    UUID,
    p_metadata        JSONB DEFAULT '{}'::jsonb,
    p_content_hash    TEXT  DEFAULT NULL,
    p_importance      INT   DEFAULT 3,
    p_project_id      UUID  DEFAULT NULL,
    p_source_type     TEXT  DEFAULT NULL,
    p_source_location TEXT  DEFAULT NULL
)
RETURNS TABLE(id UUID, action TEXT)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    INSERT INTO memories AS m (
        user_id, content, namespace_id, metadata, content_hash,
        importance, project_id, source_type, source_location
    ) VALUES (
        p_user_id, p_content, p_namespace_id, p_metadata, p_content_hash,
        p_importance, p_project_id, p_source_type, p_source_location
    )
    ON CONFLICT (namespace_id, content_hash)
        WHERE content_hash IS NOT NULL
          AND status = 'asserted' AND valid_to IS NULL
    DO UPDATE SET
        content    = EXCLUDED.content,             -- триггер 018 инкрементит version
        metadata   = m.metadata || EXCLUDED.metadata,
        importance = EXCLUDED.importance,
        project_id = COALESCE(EXCLUDED.project_id, m.project_id)
    RETURNING
        m.id,
        CASE WHEN xmax = 0 THEN 'inserted'::text ELSE 'updated'::text END;
END;
$$;

COMMENT ON FUNCTION memory_upsert IS 'Канонический upsert гранулы (без embedding). Дедуп по (namespace_id, content_hash). Возвращает id и action.';

-- ════════════════════════════════════════════════════════════
-- 4. memory_insert_batch — batch insert (без embeddings)
-- ════════════════════════════════════════════════════════════
-- Тот же контракт дедупа. Дубли молча пропускаются, возвращаются только
-- id вставленных (порядок — как во входных массивах для уникальных).

DROP FUNCTION IF EXISTS memory_insert_batch(TEXT[], TEXT[], TEXT[], JSONB[], TEXT[], UUID[], TEXT[], INT[]);

CREATE OR REPLACE FUNCTION memory_insert_batch(
    p_user_ids       TEXT[],
    p_contents       TEXT[],
    p_namespace_ids  UUID[],
    p_metadatas      JSONB[],
    p_content_hashes TEXT[],
    p_importances    INT[]
)
RETURNS TABLE(id UUID)
LANGUAGE sql
AS $$
    INSERT INTO memories (
        user_id, content, namespace_id, metadata, content_hash, importance
    )
    SELECT
        unnest(p_user_ids),
        unnest(p_contents),
        unnest(p_namespace_ids),
        unnest(p_metadatas),
        unnest(p_content_hashes),
        unnest(p_importances)
    ON CONFLICT (namespace_id, content_hash)
        WHERE content_hash IS NOT NULL
          AND status = 'asserted' AND valid_to IS NULL
    DO NOTHING
    RETURNING memories.id;
$$;

COMMENT ON FUNCTION memory_insert_batch IS 'Batch insert с exact dedup по (namespace_id, content_hash). Пропускает дубли. Возвращает id вставленных записей.';

-- ════════════════════════════════════════════════════════════
-- 5. list_with_count — список с общим счётчиком (status + namespace_id)
-- ════════════════════════════════════════════════════════════
-- Внешний контракт namespace остаётся TEXT (uid): резолвим через JOIN
-- namespaces по UNIQUE-индексу idx_namespaces_uid, без round-trip в Python.

DROP FUNCTION IF EXISTS list_with_count(TEXT, TEXT, INT, INT);

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
        m.id, m.user_id, m.content, m.metadata, n.uid AS namespace,
        m.importance, m.created_at, m.updated_at, m.content_hash,
        COUNT(*) OVER() AS total_count
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE (p_user_id   IS NULL OR m.user_id   = p_user_id)
      AND (p_namespace IS NULL OR n.uid       = p_namespace)
      AND m.status = 'asserted' AND m.valid_to IS NULL
    ORDER BY m.created_at DESC
    LIMIT p_limit OFFSET p_offset;
$$;

COMMENT ON FUNCTION list_with_count IS 'Список memories с общим счётчиком (COUNT(*) OVER). Фильтр актуальности: status=asserted AND valid_to IS NULL. namespace — uid через JOIN.';

-- ════════════════════════════════════════════════════════════
-- 6. memory_forget_soft — мягкое забвение (status + valid_to)
-- ════════════════════════════════════════════════════════════
-- Закрытие окна валидности вместо флага is_archived.

DROP FUNCTION IF EXISTS memory_forget_soft(TEXT, TEXT);

CREATE OR REPLACE FUNCTION memory_forget_soft(
    p_user_id   TEXT,
    p_namespace TEXT DEFAULT NULL
)
RETURNS BIGINT
LANGUAGE sql
AS $$
    WITH updated AS (
        UPDATE memories m
        SET status = 'retracted', valid_to = now(), updated_at = now()
        FROM namespaces n
        WHERE n.id = m.namespace_id
          AND m.user_id = p_user_id
          AND m.status = 'asserted' AND m.valid_to IS NULL
          AND (p_namespace IS NULL OR n.uid = p_namespace)
        RETURNING m.id
    )
    SELECT count(*)::bigint FROM updated;
$$;

COMMENT ON FUNCTION memory_forget_soft IS 'Мягкое забвение: status=retracted, valid_to=now(). Возвращает количество обновлённых записей.';

-- ════════════════════════════════════════════════════════════
-- 7. graph_stats_unified — статистика графа (status + namespace_id)
-- ════════════════════════════════════════════════════════════
DROP FUNCTION IF EXISTS graph_stats_unified();

CREATE OR REPLACE FUNCTION graph_stats_unified(
    OUT p_total_granules  INT,
    OUT p_total_relations INT,
    OUT p_linked_granules INT,
    OUT p_orphans         INT,
    OUT p_by_namespace    JSONB,
    OUT p_by_link_type    JSONB
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_linked_ids UUID[];
BEGIN
    SELECT array_agg(DISTINCT id)
    INTO v_linked_ids
    FROM (
        SELECT source_id AS id FROM relations
        UNION
        SELECT target_id AS id FROM relations WHERE target_id IS NOT NULL
    ) sub;

    IF v_linked_ids IS NULL THEN
        v_linked_ids := ARRAY[]::UUID[];
    END IF;

    -- Общая статистика (только актуальные гранулы)
    SELECT
        count(*) FILTER (WHERE status = 'asserted' AND valid_to IS NULL),
        (SELECT count(*) FROM relations),
        count(*) FILTER (WHERE status = 'asserted' AND valid_to IS NULL AND id = ANY(v_linked_ids)),
        count(*) FILTER (WHERE status = 'asserted' AND valid_to IS NULL AND NOT id = ANY(v_linked_ids))
    INTO p_total_granules, p_total_relations, p_linked_granules, p_orphans
    FROM memories;

    -- Статистика по namespace (uid через JOIN)
    SELECT coalesce(jsonb_object_agg(
        ns_uid,
        jsonb_build_object('total', total, 'linked', linked, 'orphans', orphans)
    ), '{}'::jsonb)
    INTO p_by_namespace
    FROM (
        SELECT
            n.uid AS ns_uid,
            count(*) AS total,
            count(*) FILTER (WHERE m.id = ANY(v_linked_ids)) AS linked,
            count(*) FILTER (WHERE NOT m.id = ANY(v_linked_ids)) AS orphans
        FROM memories m
        JOIN namespaces n ON n.id = m.namespace_id
        WHERE m.status = 'asserted' AND m.valid_to IS NULL
        GROUP BY n.uid
    ) ns;

    -- Статистика по link_type
    SELECT coalesce(jsonb_object_agg(link_type, cnt), '{}'::jsonb)
    INTO p_by_link_type
    FROM (
        SELECT link_type, count(*) AS cnt
        FROM relations
        GROUP BY link_type
    ) lt;
END;
$$;

COMMENT ON FUNCTION graph_stats_unified IS 'Статистика графа знаний (актуальные гранулы): общая + по namespace + по link_type.';

-- ════════════════════════════════════════════════════════════
-- 8. graph_traverse_full — обход графа с полным возвратом нод/рёбер
-- ════════════════════════════════════════════════════════════
DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[]);

CREATE OR REPLACE FUNCTION graph_traverse_full(
    p_start_id  UUID,
    p_depth     INT     DEFAULT 3,
    p_link_types TEXT[] DEFAULT NULL
)
RETURNS TABLE(
    nodes JSONB,
    edges JSONB
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_node_ids UUID[];
    v_nodes    JSONB;
    v_edges    JSONB;
BEGIN
    WITH RECURSIVE graph_walk AS (
        SELECT
            p_start_id AS node_id,
            0          AS depth,
            ARRAY[p_start_id] AS path
        UNION
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
    )
    SELECT array_agg(DISTINCT node_id)
    INTO v_node_ids
    FROM graph_walk;

    IF v_node_ids IS NULL THEN
        nodes := '[]'::jsonb;
        edges := '[]'::jsonb;
        RETURN NEXT;
        RETURN;
    END IF;

    -- Ноды: namespace — uid через JOIN namespaces
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',          m.id,
            'content',     left(m.content, 200),
            'namespace',   n.uid,
            'importance',  m.importance,
            'depth',       gw.depth
        )
    ), '[]'::jsonb)
    INTO v_nodes
    FROM graph_walk gw
    JOIN memories m ON m.id = gw.node_id
    JOIN namespaces n ON n.id = m.namespace_id;

    -- Рёбра (обе ноды в пределах обхода)
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',          rel.id,
            'source_id',   rel.source_id,
            'target_id',   rel.target_id,
            'link_type',   rel.link_type,
            'description', rel.description,
            'weight',      rel.weight
        )
    ), '[]'::jsonb)
    INTO v_edges
    FROM relations rel
    WHERE rel.source_id = ANY(v_node_ids)
      AND rel.target_id IS NOT NULL
      AND rel.target_id = ANY(v_node_ids);

    nodes := v_nodes;
    edges := v_edges;
    RETURN NEXT;
END;
$$;

COMMENT ON FUNCTION graph_traverse_full IS 'Обход графа от start_id. Возвращает ноды (namespace=uid) и рёбра одним запросом.';

-- ════════════════════════════════════════════════════════════
-- 9. merge_similar_granules — кластеризация (namespace через JOIN)
-- ════════════════════════════════════════════════════════════
-- Пары сходства грузит scripts/merge_similar_granules.py из Qdrant.
-- Здесь только точечные правки: m.namespace → JOIN namespaces (uid).

CREATE OR REPLACE FUNCTION merge_similar_granules(
    p_threshold FLOAT DEFAULT 0.9,
    p_max_groups INT DEFAULT 50
)
RETURNS JSON
LANGUAGE plpgsql
AS $$
DECLARE
    result JSON;
    v_total_pairs INT;
    v_total_groups INT;
    v_total_granules INT;
    v_start_time TIMESTAMPTZ := clock_timestamp();
BEGIN
    SELECT COUNT(*) INTO v_total_pairs FROM _similarity_pairs;

    IF v_total_pairs = 0 THEN
        RETURN json_build_object(
            'version', 2,
            'total_groups', 0,
            'total_granules', 0,
            'total_pairs', 0,
            'elapsed_ms', 0,
            'groups', '[]'::json
        );
    END IF;

    WITH RECURSIVE
    all_nodes AS (
        SELECT source_id AS node_id FROM _similarity_pairs
        UNION
        SELECT target_id FROM _similarity_pairs
    ),
    all_edges AS (
        SELECT source_id AS from_node, target_id AS to_node, similarity
        FROM _similarity_pairs
        UNION
        SELECT target_id, source_id, similarity
        FROM _similarity_pairs
    ),
    components AS (
        SELECT
            node_id,
            node_id AS component_root,
            ARRAY[node_id] AS visited,
            0 AS depth
        FROM all_nodes

        UNION ALL

        SELECT
            e.to_node,
            c.component_root,
            c.visited || e.to_node,
            c.depth + 1
        FROM components c
        JOIN all_edges e ON e.from_node = c.node_id
        WHERE e.to_node <> ALL(c.visited)
          AND c.depth < 10
    ),
    component_roots AS (
        SELECT
            node_id,
            (array_agg(component_root ORDER BY component_root::text))[1] AS group_id
        FROM components
        GROUP BY node_id
    ),
    group_stats AS (
        SELECT
            cr.group_id,
            COUNT(DISTINCT cr.node_id) AS member_count,
            AVG(sp.similarity) AS avg_similarity
        FROM component_roots cr
        JOIN _similarity_pairs sp
            ON sp.source_id = cr.node_id OR sp.target_id = cr.node_id
        GROUP BY cr.group_id
        HAVING COUNT(DISTINCT cr.node_id) >= 2
        ORDER BY avg_similarity DESC
        LIMIT p_max_groups
    ),
    group_cores AS (
        SELECT DISTINCT ON (gs.group_id)
            gs.group_id,
            gs.member_count,
            gs.avg_similarity,
            m.id AS core_id,
            m.content AS core_content,
            m.importance AS core_importance,
            nm.uid AS core_namespace
        FROM group_stats gs
        JOIN component_roots cr ON cr.group_id = gs.group_id
        JOIN memories m ON m.id = cr.node_id
        JOIN namespaces nm ON nm.id = m.namespace_id
        ORDER BY gs.group_id, m.importance DESC, m.created_at DESC
    ),
    group_members AS (
        SELECT
            cr.group_id,
            json_agg(
                json_build_object(
                    'id', m.id::text,
                    'content', LEFT(m.content, 300),
                    'importance', m.importance,
                    'namespace', n.uid,
                    'is_core', (m.id = gc.core_id),
                    'project_id', COALESCE(m.project_id::text, '')
                )
                ORDER BY m.importance DESC, m.id
            ) AS members_json
        FROM component_roots cr
        JOIN group_stats gs ON gs.group_id = cr.group_id
        JOIN memories m ON m.id = cr.node_id
        JOIN namespaces n ON n.id = m.namespace_id
        JOIN group_cores gc ON gc.group_id = cr.group_id
        GROUP BY cr.group_id, gc.core_id
    )
    SELECT json_build_object(
        'version', 2,
        'total_groups', (SELECT COUNT(*) FROM group_stats),
        'total_granules', (SELECT SUM(member_count) FROM group_stats),
        'total_pairs', v_total_pairs,
        'elapsed_ms', EXTRACT(MILLISECONDS FROM clock_timestamp() - v_start_time),
        'groups', (
            SELECT json_agg(
                json_build_object(
                    'group_id', gc.group_id,
                    'core_id', gc.core_id::text,
                    'core_content', LEFT(gc.core_content, 300),
                    'core_importance', gc.core_importance,
                    'similarity_score', ROUND(gc.avg_similarity::numeric, 4),
                    'member_count', gc.member_count,
                    'recommended_action', 'merge',
                    'members', gm.members_json
                )
            )
            FROM group_cores gc
            JOIN group_members gm ON gm.group_id = gc.group_id
        )
    )
    INTO result;

    RETURN result;
END;
$$;

COMMENT ON FUNCTION merge_similar_granules IS 'Кластеризация похожих гранул (пары из Qdrant загружаются в _similarity_pairs). namespace — uid через JOIN.';

-- ════════════════════════════════════════════════════════════
-- 10. project_context_snapshot — пересоздание БЕЗ is_archived
-- ════════════════════════════════════════════════════════════
-- (019 создала с is_archived=false; здесь — status + valid_to, финальное.)

DROP FUNCTION IF EXISTS project_context_snapshot(UUID, INT);

CREATE OR REPLACE FUNCTION project_context_snapshot(
    p_project_id   UUID,
    p_limit_per_ns INT DEFAULT 15
)
RETURNS TABLE(
    content    TEXT,
    namespace  TEXT,
    importance INT,
    updated_at TIMESTAMPTZ
)
LANGUAGE sql
STABLE
AS $$
    WITH ranked AS (
        SELECT
            m.content,
            n.uid AS namespace,
            m.importance,
            m.updated_at,
            row_number() OVER (
                PARTITION BY m.namespace_id
                ORDER BY m.importance DESC, m.updated_at DESC
            ) AS rn
        FROM memories m
        JOIN namespaces n ON n.id = m.namespace_id
        WHERE m.project_id = p_project_id
          AND m.status = 'asserted'
          AND m.valid_to IS NULL
    )
    SELECT content, namespace, importance, updated_at
    FROM ranked
    WHERE rn <= LEAST(
        p_limit_per_ns,
        CASE namespace
            WHEN 'project_meta'      THEN 10
            WHEN 'code_knowledge'    THEN 15
            WHEN 'dialogue_insights' THEN 5
            WHEN 'infrastructure'    THEN 5
            ELSE 5
        END
    )
    ORDER BY importance DESC, updated_at DESC;
$$;

COMMENT ON FUNCTION project_context_snapshot(UUID, INT) IS 'Топ-гранулы проекта (квоты per namespace). Актуальность: status=asserted AND valid_to IS NULL.';

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- DROP FUNCTION IF EXISTS project_context_snapshot(UUID, INT);
-- DROP FUNCTION IF EXISTS merge_similar_granules(FLOAT, INT);
-- DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[]);
-- DROP FUNCTION IF EXISTS graph_stats_unified();
-- DROP FUNCTION IF EXISTS memory_forget_soft(TEXT, TEXT);
-- DROP FUNCTION IF EXISTS list_with_count(TEXT, TEXT, INT, INT);
-- DROP FUNCTION IF EXISTS memory_insert_batch(TEXT[], TEXT[], UUID[], JSONB[], TEXT[], INT[]);
-- DROP FUNCTION IF EXISTS memory_upsert(TEXT, TEXT, UUID, JSONB, TEXT, INT, UUID, TEXT, TEXT, TEXT);
--
-- -- Восстановить дедуп-индекс на (namespace, content_hash) с is_archived
-- -- (состояние «после 014, до Фазы 0»):
-- DROP INDEX IF EXISTS idx_memories_content_hash_active;
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash_active
--     ON memories (namespace, content_hash)
--     WHERE content_hash IS NOT NULL AND is_archived = false;