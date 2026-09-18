"""SQL-константы под каноническую схему memories (миграции 006/018).

Контракты волны 2 (Фаза 0.4):
  * namespace — ТОЛЬКО namespace_id UUID + JOIN namespaces (uid — для внешнего
    строкового контракта); TEXT-колонка дропается миграцией 018c.
  * Актуальность гранулы = status='asserted' AND valid_to IS NULL
    (is_archived дропается миграцией 018b).
  * Мёртвые DEFAULT-колонки (status/valid_from/valid_to/ingested_at/
    superseded_by) в INSERT не дублируем — их покрывают DEFAULT'ы БД.
"""

# Каноническая проекция memories: всё, что нужно MemoryRecord.
_MEMORY_COLUMNS = """
    m.id, m.user_id, m.content, m.metadata, n.uid AS namespace, m.importance,
    m.created_at, m.updated_at, m.content_hash,
    m.project_id, m.status, m.confidence, m.valid_from, m.valid_to, m.ingested_at,
    m.supersedes, m.superseded_by, m.frozen
"""

INSERT_MEMORY = """
    INSERT INTO memories (
        user_id, content, metadata, namespace_id,
        content_hash, importance, project_id, confidence, frozen, supersedes
    )
    VALUES ($1, $2, $3::jsonb, $4::uuid, $5, $6, $7::uuid, COALESCE($8, 1.0), COALESCE($9, false), $10::uuid)
    RETURNING id
"""

INSERT_MEMORY_BATCH = """
    INSERT INTO memories (
        user_id, content, metadata, namespace_id,
        content_hash, importance, project_id
    )
    SELECT
        unnest($1::text[]),
        unnest($2::text[]),
        unnest($3::jsonb[]),
        unnest($4::uuid[]),
        unnest($5::text[]),
        unnest($6::int[]),
        unnest($7::uuid[])
    RETURNING id
"""

SELECT_MEMORY_BY_ID = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.id = $1
"""

# Exact-dedup: JOIN по uid — резолв имени делает БД по UNIQUE-индексу
# (idx_namespaces_uid), без отдельного round-trip в Python.
SELECT_MEMORY_BY_CONTENT_HASH = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE n.uid = $1 AND m.content_hash = $2
      AND m.status = 'asserted' AND m.valid_to IS NULL
"""

SELECT_MEMORY_BY_ENTITY_NAME = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.metadata->>'entity_name' = $1
    LIMIT 1
"""

# FTS fallback — используется когда Qdrant недоступен.
# Основной путь: Qdrant vector search (repository.py → qdrant_store.py).
SEARCH_MEMORIES = f"""
    SELECT
        {_MEMORY_COLUMNS},
        ts_rank(to_tsvector('simple', m.content), plainto_tsquery('simple', $1)) AS score
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE ($2::text IS NULL OR m.user_id = $2)
      AND ($3::uuid IS NULL OR m.namespace_id = $3)
      AND ($4::uuid IS NULL OR m.project_id = $4)
      AND m.status = 'asserted' AND m.valid_to IS NULL
      AND to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $1)
    ORDER BY score DESC
    LIMIT $5
"""

# metadata — dict-merge (|| — shallow merge, новые ключи затирают старые),
# а не COALESCE-затирание всего JSONB. version инкрементит триггер
# trg_memories_version_bump (миграция 018) при изменении content.
UPDATE_MEMORY = f"""
    UPDATE memories m
    SET content    = COALESCE($2, content),
        metadata   = CASE WHEN $3::jsonb IS NULL THEN metadata ELSE metadata || $3::jsonb END,
        importance = COALESCE($4, importance),
        project_id = COALESCE($5::uuid, project_id),
        confidence = COALESCE($6, confidence),
        frozen     = COALESCE($7, frozen),
        supersedes = COALESCE($8::uuid, supersedes),
        updated_at = now()
    FROM namespaces n
    WHERE m.id = $1 AND n.id = m.namespace_id
    RETURNING {_MEMORY_COLUMNS}
"""

# Supersession (D3): окно валидности старой гранулы закрывается valid_from
# новой (правило Graphiti) одним запросом, без чтения новой версии из Python.
SUPERSEDE_MEMORY = """
    UPDATE memories old
    SET status       = 'superseded',
        valid_to     = new.valid_from,
        superseded_by = new.id,
        updated_at   = now()
    FROM memories new
    WHERE old.id = $1 AND new.id = $2 AND old.status = 'asserted'
    RETURNING old.id
