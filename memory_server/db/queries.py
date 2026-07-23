INSERT_MEMORY = """
    INSERT INTO memories (user_id, content, embedding, metadata, namespace)
    VALUES ($1, $2, $3::vector, $4::jsonb, $5)
    RETURNING id
"""

SELECT_MEMORY_BY_ID = """
    SELECT id, user_id, content, metadata, namespace, created_at, updated_at
    FROM memories
    WHERE id = $1
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
    WHERE user_id = $2
      AND ($3::text IS NULL OR namespace = $3)
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
    RETURNING id, user_id, content, metadata, namespace, created_at, updated_at
"""

DELETE_MEMORY = """
    DELETE FROM memories WHERE id = $1
    RETURNING id
"""

LIST_MEMORIES = """
    SELECT id, user_id, content, metadata, namespace, created_at, updated_at
    FROM memories
    WHERE ($1::text IS NULL OR user_id = $1)
      AND ($2::text IS NULL OR namespace = $2)
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
