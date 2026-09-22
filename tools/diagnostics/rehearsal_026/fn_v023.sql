CREATE FUNCTION public.graph_traverse_full_v023(p_start_id uuid, p_depth integer DEFAULT 3, p_link_types text[] DEFAULT NULL::text[], p_as_of timestamp with time zone DEFAULT now()) RETURNS TABLE(nodes jsonb, edges jsonb)
    LANGUAGE sql STABLE
    AS $$
WITH RECURSIVE graph_walk AS (
    -- Якорь: стартовая нода, только если её окно валидности покрывает as_of
    SELECT
        p_start_id        AS node_id,
        0                 AS depth,
        ARRAY[p_start_id] AS path
    WHERE EXISTS (
        SELECT 1 FROM memories m
        WHERE m.id = p_start_id
          AND m.valid_from <= p_as_of
          AND (m.valid_to IS NULL OR m.valid_to > p_as_of)
    )
    UNION
    -- Шаг обхода: вперёд по эффективному source → target с проекцией
    -- происхождения; нода-цель обязана быть живой на as_of (окно)
    SELECT
        eff.eff_target,
        gw.depth + 1,
        gw.path || eff.eff_target
    FROM graph_walk gw
    JOIN LATERAL (
        SELECT
            CASE WHEN r.inherited_from IS NOT NULL AND src.valid_from > p_as_of
                 THEN r.inherited_from ELSE r.source_id END AS eff_source,
            CASE WHEN r.inherited_from IS NOT NULL AND tgt.valid_from > p_as_of
                 THEN r.inherited_from ELSE r.target_id END AS eff_target,
            r.link_type
        FROM relations r
        JOIN memories src ON src.id = r.source_id
        JOIN memories tgt ON tgt.id = r.target_id
        WHERE r.target_id IS NOT NULL
          AND (r.source_id = gw.node_id OR r.inherited_from = gw.node_id)
          AND (p_link_types IS NULL OR r.link_type = ANY(p_link_types))
    ) eff ON eff.eff_source = gw.node_id
    WHERE gw.depth < p_depth
      AND NOT eff.eff_target = ANY(gw.path)
      AND EXISTS (
          SELECT 1 FROM memories m
          WHERE m.id = eff.eff_target
            AND m.valid_from <= p_as_of
            AND (m.valid_to IS NULL OR m.valid_to > p_as_of)
      )
),
uniq_nodes AS (
    -- Узел с несколькими путями достижимости → одна строка
    -- с минимальной глубиной (DISTINCT ON, как в 021)
    SELECT DISTINCT ON (node_id)
        node_id,
        depth
    FROM graph_walk
    ORDER BY node_id, depth
),
edge_proj AS (
    -- Рёбра с эффективными концами на as_of: перенесённые после as_of
    -- остаются на inherited_from-версии (историческое место ребра).
    -- Предфильтр по факту касания обхода — физических ИЛИ унаследованных
    -- концов — держит выборку индексной, финальный фильтр ниже
    -- перепроверяет эффективные концы.
    SELECT
        r.id AS rel_id,
        r.link_type,
        r.description,
        r.weight,
        CASE WHEN r.inherited_from IS NOT NULL AND src.valid_from > p_as_of
             THEN r.inherited_from ELSE r.source_id END AS eff_source,
        CASE WHEN r.inherited_from IS NOT NULL AND tgt.valid_from > p_as_of
             THEN r.inherited_from ELSE r.target_id END AS eff_target
    FROM relations r
    JOIN memories src ON src.id = r.source_id
    JOIN memories tgt ON tgt.id = r.target_id
    WHERE r.target_id IS NOT NULL
      AND (r.source_id IN (SELECT node_id FROM uniq_nodes)
           OR r.target_id IN (SELECT node_id FROM uniq_nodes)
           OR r.inherited_from IN (SELECT node_id FROM uniq_nodes))
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
    -- Рёбра: оба эффективных конца в пределах обхода
    (
        SELECT coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'id',          e.rel_id,
                    'source_id',   e.eff_source,
                    'target_id',   e.eff_target,
                    'link_type',   e.link_type,
                    'description', e.description,
                    'weight',      e.weight
                )
                ORDER BY e.rel_id
            ),
            '[]'::jsonb
        )
        FROM edge_proj e
        WHERE e.eff_source IN (SELECT node_id FROM uniq_nodes)
          AND e.eff_target IN (SELECT node_id FROM uniq_nodes)
    ) AS edges;
$$;
