-- ============================================================
-- 009_stored_procedures.sql
-- ============================================================
-- PL/pgSQL хранимки для selti.
-- Переносят бизнес-логику из Python в БД:
--   1) memory_upsert        — upsert с возвратом id
--   2) memory_insert_batch  — batch insert с exact dedup (content_hash)
--   3) memory_search_hnsw   — semantic search, совместимый с HNSW
--   4) graph_stats_unified  — статистика графа одним запросом
--   5) graph_traverse_full  — обход графа с возвратом нод и рёбер
-- ============================================================


-- ════════════════════════════════════════════════════════════
-- 1. UPSERT с возвратом id
-- ════════════════════════════════════════════════════════════
-- Если content_hash уже есть в namespace — обновляет content, embedding, metadata, importance.
-- Если нет — вставляет новую запись.
-- Возвращает: id, action ('inserted' / 'updated')
-- ════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION memory_upsert(
    p_user_id       TEXT,
    p_content       TEXT,
    p_embedding     vector(4096),
    p_namespace_id  UUID,
    p_metadata      JSONB   DEFAULT '{}'::jsonb,
    p_namespace     TEXT    DEFAULT 'default',
    p_content_hash  TEXT    DEFAULT NULL,
    p_importance    INT     DEFAULT 3
)
RETURNS TABLE(id UUID, action TEXT)
LANGUAGE plpgsql
AS $$
BEGIN
    -- Пытаемся найти существующую запись по content_hash
    RETURN QUERY
    INSERT INTO memories AS m (
        user_id, content, embedding, metadata, namespace, namespace_id, content_hash, importance
    ) VALUES (
        p_user_id, p_content, p_embedding, p_metadata, p_namespace, p_namespace_id, p_content_hash, p_importance
    )
    ON CONFLICT (namespace, content_hash)
        WHERE content_hash IS NOT NULL
    DO UPDATE SET
        content    = EXCLUDED.content,
        embedding  = EXCLUDED.embedding,
        metadata   = m.metadata || EXCLUDED.metadata,  -- merge metadata
        importance = EXCLUDED.importance
    RETURNING
        m.id,
        CASE
            WHEN xmax = 0 THEN 'inserted'::text
            ELSE 'updated'::text
        END;
END;
$$;

COMMENT ON FUNCTION memory_upsert IS 'Upsert памяти: INSERT или UPDATE при дубле по (namespace, content_hash). Возвращает id и action.';


-- ════════════════════════════════════════════════════════════
-- 2. Batch insert с exact dedup (content_hash)
-- ════════════════════════════════════════════════════════════
-- Принимает массивы параллельных данных.
-- ON CONFLICT DO NOTHING — дубликаты молча пропускаются.
-- Возвращает: массив id вставленных записей (порядок = порядок входных данных для уникальных).
--
-- Преимущество перед текущим INSERT_MEMORY_BATCH:
--   - Дедуп на уровне БД (без Python-раундтрипа)
--   - Атомарность: либо все, либо ничего
--   - Возвращает только вставленные id (не дубли)
-- ════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION memory_insert_batch(
    p_user_ids      TEXT[],
    p_contents      TEXT[],
    p_embeddings    TEXT[],       -- text[] cast to vector в SQL
    p_metadatas     JSONB[],
    p_namespaces    TEXT[],
    p_namespace_ids UUID[],
    p_content_hashes TEXT[],
    p_importances   INT[]
)
RETURNS TABLE(id UUID)
LANGUAGE sql
AS $$
    INSERT INTO memories (
        user_id, content, embedding, metadata, namespace, namespace_id, content_hash, importance
    )
    SELECT
        unnest(p_user_ids),
        unnest(p_contents),
        unnest(p_embeddings)::vector,
        unnest(p_metadatas),
        unnest(p_namespaces),
        unnest(p_namespace_ids),
        unnest(p_content_hashes),
        unnest(p_importances)
    ON CONFLICT (namespace, content_hash)
        WHERE content_hash IS NOT NULL
    DO NOTHING
    RETURNING memories.id;
