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
# last_accessed_at/access_count — для ранжирования D4 (Фаза 1.2).
_MEMORY_COLUMNS = """
    m.id, m.user_id, m.content, m.metadata, n.uid AS namespace, m.importance,
    m.created_at, m.updated_at, m.content_hash,
    m.project_id, m.status, m.confidence, m.valid_from, m.valid_to, m.ingested_at,
    m.supersedes, m.superseded_by, m.frozen, m.last_accessed_at, m.access_count
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

# Batch exact-dedup (Фаза 1.4): ОДИН запрос на весь батч пар (uid, hash)
# вместо цикла find_by_content_hash. unnest параллельными массивами —
# точное совпадение пары, не декартово произведение.
SELECT_MEMORY_BY_CONTENT_HASHES = f"""
    SELECT {_MEMORY_COLUMNS}, k.ns_uid, k.matched_hash
    FROM unnest($1::text[], $2::text[]) AS k(ns_uid, matched_hash)
    JOIN namespaces n ON n.uid = k.ns_uid
    JOIN memories m ON m.namespace_id = n.id AND m.content_hash = k.matched_hash
    WHERE m.status = 'asserted' AND m.valid_to IS NULL
"""

SELECT_MEMORY_BY_ENTITY_NAME = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.metadata->>'entity_name' = $1
    LIMIT 1
"""

# FTS: канал B гибридного поиска (Фаза 1.1) и fallback при недоступном Qdrant.
# Конфиг 'russian' — стемминг для основного корпуса памяти (кириллица);
# TODO(migration 021): GIN-индекс to_tsvector('russian', content) — сейчас
# выражение вычисляется на лету; заготовка migrations/021_phase1_search_fixes.sql.
SEARCH_MEMORIES = f"""
    SELECT
        {_MEMORY_COLUMNS},
        ts_rank(to_tsvector('russian', m.content), plainto_tsquery('russian', $1)) AS score
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE ($2::text IS NULL OR m.user_id = $2)
      AND ($3::uuid IS NULL OR m.namespace_id = $3)
      AND ($4::uuid IS NULL OR m.project_id = $4)
      AND ($6::bool OR (m.status = 'asserted' AND m.valid_to IS NULL))
      AND to_tsvector('russian', m.content) @@ plainto_tsquery('russian', $1)
    ORDER BY score DESC
    LIMIT $5
"""

# metadata — dict-merge (|| — shallow merge, новые ключи затирают старые),
# а не COALESCE-затирание всего JSONB. version инкрементит триггер
# trg_memories_version_bump (миграция 018) при изменении content.
# $9 content_hash: при обновлении content вызывающий слой ОБЯЗАН передать
# свежий sha256 — иначе рассинхрон поймает unique-индекс
# idx_memories_content_hash_active (020) на следующем UPDATE.
UPDATE_MEMORY = f"""
    UPDATE memories m
    SET content      = COALESCE($2, content),
        metadata     = CASE WHEN $3::jsonb IS NULL THEN metadata ELSE metadata || $3::jsonb END,
        importance   = COALESCE($4, importance),
        project_id   = COALESCE($5::uuid, project_id),
        confidence   = COALESCE($6, confidence),
        frozen       = COALESCE($7, frozen),
        supersedes   = COALESCE($8::uuid, supersedes),
        content_hash = COALESCE($9, content_hash),
        updated_at   = now()
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

# Новая версия гранулы (Фаза 2.1): user_id/namespace_id/project_id/version
# наследуются из старой строки INSERT-SELECT'ом — атомарнее и без лишних
# round-trip'ов; version = old.version + 1 хранится там же, где его
# инкрементит триггер 018. INSERT до SUPERSEDE: unique-индекс
# idx_memories_content_hash_active видит обе asserted-строки только при
# идентичном content_hash — этот случай отсекает service (ConflictError).
INSERT_MEMORY_VERSION = """
    INSERT INTO memories (
        user_id, content, metadata, namespace_id,
        content_hash, importance, project_id, confidence, frozen, supersedes, version
    )
    SELECT
        old.user_id, $2::text, $3::jsonb, old.namespace_id,
        $4::text, COALESCE($5::int, old.importance), old.project_id,
        $6::float4, false, old.id, old.version + 1
    FROM memories old
    WHERE old.id = $1::uuid
    RETURNING id, namespace_id
