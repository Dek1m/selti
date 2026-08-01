INSERT_MEMORY = """
    INSERT INTO memories (user_id, content, metadata, namespace, namespace_id, content_hash, importance)
    VALUES ($1, $2, $3::jsonb, $4, $5::uuid, $6, $7)
    RETURNING id
"""

INSERT_MEMORY_BATCH = """
    INSERT INTO memories (user_id, content, metadata, namespace, namespace_id, content_hash, importance)
    SELECT
        unnest($1::text[]),
        unnest($2::text[]),
        unnest($3::jsonb[]),
        unnest($4::text[]),
        unnest($5::uuid[]),
        unnest($6::text[]),
        unnest($7::int[])
    RETURNING id
"""

SELECT_MEMORY_BY_ID = """
    SELECT id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
    FROM memories
    WHERE id = $1
"""

SELECT_MEMORY_BY_CONTENT_HASH = """
    SELECT id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
    FROM memories
    WHERE namespace = $1 AND content_hash = $2
"""

# FTS fallback — используется когда Qdrant недоступен.
# Основной путь: Qdrant vector search (repository_qdrant.py).
SEARCH_MEMORIES = """
    SELECT
        id, user_id, content, metadata, namespace, importance,
        ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', $1)) AS score
    FROM memories
    WHERE ($2::text IS NULL OR user_id = $2)
      AND ($3::text IS NULL OR namespace = $3)
      AND is_archived = false
      AND to_tsvector('simple', content) @@ plainto_tsquery('simple', $1)
    ORDER BY score DESC
    LIMIT $4
"""

UPDATE_MEMORY = """
    UPDATE memories
    SET content = COALESCE($2, content),
        metadata = COALESCE($3::jsonb, metadata),
        importance = COALESCE($4, importance),
        updated_at = now()
    WHERE id = $1
    RETURNING id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
"""

DELETE_MEMORY = """
    DELETE FROM memories WHERE id = $1
    RETURNING id
"""

LIST_MEMORIES = """
    SELECT id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
    FROM memories
    WHERE ($1::text IS NULL OR user_id = $1)
      AND ($2::text IS NULL OR namespace = $2)
      AND is_archived = false
    ORDER BY created_at DESC
    LIMIT $3 OFFSET $4
"""

COUNT_MEMORIES = """
    SELECT count(*) FROM memories
    WHERE ($1::text IS NULL OR user_id = $1)
      AND ($2::text IS NULL OR namespace = $2)
"""

FORGET_MEMORIES = """
    DELETE FROM memories
    WHERE user_id = $1
      AND ($2::text IS NULL OR namespace = $2)
"""

RECENT_MEMORIES = """
    SELECT id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
    FROM memories
    WHERE ($1::text IS NULL OR namespace = $1)
      AND ($2::timestamptz IS NULL OR created_at >= $2)
      AND is_archived = false
    ORDER BY created_at DESC
    LIMIT $3
"""

MEMORY_STATS = """
    SELECT namespace, count(*) as count, max(updated_at) as last_updated
    FROM memories
    WHERE ($1::text IS NULL OR user_id = $1) AND is_archived = false
    GROUP BY namespace
    ORDER BY namespace
"""


# ── Relations queries ──

INSERT_RELATION = """
    INSERT INTO relations (source_id, target_id, target_name, link_type, description, weight, metadata)
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
    ON CONFLICT (source_id, target_id, link_type) WHERE target_id IS NOT NULL
    DO UPDATE SET
        description = EXCLUDED.description,
        weight = EXCLUDED.weight,
        metadata = EXCLUDED.metadata
    RETURNING id
"""

SELECT_RELATIONS_BY_SOURCE = """
    SELECT id, source_id, target_id, target_name, link_type, description, weight, metadata, created_at
    FROM relations
    WHERE source_id = $1
      AND ($2::text IS NULL OR link_type = $2)
    ORDER BY created_at DESC
"""

SELECT_RELATIONS_BY_TARGET = """
    SELECT id, source_id, target_id, target_name, link_type, description, weight, metadata, created_at
    FROM relations
    WHERE target_id = $1
      AND ($2::text IS NULL OR link_type = $2)
    ORDER BY created_at DESC
"""

DELETE_RELATION = """
    DELETE FROM relations
    WHERE source_id = $1 AND target_id = $2 AND link_type = $3
    RETURNING id
"""