$$;

COMMENT ON FUNCTION memory_insert_batch IS 'Batch insert с exact dedup. Пропускает дубли по (namespace, content_hash). Возвращает id вставленных записей.';


-- ════════════════════════════════════════════════════════════
-- 3. Semantic search (совместимый с HNSW)
-- ════════════════════════════════════════════════════════════
-- Использует <=> (cosine distance) оператор pgvector.
-- Подготовлен к HNSW индексу: ORDER BY embedding <=> $1
-- автоматически использует индекс если он существует.
--
-- Текущий SEARCH_MEMORIES делает то же самое, но как SQL-строка.
-- Эта функция — обёртка с типизацией и возможностью расширения.
--
-- При создании HNSW индекса:
--   CREATE INDEX idx_memories_embedding_hnsw
--       ON memories USING hnsw (embedding vector_cosine_ops)
--       WITH (m = 16, ef_construction = 64);
--
-- ef_search настраивается на пуле:
--   SET LOCAL hnsw.ef_search = 64;
-- ════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION memory_search_hnsw(
    p_query_embedding vector(4096),
    p_user_id         TEXT    DEFAULT NULL,
    p_namespace       TEXT    DEFAULT NULL,
    p_threshold       FLOAT   DEFAULT 0.7,
    p_limit           INT     DEFAULT 10
)
RETURNS TABLE(
    id         UUID,
    content    TEXT,
    metadata   JSONB,
    importance INT,
    score      FLOAT
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        m.id,
        m.content,
        m.metadata,
        m.importance,
        1 - (m.embedding <=> p_query_embedding) AS score
    FROM memories m
    WHERE (p_user_id   IS NULL OR m.user_id   = p_user_id)
      AND (p_namespace IS NULL OR m.namespace = p_namespace)
      AND m.is_archived = false
      AND 1 - (m.embedding <=> p_query_embedding) >= p_threshold
    ORDER BY m.embedding <=> p_query_embedding
    LIMIT p_limit;
$$;

COMMENT ON FUNCTION memory_search_hnsw IS 'Semantic search по cosine similarity. Автоматически использует HNSW индекс если он создан.';


-- ════════════════════════════════════════════════════════════
-- 4. Статистика графа — один запрос вместо трёх
-- ════════════════════════════════════════════════════════════
-- Объединяет GRAPH_STATS + GRAPH_STATS_BY_NAMESPACE + GRAPH_STATS_BY_LINK_TYPE.
-- Возвращает три результата через PG cursors / OUT параметры.
--
-- Формат:
--   total_granules, total_relations, linked_granules, orphans
--   by_namespace: JSONB {namespace: {total, linked, orphans}}
--   by_link_type: JSONB {link_type: count}
-- ════════════════════════════════════════════════════════════

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
    -- Собираем все уникальные id, которые участвуют в связях
    SELECT array_agg(DISTINCT id)
    INTO v_linked_ids
    FROM (
        SELECT source_id AS id FROM relations
        UNION
        SELECT target_id AS id FROM relations WHERE target_id IS NOT NULL
    ) sub;

    -- Если массив NULL (нет связей) — заменяем на пустой массив
    IF v_linked_ids IS NULL THEN
        v_linked_ids := ARRAY[]::UUID[];
    END IF;

    -- Общая статистика
    SELECT
        count(*) FILTER (WHERE is_archived = false),
        (SELECT count(*) FROM relations),
        count(*) FILTER (WHERE is_archived = false AND id = ANY(v_linked_ids)),
        count(*) FILTER (WHERE is_archived = false AND NOT id = ANY(v_linked_ids))
    INTO p_total_granules, p_total_relations, p_linked_granules, p_orphans
    FROM memories;

    -- Статистика по namespace
    SELECT coalesce(jsonb_object_agg(
        namespace,
        jsonb_build_object('total', total, 'linked', linked, 'orphans', orphans)
    ), '{}'::jsonb)
    INTO p_by_namespace
    FROM (
        SELECT
            m.namespace,
            count(*) AS total,
            count(*) FILTER (WHERE m.id = ANY(v_linked_ids)) AS linked,
            count(*) FILTER (WHERE NOT m.id = ANY(v_linked_ids)) AS orphans
        FROM memories m
        WHERE m.is_archived = false
        GROUP BY m.namespace
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

COMMENT ON FUNCTION graph_stats_unified IS 'Статистика графа знаний: общая + по namespace + по link_type. Один запрос вместо трёх.';


-- ════════════════════════════════════════════════════════════
-- 5. Обход графа с полным возвратом нод и рёбер
-- ════════════════════════════════════════════════════════════
-- Рекурсивный CTE обходит граф от start_id на глубину depth.
-- Возвращает:
--   nodes: {id, content, namespace, importance, depth}
--   edges: {id, source_id, target_id, link_type, description, weight}
--
-- Преимущество перед текущим TRAVERSE_CTE:
--   - Возвращает полные данные нод (без N+1 запросов)
--   - Возвращает рёбра в пределах обхода (без N+1 запросов)
--   - Один round-trip вместо 2N+2
-- ════════════════════════════════════════════════════════════

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
    -- Рекурсивный обход: собираем все reachability node_ids
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

    -- Если стартовая нода не существует — возвращаем пустые массивы
    IF v_node_ids IS NULL THEN
        nodes := '[]'::jsonb;
        edges := '[]'::jsonb;
        RETURN NEXT;
        RETURN;
    END IF;

    -- Собираем ноды
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',          m.id,
            'content',     left(m.content, 200),
            'namespace',   m.namespace,
            'importance',  m.importance,
            'depth',       gw.depth
        )
    ), '[]'::jsonb)
    INTO v_nodes
    FROM graph_walk gw
    JOIN memories m ON m.id = gw.node_id;

    -- Собираем рёбра (только те, где обе ноды в пределах обхода)
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

