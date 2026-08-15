-- ============================================================
-- 016_merge_similar_granules_stored_proc.sql
-- ============================================================
-- Хранимка для кластеризации похожих гранул.
-- Принимает пары (source_id, target_id, similarity) из Python,
-- кластеризует через recursive CTE (Union-Find аналог),
-- возвращает JSON план мерджа.
--
-- Архитектура:
--   1. Qdrant — ANN поиск кандидатов (cosine > threshold)
--   2. PostgreSQL — кластеризация + построение плана
--   3. Python — thin wrapper (вызов Qdrant + вызов хранимки)
--
-- Почему не pgvector:
--   pgvector удалён из контейнера (миграция 011).
--   HNSW pgvector ограничен 2000 dim, эмбеддинги 4096-dim.
--   Qdrant уже предоставляет ANN с HNSW без ограничений.
-- ============================================================

BEGIN;

-- ════════════════════════════════════════════════════════════
-- Временная таблица для хранения пар сходства
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS _similarity_pairs (
    source_id UUID NOT NULL,
    target_id UUID NOT NULL,
    similarity FLOAT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sim_pairs_source ON _similarity_pairs (source_id);
CREATE INDEX IF NOT EXISTS idx_sim_pairs_target ON _similarity_pairs (target_id);

-- ════════════════════════════════════════════════════════════
-- Хранимка: merge_similar_granules
-- ════════════════════════════════════════════════════════════
-- Вход: пары уже загружены в _similarity_pairs через Python
-- Выход: JSON с планом мерджа
--
-- Алгоритм кластеризации:
--   1. Строим граф смежности из пар сходства
--   2. Recursive CTE обходит граф, находит компоненты связности
--   3. Для каждой группы: ядро (макс importance), средний score
--   4. Возвращаем JSON
-- ════════════════════════════════════════════════════════════

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
    -- Проверяем наличие данных
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

    -- ════════════════════════════════════════════════════════
    -- Шаг 1: Кластеризация через recursive CTE
    -- ════════════════════════════════════════════════════════
    -- Recursive CTE находит компоненты связности в графе
    -- Это аналог Union-Find: если A-B и B-C, то A-B-C в одной группе

    WITH RECURSIVE
    -- Все уникальные узлы графа
    all_nodes AS (
        SELECT source_id AS node_id FROM _similarity_pairs
        UNION
        SELECT target_id FROM _similarity_pairs
    ),
    -- Рёбра графа (обе направления для неориентированного графа)
    all_edges AS (
        SELECT source_id AS from_node, target_id AS to_node, similarity
        FROM _similarity_pairs
        UNION
        SELECT target_id, source_id, similarity
        FROM _similarity_pairs
    ),
    -- Recursive обход: начинаем с каждого узла, расширяем компоненту
    components AS (
        -- Anchor: каждый узел — начальная компонента
        SELECT
            node_id,
            node_id AS component_root,
            ARRAY[node_id] AS visited,
            0 AS depth
        FROM all_nodes

        UNION ALL

        -- Recursive: расширяем компоненту через рёбра
        SELECT
            e.to_node,
            c.component_root,
            c.visited || e.to_node,
            c.depth + 1
        FROM components c
        JOIN all_edges e ON e.from_node = c.node_id
        WHERE e.to_node <> ALL(c.visited)  -- защита от циклов
          AND c.depth < 10  -- лимит глубины рекурсии
    ),
    -- Находим минимальный component_root для каждого узла
    -- (это и будет ID компоненты связности)
    -- MIN для UUID не поддерживается, используем текстовое сравнение
    component_roots AS (
        SELECT
            node_id,
            (array_agg(component_root ORDER BY component_root::text))[1] AS group_id
        FROM components
        GROUP BY node_id
    ),
    -- Статистика по группам
    group_stats AS (
        SELECT
            cr.group_id,
            COUNT(DISTINCT cr.node_id) AS member_count,
            AVG(sp.similarity) AS avg_similarity
        FROM component_roots cr
        JOIN _similarity_pairs sp
            ON sp.source_id = cr.node_id OR sp.target_id = cr.node_id
        GROUP BY cr.group_id
        HAVING COUNT(DISTINCT cr.node_id) >= 2  -- только группы с 2+成员
        ORDER BY avg_similarity DESC
        LIMIT p_max_groups
    ),
    -- Для каждой группы находим ядро (максимальный importance)
    group_cores AS (
        SELECT DISTINCT ON (gs.group_id)
            gs.group_id,
            gs.member_count,
            gs.avg_similarity,
            m.id AS core_id,
            m.content AS core_content,
            m.importance AS core_importance,
            m.namespace AS core_namespace
        FROM group_stats gs
        JOIN component_roots cr ON cr.group_id = gs.group_id
        JOIN memories m ON m.id = cr.node_id
        ORDER BY gs.group_id, m.importance DESC, m.created_at DESC
    ),
    -- Собираем всех成员ей группы
    group_members AS (
        SELECT
            cr.group_id,
            json_agg(
                json_build_object(
                    'id', m.id::text,
                    'content', LEFT(m.content, 300),
                    'importance', m.importance,
                    'namespace', m.namespace,
                    'is_core', (m.id = gc.core_id),
                    'project_id', COALESCE(m.metadata->>'project_id', '')
                )
                ORDER BY m.importance DESC, m.id
            ) AS members_json
        FROM component_roots cr
        JOIN group_stats gs ON gs.group_id = cr.group_id
        JOIN memories m ON m.id = cr.node_id
        JOIN group_cores gc ON gc.group_id = cr.group_id
        GROUP BY cr.group_id, gc.core_id
    )
    -- Финальная сборка JSON
    SELECT json_build_object(
        'version', 2,
        'total_groups', (SELECT COUNT(*) FROM group_stats),
        'total_granules', (
            SELECT SUM(member_count) FROM group_stats
        ),
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

COMMENT ON FUNCTION merge_similar_granules IS 'Кластеризация похожих гранул. Пары загружаются в _similarity_pairs перед вызовом. Возвращает JSON план мерджа.';

-- ════════════════════════════════════════════════════════════
-- Хранимка: find_similar_pairs (для pgvector, если будет добавлен)
-- ════════════════════════════════════════════════════════════
-- Альтернативный путь: если pgvector будет восстановлен,
-- можно искать пары прямо в PostgreSQL без Qdrant.
--
-- Ограничения pgvector:
--   - HNSW: max 2000 dim (у нас 4096 → не подходит)
--   - Точный поиск: sequential scan, O(N²) — медленно
--   - halfvec(4096): теряется точность fp16
--
-- Рекомендация: используйте Qdrant для ANN, эту функцию — как fallback.

CREATE OR REPLACE FUNCTION find_similar_pairs_pgvector(
    p_threshold FLOAT DEFAULT 0.9,
    p_limit INT DEFAULT 2000,
    p_max_neighbors INT DEFAULT 10
)
RETURNS INT
LANGUAGE plpgsql
AS $$
DECLARE
    v_count INT := 0;
BEGIN
    -- Очищаем временную таблицу
    TRUNCATE _similarity_pairs;

    -- Если embedding колонка существует — используем pgvector
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'memories' AND column_name = 'embedding'
    ) THEN
        INSERT INTO _similarity_pairs (source_id, target_id, similarity)
        SELECT
            a.id AS source_id,
            b.id AS target_id,
            1 - (a.embedding <=> b.embedding) AS similarity
        FROM memories a
        CROSS JOIN LATERAL (
            SELECT id, embedding
            FROM memories b
            WHERE b.id <> a.id
              AND b.namespace = a.namespace
              AND b.is_archived = false
              AND a.embedding IS NOT NULL
              AND b.embedding IS NOT NULL
            ORDER BY a.embedding <=> b.embedding
            LIMIT p_max_neighbors
        ) b
        WHERE 1 - (a.embedding <=> b.embedding) >= p_threshold
          AND a.is_archived = false
        LIMIT p_limit;

        GET DIAGNOSTICS v_count = ROW_COUNT;
    END IF;

    RETURN v_count;
END;
$$;

COMMENT ON FUNCTION find_similar_pairs_pgvector IS 'Поиск пар через pgvector (fallback). Требует колонку embedding. Используйте Qdrant если pgvector недоступен.';

-- ════════════════════════════════════════════════════════════
--DOWN migration
-- ════════════════════════════════════════════════════════════
-- DROP FUNCTION IF EXISTS merge_similar_granules(FLOAT, INT);
-- DROP FUNCTION IF EXISTS find_similar_pairs_pgvector(FLOAT, INT, INT);
-- DROP TABLE IF EXISTS _similarity_pairs;

COMMIT;
