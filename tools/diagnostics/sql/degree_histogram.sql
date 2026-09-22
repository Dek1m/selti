-- degree_histogram.sql — T0.1 «Жизнь графа знаний» (V3.5)
-- ============================================================
-- Распределение степеней узлов графа знаний (relations, ~106k рёбер):
--   1. перцентили/среднее/максимум полной степени (in + out);
--   2. бакеты степеней (0, 1, 2-3, ... 128+) — форма распределения;
--   3. покрытие графа: сколько asserted-гранул вообще без рёбер;
--   4. топ-20 хабов с именами (entity_name из metadata, миграция 018).
--
-- READ ONLY, statement_timeout 30s. Прод-окно: после 05:30 UTC.
-- Запуск: psql "$DSN" -f degree_histogram.sql
-- Висячие рёбра (target_id IS NULL) в степень target-узла не попадают:
-- адресата не существует.

BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '30s';

-- 1. Перцентили полной степени (только узлы, участвующие в рёбрах)
WITH node_degree AS (
    SELECT node_id, sum(deg) AS degree
    FROM (
        SELECT source_id AS node_id, count(*) AS deg
        FROM relations
        GROUP BY 1
        UNION ALL
        SELECT target_id, count(*)
        FROM relations
        WHERE target_id IS NOT NULL
        GROUP BY 1
    ) t
    GROUP BY 1
)
SELECT count(*)                                            AS nodes_with_edges,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY degree) AS p50,
       percentile_cont(0.90) WITHIN GROUP (ORDER BY degree) AS p90,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY degree) AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY degree) AS p99,
       round(avg(degree)::numeric, 2)                       AS avg_degree,
       max(degree)                                          AS max_degree
FROM node_degree;

-- 2. Бакеты степеней: границы — степени двойки минус 1 (power-law-хвост
--    в один широкий бакет 128+). Порядок бакетов — по нижней границе
--    (min(degree) в ORDER BY при GROUP BY: агрегат разрешён).
WITH node_degree AS (
    SELECT node_id, sum(deg) AS degree
    FROM (
        SELECT source_id AS node_id, count(*) AS deg
        FROM relations
        GROUP BY 1
        UNION ALL
        SELECT target_id, count(*)
        FROM relations
        WHERE target_id IS NOT NULL
        GROUP BY 1
    ) t
    GROUP BY 1
)
SELECT bucket,
       count(*)                                            AS nodes,
       round(100.0 * count(*) / sum(count(*)) OVER (), 2)   AS pct
FROM (
    SELECT CASE
               WHEN degree <= 1   THEN '0-1'
               WHEN degree <= 3   THEN '2-3'
               WHEN degree <= 7   THEN '4-7'
               WHEN degree <= 15  THEN '8-15'
               WHEN degree <= 31  THEN '16-31'
               WHEN degree <= 63  THEN '32-63'
               WHEN degree <= 127 THEN '64-127'
               ELSE '128+'
           END AS bucket,
       degree
    FROM node_degree
) b
GROUP BY bucket
ORDER BY min(degree);

-- 3. Покрытие графа: asserted-гранулы против узлов с рёбрами
WITH node_degree AS (
    SELECT node_id, sum(deg) AS degree
    FROM (
        SELECT source_id AS node_id, count(*) AS deg
        FROM relations
        GROUP BY 1
        UNION ALL
        SELECT target_id, count(*)
        FROM relations
        WHERE target_id IS NOT NULL
        GROUP BY 1
    ) t
    GROUP BY 1
)
SELECT (SELECT count(*)
        FROM memories
        WHERE status = 'asserted' AND valid_to IS NULL)     AS asserted_granules,
       count(node_id)                                        AS nodes_with_edges,
       (SELECT count(*)
        FROM memories
        WHERE status = 'asserted' AND valid_to IS NULL) - count(node_id)
                                                             AS isolated_granules,
       round(100.0 * count(node_id)
             / nullif((SELECT count(*)
                       FROM memories
                       WHERE status = 'asserted' AND valid_to IS NULL), 0), 2)
                                                             AS coverage_pct
FROM node_degree;

-- 4. Топ-20 хабов: полная степень + разложение in/out + имя
WITH out_degree AS (
    SELECT source_id AS node_id, count(*) AS out_deg
    FROM relations
    GROUP BY 1
),
in_degree AS (
    SELECT target_id AS node_id, count(*) AS in_deg
    FROM relations
    WHERE target_id IS NOT NULL
    GROUP BY 1
)
SELECT m.id::text                 AS granule_id,
       m.metadata->>'entity_name' AS entity_name,
       n.uid                      AS namespace,
       COALESCE(i.in_deg, 0)      AS in_degree,
       COALESCE(o.out_deg, 0)     AS out_degree,
       COALESCE(i.in_deg, 0) + COALESCE(o.out_deg, 0) AS total_degree,
       m.importance,
       m.cluster_id::text         AS cluster_id
FROM memories m
JOIN namespaces n          ON n.id = m.namespace_id
LEFT JOIN in_degree i      ON i.node_id = m.id
LEFT JOIN out_degree o     ON o.node_id = m.id
WHERE COALESCE(i.in_deg, 0) + COALESCE(o.out_deg, 0) > 0
ORDER BY total_degree DESC, m.id
LIMIT 20;

COMMIT;