DELETE_RELATIONS_BY_SOURCE = """
    DELETE FROM relations WHERE source_id = $1
"""

GRAPH_STATS = """
    WITH
    active_memories AS (
        SELECT id, namespace FROM memories WHERE is_archived = false
    ),
    linked_sources AS (
        SELECT DISTINCT source_id FROM relations
    ),
    linked_targets AS (
        SELECT DISTINCT target_id FROM relations WHERE target_id IS NOT NULL
    ),
    linked_ids AS (
        SELECT source_id AS id FROM linked_sources
        UNION
        SELECT target_id AS id FROM linked_targets
    ),
    orphan_count AS (
        SELECT count(*) AS cnt
        FROM active_memories m
        LEFT JOIN linked_ids l ON m.id = l.id
        WHERE l.id IS NULL
    )
    SELECT
        (SELECT count(*) FROM active_memories) AS total_granules,
        (SELECT count(*) FROM relations) AS total_relations,
        (SELECT count(*) FROM linked_ids) AS linked_granules,
        (SELECT cnt FROM orphan_count) AS orphans
"""

# ── Stored procedures (миграция 014) ──

GET_RELATIONS_UNIFIED = """
    SELECT * FROM get_relations_unified($1, $2)
"""

LIST_WITH_COUNT = """
    SELECT * FROM list_with_count($1, $2, $3, $4)
"""

MEMORY_FORGET_SOFT = """
    SELECT * FROM memory_forget_soft($1, $2)
"""


# ── Stored procedures (миграция 009) ──

TRAVERSE_FULL = """
    SELECT * FROM graph_traverse_full($1, $2, $3)
"""

GRAPH_STATS_UNIFIED = """
    SELECT * FROM graph_stats_unified()
"""

GRAPH_STATS_BY_NAMESPACE = """
    WITH
    active_memories AS (
        SELECT id, namespace FROM memories WHERE is_archived = false
    ),
    linked_ids AS (
        SELECT DISTINCT source_id AS id FROM relations
        UNION
        SELECT DISTINCT target_id AS id FROM relations WHERE target_id IS NOT NULL
    )
    SELECT
        m.namespace,
        count(DISTINCT m.id) AS total,
        count(DISTINCT m.id) FILTER (WHERE l.id IS NOT NULL) AS linked,
        count(DISTINCT m.id) FILTER (WHERE l.id IS NULL) AS orphans
    FROM active_memories m
    LEFT JOIN linked_ids l ON m.id = l.id
    GROUP BY m.namespace
    ORDER BY m.namespace
"""

GRAPH_STATS_BY_LINK_TYPE = """
    SELECT link_type, count(*) AS cnt
    FROM relations
    GROUP BY link_type
    ORDER BY cnt DESC
"""

TRAVERSE_CTE = """
    WITH RECURSIVE graph_walk AS (
        SELECT
            $1::uuid AS node_id,
            0 AS depth,
            ARRAY[$1::uuid] AS path
        UNION
        SELECT
            r.target_id,
            gw.depth + 1,
            gw.path || r.target_id
        FROM graph_walk gw
        JOIN relations r ON r.source_id = gw.node_id
        WHERE gw.depth < $2
          AND r.target_id IS NOT NULL
          AND NOT r.target_id = ANY(gw.path)
          AND ($3::text[] IS NULL OR r.link_type = ANY($3))
    )
    SELECT DISTINCT node_id, depth
    FROM graph_walk
    ORDER BY depth, node_id
"""

FIND_RELATIONS_BETWEEN = """
    SELECT id, source_id, target_id, target_name, link_type, description, weight, metadata, created_at
    FROM relations
    WHERE source_id = $1 AND target_id = $2
"""

ARCHIVE_MEMORY = """
    UPDATE memories
    SET is_archived = true, updated_at = now()
    WHERE id = $1 AND is_archived = false
    RETURNING id
"""


# ── Resource Hashes queries ──

UPSERT_RESOURCE_HASH = """
    INSERT INTO resource_hashes (source_type, source_id, content_hash, size_bytes, metadata)
    VALUES ($1, $2, $3, $4, $5::jsonb)
    ON CONFLICT (source_type, source_id)
    DO UPDATE SET
        content_hash = EXCLUDED.content_hash,
        size_bytes = EXCLUDED.size_bytes,
        metadata = EXCLUDED.metadata,
        updated_at = CASE
            WHEN resource_hashes.content_hash IS DISTINCT FROM EXCLUDED.content_hash
            THEN now()
            ELSE resource_hashes.updated_at
        END
    RETURNING id, created_at, updated_at
"""

