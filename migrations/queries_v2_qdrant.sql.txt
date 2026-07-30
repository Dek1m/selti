-- ============================================================
-- queries_v2_qdrant.sql — Обновлённые SQL-запросы для Qdrant flow
-- ============================================================
-- После миграции: embedding хранится в Qdrant, PostgreSQL хранит
-- только метаданные. Search идёт через Qdrant API.
-- ============================================================
-- Использовать как справочник для обновления queries.py
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- INSERT: без колонки embedding
-- ════════════════════════════════════════════════════════════
-- Новая запись → сначала INSERT в PG, потом upsert в Qdrant
-- ON CONFLICT для content_hash (dedup)
INSERT_MEMORY_V2 = """
    INSERT INTO memories (user_id, content, metadata, namespace, namespace_id, content_hash, importance)
    VALUES ($1, $2, $3::jsonb, $4, $5::uuid, $6, $7)
    ON CONFLICT (namespace, content_hash) WHERE content_hash IS NOT NULL
    DO UPDATE SET
        metadata = memories.metadata || EXCLUDED.metadata,
        importance = EXCLUDED.importance,
        updated_at = now()
    RETURNING id
"""

-- ════════════════════════════════════════════════════════════
-- UPDATE: без колонки embedding
-- ════════════════════════════════════════════════════════════
-- Обновление метаданных. Вектор обновляется отдельно через Qdrant API.
UPDATE_MEMORY_V2 = """
    UPDATE memories
    SET content = COALESCE($2, content),
        metadata = COALESCE($3::jsonb, metadata),
        importance = COALESCE($4, importance),
        updated_at = now()
    WHERE id = $1
    RETURNING id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
"""

-- ════════════════════════════════════════════════════════════
-- SELECT: без колонки embedding
-- ════════════════════════════════════════════════════════════
SELECT_MEMORY_BY_ID_V2 = """
    SELECT id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
    FROM memories
    WHERE id = $1
"""

SELECT_MEMORY_BY_CONTENT_HASH_V2 = """
    SELECT id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
    FROM memories
    WHERE namespace = $1 AND content_hash = $2
"""

-- ════════════════════════════════════════════════════════════
-- SEARCH: теперь через Qdrant API (не SQL)
-- ════════════════════════════════════════════════════════════
-- Python pseudocode:
--
--   # 1. Embed query
--   query_embedding = await embedding_provider.embed(query)
--
--   # 2. Search Qdrant with filters
--   results = qdrant_client.search(
--       collection_name="memories",
--       query_vector=query_embedding,
--       query_filter=qm.Filter(
--           must=[
--               qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
--               qm.FieldCondition(key="namespace", match=qm.MatchValue(value=namespace)),
--           ]
--       ),
--       limit=limit,
--       score_threshold=threshold,
--   )
--
--   # 3. Fetch full records from PG by IDs
--   ids = [r.id for r in results]
--   records = await pool.fetch(
--       "SELECT * FROM memories WHERE id = ANY($1::uuid[])",
--       ids
--   )
--
--   # 4. Merge: Qdrant scores + PG metadata
--   return merge(results, records)

-- ════════════════════════════════════════════════════════════
-- BATCH INSERT: без embedding
-- ════════════════════════════════════════════════════════════
INSERT_MEMORY_BATCH_V2 = """
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

-- ════════════════════════════════════════════════════════════
-- SEARCH_MEMORIES_V2: hybrid (Qdrant vector + PG metadata)
-- ════════════════════════════════════════════════════════════
-- PostgreSQL функция для batch fetch по ID (после Qdrant search)
CREATE OR REPLACE FUNCTION get_memories_by_ids(
    p_ids UUID[]
)
RETURNS TABLE(
    id UUID,
    user_id TEXT,
    content TEXT,
    metadata JSONB,
    namespace TEXT,
    importance INT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    content_hash TEXT
)
LANGUAGE SQL STABLE
AS $$
    SELECT id, user_id, content, metadata, namespace, importance,
           created_at, updated_at, content_hash
    FROM memories
    WHERE id = ANY(p_ids)
      AND is_archived = false
$$;

-- ════════════════════════════════════════════════════════════
-- Примечания по ON CONFLICT паттерну
-- ════════════════════════════════════════════════════════════
--
-- Паттерн: PG → Qdrant (двухфазная запись)
--
-- 1. memory_store(content, user_id, namespace):
--    a. embedding = await embed(content)
--    b. content_hash = sha256(content)
--    c. INSERT INTO memories (PG) → memory_id
--    d. qdrant.upsert(id=memory_id, vector=embedding, payload={...})
--    e. return memory_id
--
-- 2. memory_update(id, content=None, metadata=None):
--    a. Если content изменился:
--       - embedding = await embed(content)
--       - qdrant.update_vectors(id, vector=embedding)
--    b. UPDATE memories SET ... (PG)
--
-- 3. memory_delete(id):
--    a. DELETE FROM memories WHERE id = $1 (PG)
--    b. qdrant.delete(id) (Qdrant)
--
-- 4. memory_search(query, user_id, namespace):
--    a. embedding = await embed(query)
--    b. results = qdrant.search(vector=embedding, filter=...) → [{id, score}]
--    c. ids = [r.id for r in results]
--    d. records = SELECT * FROM memories WHERE id = ANY(ids) (PG)
--    e. return merge(records, scores)
