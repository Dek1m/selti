-- 012_backfill_relations_from_metadata.sql
-- Backfill: metadata.links → relations
-- Идемпотентный: ON CONFLICT DO NOTHING (не перезаписывает существующие связи)
--
-- Запуск:
--   docker exec postgres psql -U selti -d athena_memory -f /path/to/012_backfill_relations_from_metadata.sql
--
-- ROLLBACK:
--   DELETE FROM relations WHERE source_id IN (
--     SELECT id FROM memories WHERE metadata->'links' IS NOT NULL
--   ) AND id NOT IN (
--     -- Оставляем связи, созданные вручную через memory_link
--     SELECT r.id FROM relations r
--     JOIN memories m ON r.source_id = m.id
--     WHERE m.metadata->'links' IS NULL
--   );

BEGIN;

WITH source_links AS (
    SELECT
        m.id AS source_id,
        m.namespace AS source_namespace,
        link->>'type' AS link_type,
        link->>'target' AS target_id_str,
        link->>'description' AS description,
        CASE
            WHEN link->>'target' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (link->>'target')::uuid
            ELSE NULL
        END AS target_id,
        link->>'target' AS target_name
    FROM memories m,
         jsonb_array_elements(m.metadata->'links') AS link
    WHERE m.metadata->'links' IS NOT NULL
      AND jsonb_array_length(m.metadata->'links') > 0
      AND m.is_archived = false
),
inserted AS (
    INSERT INTO relations (source_id, target_id, target_name, link_type, description, weight, metadata)
    SELECT
        sl.source_id,
        sl.target_id,
        CASE WHEN sl.target_id IS NULL THEN sl.target_name ELSE NULL END,
        sl.link_type,
        sl.description,
        1.0,
        '{"synced_from": "metadata.links"}'::jsonb
    FROM source_links sl
    WHERE sl.link_type IS NOT NULL
      AND (sl.target_id IS NULL OR EXISTS (SELECT 1 FROM memories WHERE id = sl.target_id))
    ON CONFLICT (source_id, target_id, link_type) WHERE target_id IS NOT NULL
    DO NOTHING
    RETURNING id
)
SELECT count(*) AS inserted_count FROM inserted;

COMMIT;

-- Верификация после запуска:
-- SELECT count(*) AS total_relations FROM relations;
-- SELECT count(*) AS linked_granules FROM (
--   SELECT DISTINCT source_id FROM relations
--   UNION
--   SELECT DISTINCT target_id FROM relations WHERE target_id IS NOT NULL
-- ) sub;
