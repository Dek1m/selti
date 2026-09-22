-- edges_by_layer.sql — T0.1 «Жизнь графа знаний» (V3.5)
-- ============================================================
-- Срез рёбер по владельцам (metadata->>'source') и слоям линкера
-- (metadata->>'layer') для сверки с memory_graph_stats / memory_linker_stats
-- (links_by_layer). Плюс контрольный баланс: сумма срезов = total relations.
--
-- READ ONLY, statement_timeout 30s. Прод-окно: после 05:30 UTC.
-- Запуск: psql "$DSN" -f edges_by_layer.sql
-- Группировка по JSONB-полям = full scan 106k строк (~десятки мс), это
-- дешевле поддерживать, чем частичный индекс под разовую диагностику.

BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '30s';

-- 1. Рёбра по source × layer
SELECT COALESCE(metadata->>'source', '<none>')  AS rel_source,
       COALESCE(metadata->>'layer', '<none>')   AS layer,
       count(*)                                 AS edges,
       round(avg(weight)::numeric, 3)           AS avg_weight,
       min(weight)                              AS min_weight,
       max(weight)                              AS max_weight,
       count(*) FILTER (WHERE target_id IS NULL) AS dangling_target,
       round(100.0 * count(*) FILTER (WHERE target_id IS NULL)
             / nullif(count(*), 0), 2)          AS dangling_pct,
       min(created_at)                          AS oldest_edge,
       max(created_at)                          AS newest_edge
FROM relations
GROUP BY 1, 2
ORDER BY edges DESC;

-- 2. Баланс и справочные числа для сверки с memory_graph_stats
SELECT count(*)                                    AS relations_total,
       count(*) FILTER (WHERE target_id IS NULL)   AS dangling_total,
       count(*) FILTER (
           WHERE metadata->>'source' = 'linker_v3'
             AND metadata->>'layer' = 'l1c'
       )                                           AS linker_l1c,
       count(*) FILTER (
           WHERE metadata->>'source' = 'linker_v3'
             AND metadata->>'layer' = 'l1a'
       )                                           AS linker_l1a,
       count(*) FILTER (
           WHERE metadata->>'source' = 'linker_v3'
             AND metadata->>'layer' = 'l2'
       )                                           AS linker_l2,
       count(DISTINCT source_id)                   AS nodes_seen_as_source,
       count(DISTINCT target_id) FILTER (WHERE target_id IS NOT NULL)
                                                    AS nodes_seen_as_target
FROM relations;

COMMIT;