SELECT_RESOURCE_HASH = """
    SELECT id, source_type, source_id, content_hash, size_bytes, metadata, created_at, updated_at
    FROM resource_hashes
    WHERE source_type = $1 AND source_id = $2
"""

LIST_RESOURCE_HASHES = """
    SELECT id, source_type, source_id, content_hash, size_bytes, metadata, created_at, updated_at
    FROM resource_hashes
    WHERE ($1::text IS NULL OR source_type = $1)
      AND ($2::timestamptz IS NULL OR updated_at >= $2)
      AND ($3::text IS NULL OR metadata->>'project_id' = $3)
    ORDER BY updated_at DESC
    LIMIT $4 OFFSET $5
"""

DELETE_RESOURCE_HASH = """
    DELETE FROM resource_hashes
    WHERE source_type = $1 AND source_id = $2
    RETURNING id
"""


# ── Backfill: metadata.links → relations ──

BACKFILL_RELATIONS_FROM_METADATA = """
    WITH source_links AS (
        SELECT
            m.id AS source_id,
            link->>'type' AS link_type,
            link->>'target' AS target_str,
            link->>'description' AS description,
            CASE
                WHEN link->>'target' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (link->>'target')::uuid
                ELSE NULL
            END AS target_id
        FROM memories m,
             jsonb_array_elements(m.metadata->'links') AS link
        WHERE m.id = $1
          AND m.metadata->'links' IS NOT NULL
          AND jsonb_array_length(m.metadata->'links') > 0
    )
    INSERT INTO relations (source_id, target_id, target_name, link_type, description, weight, metadata)
    SELECT
        sl.source_id,
        sl.target_id,
        CASE WHEN sl.target_id IS NULL THEN sl.target_str ELSE NULL END,
        sl.link_type,
        sl.description,
        1.0,
        '{"synced_from": "metadata.links"}'::jsonb
    FROM source_links sl
    WHERE sl.link_type IS NOT NULL
      AND (sl.target_id IS NULL OR EXISTS (SELECT 1 FROM memories WHERE id = sl.target_id))
    ON CONFLICT (source_id, target_id, link_type) WHERE target_id IS NOT NULL
    DO UPDATE SET
        description = EXCLUDED.description,
        weight = EXCLUDED.weight
    RETURNING id
"""

# Удалить metadata-based связи для гранулы (source_id = $1)
# Удаляются связи с пометкой synced_from = 'metadata.links'
# Ручные связи (без пометки) сохраняются
DELETE_SYNCED_RELATIONS = """
    DELETE FROM relations
    WHERE source_id = $1
      AND metadata->>'synced_from' = 'metadata.links'
"""

SYNC_LINKS_BATCH = """
    WITH source_links AS (
        SELECT
            m.id AS source_id,
            link->>'type' AS link_type,
            link->>'target' AS target_str,
            link->>'description' AS description,
            CASE
                WHEN link->>'target' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (link->>'target')::uuid
                ELSE NULL
            END AS target_id
        FROM memories m,
             jsonb_array_elements(m.metadata->'links') AS link
        WHERE m.id = ANY($1::uuid[])
          AND m.metadata->'links' IS NOT NULL
          AND jsonb_array_length(m.metadata->'links') > 0
    ),
    deleted AS (
        DELETE FROM relations
        WHERE source_id = ANY($1::uuid[])
          AND metadata->>'synced_from' = 'metadata.links'
        RETURNING id
    )
    INSERT INTO relations (source_id, target_id, target_name, link_type, description, weight, metadata)
    SELECT
        sl.source_id,
        sl.target_id,
        CASE WHEN sl.target_id IS NULL THEN sl.target_str ELSE NULL END,
        sl.link_type,
        sl.description,
        1.0,
        '{"synced_from": "metadata.links"}'::jsonb
    FROM source_links sl
    WHERE sl.link_type IS NOT NULL
      AND (sl.target_id IS NULL OR EXISTS (SELECT 1 FROM memories WHERE id = sl.target_id))
    ON CONFLICT (source_id, target_id, link_type) WHERE target_id IS NOT NULL
    DO UPDATE SET
        description = EXCLUDED.description,
        weight = EXCLUDED.weight,
        metadata = EXCLUDED.metadata
    RETURNING id
"""