"""

DELETE_MEMORY = """
    DELETE FROM memories WHERE id = $1
    RETURNING id
"""

# COUNT(*) OVER() — total в том же проходе (паттерн list_with_count из 014,
# вынесен в код: полная проекция без контракта с телом хранимки).
LIST_MEMORIES = f"""
    SELECT {_MEMORY_COLUMNS}, COUNT(*) OVER() AS total_count
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE ($1::text IS NULL OR m.user_id = $1)
      AND ($2::uuid IS NULL OR m.namespace_id = $2)
      AND ($3::uuid IS NULL OR m.project_id = $3)
      AND m.status = 'asserted' AND m.valid_to IS NULL
    ORDER BY m.created_at DESC
    LIMIT $4 OFFSET $5
"""

# Мягкое забвение (бывшая memory_forget_soft): retracted + окно валидности
# закрывается now(). CTE — один round-trip.
FORGET_MEMORIES = """
    WITH retracted AS (
        UPDATE memories
        SET status = 'retracted', valid_to = now(), updated_at = now()
        WHERE user_id = $1
          AND status = 'asserted'
          AND ($2::uuid IS NULL OR namespace_id = $2)
        RETURNING id
    )
    SELECT count(*)::bigint FROM retracted
"""

RECENT_MEMORIES = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE ($1::uuid IS NULL OR m.namespace_id = $1)
      AND ($2::uuid IS NULL OR m.project_id = $2)
      AND ($3::timestamptz IS NULL OR m.created_at >= $3)
      AND m.status = 'asserted' AND m.valid_to IS NULL
    ORDER BY m.created_at DESC
    LIMIT $4
"""

MEMORY_STATS = """
    SELECT n.uid AS namespace, count(*) AS count, max(m.updated_at) AS last_updated
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE ($1::text IS NULL OR m.user_id = $1)
      AND m.status = 'asserted' AND m.valid_to IS NULL
    GROUP BY n.uid
    ORDER BY n.uid
"""

# Батч-fetch метаданных по IDs для Qdrant-выдачи: фильтр актуальности
# на уровне SQL, чтобы archived не съедали лимит выдачи.
FETCH_MEMORIES_BY_IDS = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.id = ANY($1::uuid[])
      AND m.status = 'asserted' AND m.valid_to IS NULL
"""

# Отзыв гранулы (бывший ARCHIVE_MEMORY → is_archived).
RETRACT_MEMORY = """
    UPDATE memories
    SET status = 'retracted', valid_to = now(), updated_at = now()
    WHERE id = $1 AND status = 'asserted'
    RETURNING id
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


# ── Stored procedures (миграции 009/014; тела под каноническую схему — 020) ──

GET_RELATIONS_UNIFIED = """
    SELECT * FROM get_relations_unified($1, $2)
"""

TRAVERSE_FULL = """
    SELECT * FROM graph_traverse_full($1, $2, $3)
"""

GRAPH_STATS_UNIFIED = """
    SELECT * FROM graph_stats_unified()
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


# ── Project contexts (миграция 019, «облачко знаний» D9) ──

# Топ-гранулы проекта с квотами per namespace — хранимка 019.
FETCH_PROJECT_CONTEXT = """
    SELECT content, namespace, importance, updated_at
    FROM project_context_snapshot($1::uuid, $2)
"""

UPSERT_PROJECT_CONTEXT = """
    INSERT INTO project_contexts (project_id, content, sections, granule_count, computed_at)
    VALUES ($1::uuid, $2, $3::jsonb, $4, now())
    ON CONFLICT (project_id) DO UPDATE SET
        content = EXCLUDED.content,
        sections = EXCLUDED.sections,
        granule_count = EXCLUDED.granule_count,
        computed_at = EXCLUDED.computed_at
    RETURNING project_id::text, computed_at
"""

SELECT_PROJECT_CONTEXT = """
    SELECT project_id::text, content, sections, granule_count, computed_at
    FROM project_contexts
    WHERE project_id = $1::uuid
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

# Прямой колонки project_id в resource_hashes нет (009 — JSONB metadata,
# 017 таблицу не расширяла): фильтр по выражению; значение — UUID-строка
# после резолва slug→UUID. Expression-индекс — на стороне миграции 020.
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
                WHEN link->>'target' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
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
