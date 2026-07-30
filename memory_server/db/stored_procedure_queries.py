-- ============================================================
-- 009_stored_procedures_queries.py
-- ============================================================
-- SQL-вызовы хранимок для MemoryRepository.
-- Используются вместо инлайн-SQL из queries.py.
-- ============================================================


# ── 1. UPSERT ──────────────────────────────────────────────
# Вызов memory_upsert() — INSERT или UPDATE при дубле по content_hash.
# Возвращает (id, action).
CALL_MEMORY_UPSERT = """
    SELECT * FROM memory_upsert($1, $2, $3::vector, $4::jsonb, $5, $6, $7, $8)
"""


# ── 2. BATCH INSERT WITH DEDUP ────────────────────────────
# Вызов memory_insert_batch() — batch insert, пропускает дубли.
# Возвращает список id вставленных записей.
CALL_MEMORY_INSERT_BATCH = """
    SELECT * FROM memory_insert_batch($1, $2, $3, $4, $5, $6, $7, $8)
"""


# ── 3. SEMANTIC SEARCH ────────────────────────────────────
# Вызов memory_search_hnsw() — cosine search, совместимый с HNSW индексом.
CALL_MEMORY_SEARCH_HNSW = """
    SELECT * FROM memory_search_hnsw($1, $2, $3, $4, $5)
"""


# ── 4. GRAPH STATS UNIFIED ────────────────────────────────
# Вызов graph_stats_unified() — статистика графа одним запросом.
# Возвращает JSONB с общей статистикой, по namespace и по link_type.
CALL_GRAPH_STATS_UNIFIED = """
    SELECT * FROM graph_stats_unified()
"""


# ── 5. GRAPH TRAVERSE FULL ────────────────────────────────
# Вызов graph_traverse_full() — обход графа с полным возвратом нод и рёбер.
# Возвращает JSONB с nodes и edges.
CALL_GRAPH_TRAVERSE_FULL = """
    SELECT * FROM graph_traverse_full($1, $2, $3)
"""