"""

# Supersession-цепочка в обе стороны (Фаза 2.1): назад по supersedes
# (dist < 0), вперёд по superseded_by (dist > 0); старт — 0. UNION + min
# дедупит стартовый узел, ORDER BY dist — от старейшей к новейшей.
# Миграций не требует: обычный запрос по колонкам 018.
# Рекурсивный шаг джойнится по next_id — у CTE НЕТ колонок supersedes/
# superseded_by (якорь переименовал их в next_id): b.supersedes в JOIN —
# UndefinedColumnError (регрессия приёмки Фазы 2).
GET_HISTORY = f"""
    WITH RECURSIVE
    backwards AS (
        SELECT id, supersedes AS next_id, 0 AS dist
        FROM memories WHERE id = $1::uuid
        UNION ALL
        SELECT m.id, m.supersedes, b.dist - 1
        FROM memories m JOIN backwards b ON m.id = b.next_id
    ),
    forwards AS (
        SELECT id, superseded_by AS next_id, 0 AS dist
        FROM memories WHERE id = $1::uuid
        UNION ALL
        SELECT m.id, m.superseded_by, f.dist + 1
        FROM memories m JOIN forwards f ON m.id = f.next_id
    ),
    chain AS (
        SELECT id, min(dist) AS dist
        FROM (
            SELECT id, dist FROM backwards
            UNION
            SELECT id, dist FROM forwards
        ) u
        GROUP BY id
    )
    SELECT {_MEMORY_COLUMNS}
    FROM chain c
    JOIN memories m ON m.id = c.id
    JOIN namespaces n ON n.id = m.namespace_id
    ORDER BY c.dist ASC, m.created_at ASC
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
# $2 = include_historical (time-travel, Фаза 1.3): True отключает фильтр
# актуальности — видны superseded/retracted версии. Семантика зеркалит
# $6 в SEARCH_MEMORIES; pg_repository.fetch_by_ids передаёт параметр
# напрямую, БЕЗ инверсии (regression: tests/test_repository.py,
# TestFetchByIdsSemantics).
FETCH_MEMORIES_BY_IDS = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.id = ANY($1::uuid[])
      AND ($2::bool OR (m.status = 'asserted' AND m.valid_to IS NULL))
"""

# Инкремент access-полей при выдаче (Фаза 1.2, D4): батч-UPDATE,
# вызывается ПОСЛЕ формирования выдачи вне транзакции чтения —
# только по фактически выданным id.
BUMP_ACCESS_MEMORIES = """
    UPDATE memories
    SET access_count = access_count + 1,
        last_accessed_at = now()
    WHERE id = ANY($1::uuid[])
"""

# Отзыв гранулы (бывший ARCHIVE_MEMORY → is_archived). $2 — причина
# (опционально): metadata.reason merge-ится, историю отзыва сохраняем
# в самой грануле (Фаза 2.1: единый путь retract для всех тулов).
RETRACT_MEMORY = """
    UPDATE memories
    SET status = 'retracted',
        valid_to = now(),
        updated_at = now(),
        metadata = CASE
            WHEN $2::text IS NULL THEN metadata
            ELSE metadata || jsonb_build_object('reason', $2::text)
        END
    WHERE id = $1 AND status = 'asserted'
    RETURNING id
"""


# ── Фаза 2.2: физический жизненный цикл (decay / stale / GC) ──

# Ежедневное затухание уверенности: один батч-SQL, без выборки в Python.
# rate per-namespace приходит из config.recency_decay_rates (unnest-массивы
# JOIN'ятся по uid — точное сопоставление пар, не декартово произведение);
# $3 — default-рейт для неймспейсов без override. frozen не трогаем (D4),
# ниже floor не сползаем — там зона mark_stale/ручной ревизии. GREATEST
# обязателен: WHERE confidence > floor не спасает от одношагового
# проседания (0.1005 × 0.995 = 0.0999975 < 0.1).
DECAY_CONFIDENCE = """
    UPDATE memories AS m
    SET confidence = GREATEST(m.confidence * COALESCE(r.rate, $3::float8), $4::float8),
        updated_at = now()
    FROM namespaces n
    LEFT JOIN unnest($1::text[], $2::float8[]) AS r(uid, rate) ON r.uid = n.uid
    WHERE m.namespace_id = n.id
      AND m.status = 'asserted'
      AND NOT m.frozen
      AND m.confidence > $4::float8
    RETURNING n.uid AS namespace
