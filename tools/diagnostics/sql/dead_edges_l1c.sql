-- dead_edges_l1c.sql — T0.1 «Жизнь графа знаний» (V3.5)
-- ============================================================
-- Профиль l1c-рёбер линкера (co-occurrence, ~81k на проде):
--   1. возраст и вес;
--   2. доля singleton-пар (связь подтверждена ТОЛЬКО одним l1c-ребром —
--      между парой гранул нет рёбер других слоёв/источников);
--   3. топ-хабы по l1c-степени (в обе стороны);
--   4. висячие target_id IS NULL (для l1c ожидаемо 0: INSERT всегда
--      пишет конкретный id; ненулевое значение = повреждение данных).
--
-- READ ONLY, statement_timeout 30s. Прод-окно: после 05:30 UTC.
-- Запуск: psql "$DSN" -f dead_edges_l1c.sql
-- Владение l1c-ребром: metadata->>'source' = 'linker_v3' AND
-- metadata->>'layer' = 'l1c' (канон по memory_server/db/queries.py).

BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '30s';

-- 1. Возраст и вес l1c-рёбер
WITH l1c AS (
    SELECT r.created_at, r.weight
    FROM relations r
    WHERE r.metadata->>'source' = 'linker_v3'
      AND r.metadata->>'layer' = 'l1c'
)
SELECT count(*)                                                             AS edges_total,
       min(created_at)                                                      AS oldest_edge,
       max(created_at)                                                      AS newest_edge,
       round(avg(extract(epoch FROM (now() - created_at)) / 86400)::numeric, 1) AS avg_age_days,
       count(*) FILTER (WHERE created_at >= now() - interval '7 days')      AS age_lt_7d,
       count(*) FILTER (WHERE created_at <  now() - interval '90 days')     AS age_gt_90d,
       round(avg(weight)::numeric, 3)                                       AS avg_weight,
       min(weight)                                                          AS min_weight,
       max(weight)                                                          AS max_weight
FROM l1c;

-- 2. Singleton-доля СРЕДИ l1c-пар: пары гранул (A, B) с l1c-ребром, между
--    которыми НИ ОДНОГО ребра другого источника (ни в одну сторону).
--    Повторная co-occurrence не создаёт второе ребро (ON CONFLICT DO
--    NOTHING в INSERT_COOCCURRENCE_LINKS), поэтому «повторное
--    подтверждение» наблюдаемо только через рёбра других источников:
--    l1a/l2/ручные/Тишь. Singleton-пара — кандидат decay-очистки.
--    Один full scan + hash aggregate по парам вместо вложенного EXISTS:
--    на 106k рёбер секунды, не десятки.
WITH pair_stats AS (
    SELECT least(source_id, target_id)   AS node_a,
           greatest(source_id, target_id) AS node_b,
           count(*) FILTER (
               WHERE metadata->>'source' = 'linker_v3'
                 AND metadata->>'layer' = 'l1c'
           ) AS l1c_edges,
           count(*) FILTER (
               WHERE NOT (metadata->>'source' = 'linker_v3'
                      AND metadata->>'layer' = 'l1c')
           ) AS non_l1c_edges
    FROM relations
    WHERE target_id IS NOT NULL
    GROUP BY 1, 2
)
SELECT count(*) FILTER (WHERE l1c_edges > 0)                       AS l1c_pairs,
       count(*) FILTER (WHERE l1c_edges > 0 AND non_l1c_edges = 0) AS singleton_pairs,
       round(100.0 * count(*) FILTER (WHERE l1c_edges > 0 AND non_l1c_edges = 0)
             / nullif(count(*) FILTER (WHERE l1c_edges > 0), 0), 2) AS singleton_pct
FROM pair_stats;

-- 3. Топ-20 хабов по l1c-степени (in + out); имя — entity_name из
--    metadata гранулы (отдельной колонки в схеме нет, миграция 018).
WITH l1c_nodes AS (
    SELECT node_id, count(*) AS l1c_degree
    FROM (
        SELECT source_id AS node_id
        FROM relations
        WHERE metadata->>'source' = 'linker_v3'
          AND metadata->>'layer' = 'l1c'
        UNION ALL
        SELECT target_id
        FROM relations
        WHERE metadata->>'source' = 'linker_v3'
          AND metadata->>'layer' = 'l1c'
    ) t
    GROUP BY 1
)
SELECT d.l1c_degree,
       m.id::text               AS granule_id,
       m.metadata->>'entity_name' AS entity_name,
       n.uid                    AS namespace,
       m.importance,
       m.created_at
FROM l1c_nodes d
JOIN memories m   ON m.id = d.node_id
JOIN namespaces n ON n.id = m.namespace_id
ORDER BY d.l1c_degree DESC, m.id
LIMIT 20;

-- 4. Висячие target: доля рёбер без узла-адресата — l1c против всего графа
SELECT count(*) FILTER (
           WHERE metadata->>'source' = 'linker_v3'
             AND metadata->>'layer' = 'l1c'
       )                                              AS l1c_total,
       count(*) FILTER (
           WHERE metadata->>'source' = 'linker_v3'
             AND metadata->>'layer' = 'l1c'
             AND target_id IS NULL
       )                                              AS l1c_dangling,
       count(*)                                       AS all_total,
       count(*) FILTER (WHERE target_id IS NULL)      AS all_dangling,
       count(*) FILTER (WHERE target_name IS NOT NULL AND target_id IS NULL)
                                                      AS all_dangling_with_name
FROM relations;

COMMIT;
