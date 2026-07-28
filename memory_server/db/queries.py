INSERT_MEMORY = """
    INSERT INTO memories (user_id, content, embedding, metadata, namespace, content_hash)
    VALUES ($1, $2, $3::vector, $4::jsonb, $5, $6)
    RETURNING id
"""

INSERT_MEMORY_BATCH = """
    INSERT INTO memories (user_id, content, embedding, metadata, namespace, content_hash)
    SELECT
        unnest($1::text[]),
        unnest($2::text[]),
        unnest($3::vector[])::vector,
        unnest($4::jsonb[]),
        unnest($5::text[]),
        unnest($6::text[])
    RETURNING id
"""

SELECT_MEMORY_BY_ID = """
    SELECT id, user_id, content, metadata, namespace, created_at, updated_at, content_hash
    FROM memories
    WHERE id = $1
"""

SELECT_MEMORY_BY_CONTENT_HASH = """
    SELECT id, user_id, content, metadata, namespace, created_at, updated_at, content_hash
    FROM memories
    WHERE namespace = $1 AND content_hash = $2
"""

# HNSW search via SQL function (defined in 001_initial.sql).
# Порог отсечения применяется в SQL для точности,
# но HNSW всё равно может возвращать результаты чуть ниже порога
# (используется как финальный фильтр).
# ef_search выставляется на пуле соединений (см. pool.py).
SEARCH_MEMORIES = """
    SELECT id, content, metadata,
           1 - (embedding <=> $1::vector) AS score
    FROM memories
    WHERE ($2::text IS NULL OR user_id = $2)
      AND ($3::text IS NULL OR namespace = $3)
      AND is_archived = false
      AND 1 - (embedding <=> $1::vector) >= $4
    ORDER BY embedding <=> $1::vector
    LIMIT $5
"""

UPDATE_MEMORY = """
    UPDATE memories
    SET content = COALESCE($2, content),
        embedding = COALESCE($3::vector, embedding),
        metadata = COALESCE($4::jsonb, metadata),
        updated_at = now()
    WHERE id = $1
    RETURNING id, user_id, content, metadata, namespace, created_at, updated_at, content_hash
"""

DELETE_MEMORY = """
    DELETE FROM memories WHERE id = $1
    RETURNING id
"""

LIST_MEMORIES = """
    SELECT id, user_id, content, metadata, namespace, created_at, updated_at, content_hash
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
    SELECT id, user_id, content, metadata, namespace, created_at, updated_at, content_hash
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
        SELECT id FROM linked_targets
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