COMMENT ON FUNCTION graph_traverse_full IS 'Обход графа от start_id на depth уровней. Возвращает ноды и рёбра одним запросом вместо 2N+2.';


-- ════════════════════════════════════════════════════════════
-- 6. HNSW индекс (при необходимости)
-- ════════════════════════════════════════════════════════════
-- Раскомментировать когда датасет > 100K записей
-- или latency поиска станет критичной.
--
-- pgvector 0.8+ поддерживает HNSW для vector(4096).
-- ════════════════════════════════════════════════════════════

-- CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
--     ON memories USING hnsw (embedding vector_cosine_ops)
--     WITH (m = 16, ef_construction = 64);


-- ════════════════════════════════════════════════════════════
-- 7. Индекс для ускорения graph_stats_unified
-- ════════════════════════════════════════════════════════════
-- Покрывающий индекс для GRAPH_STATS: все данные для CTE берутся из индекса.
-- ════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_memories_graph_stats
    ON memories (is_archived, id, namespace)
    WHERE is_archived = false;

-- Покрывающий индекс для graph_traverse_full: рёбра с target_id
CREATE INDEX IF NOT EXISTS idx_relations_traverse
    ON relations (source_id, target_id, link_type, id)
    WHERE target_id IS NOT NULL;


-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[]);
-- DROP FUNCTION IF EXISTS graph_stats_unified(OUT INT, OUT INT, OUT INT, OUT INT, OUT JSONB, OUT JSONB);
-- DROP FUNCTION IF EXISTS memory_search_hnsw(vector(4096), TEXT, TEXT, FLOAT, INT);
-- DROP FUNCTION IF EXISTS memory_insert_batch(TEXT[], TEXT[], TEXT[], JSONB[], TEXT[], UUID[], TEXT[], INT[]);
-- DROP FUNCTION IF EXISTS memory_upsert(TEXT, TEXT, vector(4096), JSONB, TEXT, UUID, TEXT, INT);
-- DROP INDEX IF EXISTS idx_memories_graph_stats;
-- DROP INDEX IF EXISTS idx_relations_traverse;
-- DROP INDEX IF EXISTS idx_memories_embedding_hnsw;