"""

# «Устаревшие» гранулы: статус НЕ меняем — только счётчик для лога/метрик
# (динамический критерий, колонки-флага нет by design).
COUNT_STALE = """
    SELECT count(*)::bigint
    FROM memories m
    WHERE m.status = 'asserted'
      AND m.confidence < $1::float8
      AND COALESCE(m.last_accessed_at, m.created_at) < now() - make_interval(days => $2::int)
"""

# Кандидаты на ревизию для memory_stale_list: тот же критерий + фильтры.
LIST_STALE = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.status = 'asserted'
      AND m.confidence < $1::float8
      AND COALESCE(m.last_accessed_at, m.created_at) < now() - make_interval(days => $2::int)
      AND ($3::text IS NULL OR m.user_id = $3)
      AND ($4::uuid IS NULL OR m.namespace_id = $4)
      AND ($5::uuid IS NULL OR m.project_id = $5)
    ORDER BY m.confidence ASC, m.created_at ASC
    LIMIT $6
"""

# GC superseded-версий (еженедельно): ТОЛЬКО с наследником (superseded_by
# IS NOT NULL — окно валидности живёт в цепочке) и старше retention.
# frozen не трогаем (D4): замороженный факт — вечный, GC его не жерёт.
SELECT_GC_SUPERSEDED = """
    SELECT id::text
    FROM memories
    WHERE status = 'superseded'
      AND superseded_by IS NOT NULL
      AND NOT frozen
      AND updated_at < now() - make_interval(days => $1::int)
"""

# Второй шаг GC: связи, целиком теряющие адрес после SET NULL
# (target_id удаляемой гранулы + нет target_name для soft-resolve),
# удаляем ДО memories — не оставляем полностью безадресных рёбер.
DELETE_GC_DANGLING_RELATIONS = """
    DELETE FROM relations
    WHERE target_id = ANY($1::uuid[])
      AND target_name IS NULL
    RETURNING id
"""

DELETE_GC_SUPERSEDED = """
    DELETE FROM memories
    WHERE id = ANY($1::uuid[])
      AND status = 'superseded'
      AND superseded_by IS NOT NULL
      AND NOT frozen
    RETURNING id::text
"""

# Orphans: несуществующих source/target по FK (005: CASCADE/SET NULL) не
# бывает; мусор — связи, полностью лишённые адреса после SET NULL.
DELETE_ORPHAN_RELATIONS = """
    DELETE FROM relations
    WHERE target_id IS NULL
      AND target_name IS NULL
    RETURNING id
"""


# ── Фаза 2.3 v2: кластеризация Level 2 (миграция 022 — Нора) ──
# Архитектура v2: кандидаты ищет Qdrant ANN (Python, MemoryRepository),
# SQL группирует готовые пары. Порог близости применяется на стороне
# Qdrant (score_threshold), сюда приходит уже отфильтрованное.

# Вход ANN-скролла: id актуальных гранул namespace (пачками → Qdrant).
SELECT_ASSERTED_CLUSTER_IDS = """
    SELECT id::text
    FROM memories
    WHERE namespace_id = $1::uuid
      AND status = 'asserted'
      AND valid_to IS NULL
"""

# Temp-таблица пар (паттерн _similarity_pairs из 016): создаётся в той же
# транзакции, что и вызов хранимки, — ON COMMIT DROP подчищает за собой.
CREATE_CLUSTER_PAIRS_TEMP = """
    CREATE TEMP TABLE _cluster_pairs (
        a_id       UUID NOT NULL,
        b_id       UUID NOT NULL,
        similarity REAL NOT NULL
    ) ON COMMIT DROP
"""

# Сигнатура — миграция 022 v2: assign_clusters_from_pairs(p_namespace_id
# UUID, p_min_members INT DEFAULT 2) RETURNS TABLE(cluster_id, member_count,
# coherence). До применения 022 — SchemaPendingError (graceful).
REFRESH_CLUSTERS = """
    SELECT * FROM assign_clusters_from_pairs($1::uuid, $2::int)
"""

LIST_CLUSTERS = """
    SELECT c.id::text, n.uid AS namespace, c.label, c.summary,
           c.member_count, c.coherence, c.last_computed_at
    FROM clusters c
    JOIN namespaces n ON n.id = c.namespace_id
    WHERE ($1::text IS NULL OR n.uid = $1)
      AND ($2::uuid IS NULL OR EXISTS (
          SELECT 1 FROM memories mm
          WHERE mm.cluster_id = c.id AND mm.project_id = $2::uuid
      ))
    ORDER BY c.member_count DESC
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
