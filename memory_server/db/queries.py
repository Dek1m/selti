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

# V3.0 (дыра 5 ADR-019): фильтр актуальности + детерминированный ORDER BY.
# Без него LIMIT 1 без сортировки мог вернуть superseded/retracted версию —
# ребро прилипало к трупу. Приоритет: вечный факт → важнее → свежее по
# доступу → свежее по созданию. Вызыватель один — _resolve_granule
# (add_relation), ему нужна именно актуальная гранула.
SELECT_MEMORY_BY_ENTITY_NAME = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.metadata->>'entity_name' = $1
      AND m.status = 'asserted' AND m.valid_to IS NULL
    ORDER BY m.frozen DESC,
             m.importance DESC,
             m.last_accessed_at DESC NULLS LAST,
             m.created_at DESC
    LIMIT 1
"""

# FTS: канал B гибридного поиска (Фаза 1.1) и fallback при недоступном Qdrant.
# Конфиг 'russian' — стемминг для основного корпуса памяти (кириллица);
# TODO(migration 021): GIN-индекс to_tsvector('russian', content) — сейчас
# выражение вычисляется на лету; заготовка migrations/021_phase1_search_fixes.sql.
# $10 = entity_type (Фаза 5.2): фильтр по metadata->>'entity_type', применяется
# до LIMIT — профильный канал не тратит бюджет пулла на чужие типы.
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
      AND ($7::timestamptz IS NULL OR m.created_at >= $7::timestamptz)
      AND ($8::timestamptz IS NULL OR m.created_at <= $8::timestamptz)
      AND ($9::text IS NULL OR m.status = $9::text)
      AND ($10::text IS NULL OR m.metadata->>'entity_type' = $10::text)
    ORDER BY score DESC
    LIMIT $5
"""

# metadata — dict-merge (|| — shallow merge, новые ключи затирают старые),
# а не COALESCE-затирание всего JSONB. version инкрементит триггер
# trg_memories_version_bump (миграция 018) при изменении content.
# V3.0 (E.2 ADR-019): ветки content/content_hash УДАЛЕНЫ — контент
# неизменяем на месте, путь перезаписи отсутствует в слое данных; правка
# факта = новая версия (service.update → create_version). Правки обвязки
# (metadata/importance/confidence/frozen/project_id/supersedes) остаются.
UPDATE_MEMORY = f"""
    UPDATE memories m
    SET metadata     = CASE WHEN $2::jsonb IS NULL THEN metadata ELSE metadata || $2::jsonb END,
        importance   = COALESCE($3, importance),
        project_id   = CASE WHEN $7::bool THEN NULL ELSE COALESCE($4::uuid, project_id) END,
        confidence   = COALESCE($5, confidence),
        frozen       = COALESCE($6, frozen),
        supersedes   = COALESCE($8::uuid, supersedes),
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
# V3.1 (дыра 2 ADR-019): cluster_id наследуется — версия не выпадает из
# кластера Level 2 до следующего refresh_clusters.
INSERT_MEMORY_VERSION = """
    INSERT INTO memories (
        user_id, content, metadata, namespace_id,
        content_hash, importance, project_id, confidence, frozen, supersedes, version, cluster_id
    )
    SELECT
        old.user_id, $2::text, $3::jsonb, old.namespace_id,
        $4::text, COALESCE($5::int, old.importance), old.project_id,
        $6::float4, false, old.id, old.version + 1, old.cluster_id
    FROM memories old
    WHERE old.id = $1::uuid
    RETURNING id, namespace_id
"""

# REWIRE — наследование рёбер при supersede (V3.1, дыра 1 ADR-019):
# рёбра старой версии переезжают на наследника той же транзакцией.
# Правила:
#   * link_type='supersedes' не переносится — структурная связь версий;
#   * рёбра с мёртвой второй стороной остаются на старой (история:
#     труп-трупу ребро ещё что-то значит, наследнику — нет);
#   * висячий конец (target_id IS NULL, soft-resolve по имени) переносится —
#     его вторая сторона не мёртвая, а неизвестная;
#   * дубликат (та же пара + тип уже на новой) не создаётся — UPDATE не
#     может нарушить unique-тройку (source_id, target_id, link_type);
#   * inherited_from = old.id — колонка происхождения (миграция 023):
#     исторический граф восстанавливает физическое место ребра проекцией.
REWIRE_RELATIONS_SOURCE = """
    UPDATE relations r
    SET source_id = $2::uuid,
        inherited_from = $1::uuid
    WHERE r.source_id = $1::uuid
      AND r.link_type <> 'supersedes'
      AND (r.target_id IS NULL OR EXISTS (
          SELECT 1 FROM memories m
          WHERE m.id = r.target_id
            AND m.status = 'asserted' AND m.valid_to IS NULL
      ))
      AND NOT EXISTS (
          SELECT 1 FROM relations x
          WHERE x.source_id = $2::uuid
            AND x.link_type = r.link_type
            AND x.target_id IS NOT DISTINCT FROM r.target_id
      )
    RETURNING r.id
"""

REWIRE_RELATIONS_TARGET = """
    UPDATE relations r
    SET target_id = $2::uuid,
        inherited_from = $1::uuid
    WHERE r.target_id = $1::uuid
      AND r.link_type <> 'supersedes'
      AND EXISTS (
          SELECT 1 FROM memories m
          WHERE m.id = r.source_id
            AND m.status = 'asserted' AND m.valid_to IS NULL
      )
      AND NOT EXISTS (
          SELECT 1 FROM relations x
          WHERE x.target_id = $2::uuid
            AND x.source_id = r.source_id
            AND x.link_type = r.link_type
      )
    RETURNING r.id
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
# закрывается now(). CTE — один round-trip. $3 — опциональный срез по проекту
# (Фаза 3.1: забыть знания юзера в рамках проекта, глобальный слой не трогаем).
FORGET_MEMORIES = """
    WITH retracted AS (
        UPDATE memories
        SET status = 'retracted', valid_to = now(), updated_at = now()
        WHERE user_id = $1
          AND status = 'asserted'
          AND ($2::uuid IS NULL OR namespace_id = $2)
          AND ($3::uuid IS NULL OR project_id = $3)
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
      AND ($2::uuid IS NULL OR m.project_id = $2)
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
# $3/$4/$5 = REST-фильтры /api/search (Фаза 5.1): created_at window и
# точный статус, применяются к кандидатам ДО RRF-fusion. NULL = выключен;
# explicit casts обязательны — PG не выводит тип NULL-параметра.
# $6 = entity_type (Фаза 5.2): точный тип сущности из metadata.
FETCH_MEMORIES_BY_IDS = f"""
    SELECT {_MEMORY_COLUMNS}
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.id = ANY($1::uuid[])
      AND ($2::bool OR (m.status = 'asserted' AND m.valid_to IS NULL))
      AND ($3::timestamptz IS NULL OR m.created_at >= $3::timestamptz)
      AND ($4::timestamptz IS NULL OR m.created_at <= $4::timestamptz)
      AND ($5::text IS NULL OR m.status = $5::text)
      AND ($6::text IS NULL OR m.metadata->>'entity_type' = $6::text)
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
    SELECT id, source_id, target_id, target_name, link_type, description, weight, metadata, inherited_from, created_at
    FROM relations
    WHERE source_id = $1
      AND ($2::text IS NULL OR link_type = $2)
    ORDER BY created_at DESC
"""

SELECT_RELATIONS_BY_TARGET = """
    SELECT id, source_id, target_id, target_name, link_type, description, weight, metadata, inherited_from, created_at
    FROM relations
    WHERE target_id = $1
      AND ($2::text IS NULL OR link_type = $2)
    ORDER BY created_at DESC
"""

# Обогащение соседей связей (Фаза 5.2 веб-морды): один батч-SELECT на все
# концы рёбер — цвет слоя, подпись (entity_name → голова content) и размер
# (importance) без точечных чтений на каждого соседа.
GET_NEIGHBORS_INFO = """
    SELECT m.id, n.uid AS namespace, m.importance,
           m.metadata->>'entity_name' AS entity_name,
           left(m.content, 140) AS content
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.id = ANY($1::uuid[])
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
    SELECT id, source_id, target_id, target_name, link_type, description, weight, metadata, inherited_from, created_at
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
#
# V3.2 (ADR-019 H / ADR-017 A.1, фаза 1): не-UUID цель резолвится
# lateral-JOIN'ом по entity_name ДО вставки ребра. Приоритет кандидата:
# (1) свой проект (project_id совпадает с источником), (2) глобальный слой
# (project_id IS NULL), (3) свежейшая created_at; только актуальные
# (status='asserted' AND valid_to IS NULL) — ребро не прилипает к трупу.
# Выражение m2.metadata->>'entity_name' обслуживает idx_memories_entity_name (018).
# Резолвнутое имя: target_id заполняется, target_name СОХРАНЯЕТСЯ как
# происхождение (наблюдаемость резолва: target_id+target_name = разрешённое).
_LATERAL_RESOLVE_TARGET = """
    LEFT JOIN LATERAL (
        SELECT m2.id AS resolved_id
        FROM memories m2
        WHERE m2.metadata->>'entity_name' = link->>'target'
          AND m2.status = 'asserted' AND m2.valid_to IS NULL
          AND (m.project_id IS NOT DISTINCT FROM m2.project_id
               OR m2.project_id IS NULL)
        ORDER BY m2.project_id IS NULL, m2.created_at DESC
        LIMIT 1
    ) res ON true
"""

BACKFILL_RELATIONS_FROM_METADATA = f"""
    WITH source_links AS (
        SELECT
            m.id AS source_id,
            link->>'type' AS link_type,
            link->>'target' AS target_str,
            link->>'description' AS description,
            CASE
                WHEN link->>'target' ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
                THEN (link->>'target')::uuid
                ELSE NULL
            END AS uuid_target,
            res.resolved_id
        FROM memories m,
             jsonb_array_elements(m.metadata->'links') AS link
             {_LATERAL_RESOLVE_TARGET}
        WHERE m.id = $1
          AND m.metadata->'links' IS NOT NULL
          AND jsonb_array_length(m.metadata->'links') > 0
    )
    INSERT INTO relations (source_id, target_id, target_name, link_type, description, weight, metadata)
    SELECT
        sl.source_id,
        COALESCE(sl.uuid_target, sl.resolved_id),
        CASE WHEN sl.uuid_target IS NOT NULL THEN NULL ELSE sl.target_str END,
        sl.link_type,
        sl.description,
        1.0,
        '{{"synced_from": "metadata.links"}}'::jsonb
    FROM source_links sl
    WHERE sl.link_type IS NOT NULL
      AND (COALESCE(sl.uuid_target, sl.resolved_id) IS NULL
           OR EXISTS (SELECT 1 FROM memories WHERE id = COALESCE(sl.uuid_target, sl.resolved_id)))
    ON CONFLICT (source_id, target_id, link_type) WHERE target_id IS NOT NULL
    DO UPDATE SET
        description = EXCLUDED.description,
        weight = EXCLUDED.weight
    RETURNING id, (target_name IS NOT NULL AND target_id IS NOT NULL) AS resolved_by_name
"""

# Удалить metadata-based связи для гранулы (source_id = $1)
# Удаляются связи с пометкой synced_from = 'metadata.links'
# Ручные связи (без пометки) сохраняются
DELETE_SYNCED_RELATIONS = """
    DELETE FROM relations
    WHERE source_id = $1
      AND metadata->>'synced_from' = 'metadata.links'
"""

SYNC_LINKS_BATCH = f"""
    WITH source_links AS (
        SELECT
            m.id AS source_id,
            link->>'type' AS link_type,
            link->>'target' AS target_str,
            link->>'description' AS description,
            CASE
                WHEN link->>'target' ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
                THEN (link->>'target')::uuid
                ELSE NULL
            END AS uuid_target,
            res.resolved_id
        FROM memories m,
             jsonb_array_elements(m.metadata->'links') AS link
             {_LATERAL_RESOLVE_TARGET}
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
        COALESCE(sl.uuid_target, sl.resolved_id),
        CASE WHEN sl.uuid_target IS NOT NULL THEN NULL ELSE sl.target_str END,
        sl.link_type,
        sl.description,
        1.0,
        '{{"synced_from": "metadata.links"}}'::jsonb
    FROM source_links sl
    WHERE sl.link_type IS NOT NULL
      AND (COALESCE(sl.uuid_target, sl.resolved_id) IS NULL
           OR EXISTS (SELECT 1 FROM memories WHERE id = COALESCE(sl.uuid_target, sl.resolved_id)))
    ON CONFLICT (source_id, target_id, link_type) WHERE target_id IS NOT NULL
    DO UPDATE SET
        description = EXCLUDED.description,
        weight = EXCLUDED.weight,
        metadata = EXCLUDED.metadata
    RETURNING id, (target_name IS NOT NULL AND target_id IS NOT NULL) AS resolved_by_name
"""

# ── Линкер V3 (ADR-019 C): name_reconciler / co-occurrence / L1a / L2 ──

# Кампания name_reconciler (V3.2): батчевый резолв висячих target_name тем же
# приоритетом, что и lateral в sync (свой проект → глобальный → свежейшая).
# NOT EXISTS-гарда: резолв не создаёт дубль под partial unique
# (source_id, target_id, link_type) — если каноничное ребро уже есть, висяк
# остаётся на разбор (виден в отчёте как pending).
_RESOLVE_PENDING_TARGET_NAMES_CTE = """
    WITH pending AS MATERIALIZED (
        -- candidates-first (баг приёмки В4): резолвимость проверяется ДО
        -- LIMIT, иначе 500 старейших нерезолвимых навсегда замораживают
        -- кампанию (head-of-line blocking).
        SELECT r.id AS relation_id
        FROM relations r
        JOIN memories src ON src.id = r.source_id
        JOIN LATERAL (
            SELECT m2.id
            FROM memories m2
            WHERE m2.metadata->>'entity_name' = r.target_name
              AND m2.status = 'asserted' AND m2.valid_to IS NULL
              AND (src.project_id IS NOT DISTINCT FROM m2.project_id
                   OR m2.project_id IS NULL)
            ORDER BY m2.project_id IS NULL, m2.created_at DESC
            LIMIT 1
        ) resolvable ON true
        WHERE r.target_id IS NULL
          AND r.target_name IS NOT NULL
        ORDER BY r.created_at
        LIMIT $1
    ),
    candidates AS (
        SELECT p.relation_id, cand.id AS target_id
        FROM pending p
        JOIN relations r ON r.id = p.relation_id
        JOIN memories src ON src.id = r.source_id
        JOIN LATERAL (
            SELECT m2.id
            FROM memories m2
            WHERE m2.metadata->>'entity_name' = r.target_name
              AND m2.status = 'asserted' AND m2.valid_to IS NULL
              AND (src.project_id IS NOT DISTINCT FROM m2.project_id
                   OR m2.project_id IS NULL)
            ORDER BY m2.project_id IS NULL, m2.created_at DESC
            LIMIT 1
        ) cand ON true
        WHERE NOT EXISTS (
            SELECT 1 FROM relations dup
            WHERE dup.source_id = r.source_id
              AND dup.target_id = cand.id
              AND dup.link_type = r.link_type
              AND dup.id <> r.id
        )
    )
"""

# dry_run: только счёт (WHERE-цепочка идентична боевому UPDATE)
RESOLVE_PENDING_TARGET_NAMES_DRY = (
    _RESOLVE_PENDING_TARGET_NAMES_CTE
    + """
    SELECT count(*) AS resolved FROM candidates
    """
)

RESOLVE_PENDING_TARGET_NAMES = (
    _RESOLVE_PENDING_TARGET_NAMES_CTE
    + """
    UPDATE relations r
    SET target_id = c.target_id,
        metadata = r.metadata || jsonb_build_object('resolved_by', 'name_reconciler')
    FROM candidates c
    WHERE r.id = c.relation_id
    RETURNING r.id
    """
)

# Счётчик очереди для отчёта кампании и memory_linker_stats.
COUNT_PENDING_TARGET_NAMES = """
    SELECT count(*) AS pending
    FROM relations
    WHERE target_id IS NULL AND target_name IS NOT NULL
"""

# Co-occurrence L1c (V3.2): соседи той же сессии (project+namespace+session_id)
# → related_to 0.5. Однонаправленно от обрабатываемой гранулы к свежим соседям;
# NOT EXISTS гасит оба направления (не плодим встречные related_to-дубли).
INSERT_COOCCURRENCE_LINKS = """
    INSERT INTO relations (source_id, target_id, link_type, weight, metadata)
    SELECT $1, n.id, 'related_to', 0.5,
           jsonb_build_object('source', 'linker_v3', 'layer', 'l1c',
                              'session_id', $4)
    FROM (
        SELECT m.id
        FROM memories m
        WHERE m.project_id IS NOT DISTINCT FROM $2::uuid
          AND m.namespace_id = $3::uuid
          AND m.metadata->>'session_id' = $4
          AND m.id <> $1::uuid
          AND m.status = 'asserted' AND m.valid_to IS NULL
        ORDER BY m.created_at DESC
        LIMIT $5
    ) n
    WHERE NOT EXISTS (
        SELECT 1 FROM relations r
        WHERE r.link_type = 'related_to'
          AND ((r.source_id = $1::uuid AND r.target_id = n.id)
               OR (r.source_id = n.id AND r.target_id = $1::uuid))
    )
    ON CONFLICT (source_id, target_id, link_type) WHERE target_id IS NOT NULL
    DO NOTHING
    RETURNING id
"""

# Beat-кампания co-occurrence: гранулы с session_id, БЕЗ l1c-рёбер и с живыми
# соседями (гранулы без соседей не крутятся в выборке вечно — идемпотентность).
SELECT_COOCCURRENCE_CANDIDATES = """
    SELECT m.id::text, m.project_id, m.namespace_id::text,
           m.metadata->>'session_id' AS session_id
    FROM memories m
    WHERE m.metadata->>'session_id' IS NOT NULL
      AND m.status = 'asserted' AND m.valid_to IS NULL
      AND NOT EXISTS (
          -- Любые l1c (в обе стороны): INSERT гасит встречные пары, поэтому
          -- гранула с полностью покрытыми соседями никогда не получит
          -- исходящих l1c и без этой Symmetric-гarder крутилась бы в
          -- выборке вечно (баг приёмки М6).
          SELECT 1 FROM relations r
          WHERE r.metadata->>'layer' = 'l1c'
            AND (r.source_id = m.id OR r.target_id = m.id)
      )
      AND EXISTS (
          SELECT 1 FROM memories m2
          WHERE m2.project_id IS NOT DISTINCT FROM m.project_id
            AND m2.namespace_id = m.namespace_id
            AND m2.metadata->>'session_id' = m.metadata->>'session_id'
            AND m2.id <> m.id
            AND m2.status = 'asserted' AND m2.valid_to IS NULL
      )
    ORDER BY m.created_at DESC
    LIMIT $1
"""

# Источник/кандидаты L2: контент с заголовком (entity_name) и хэшем для
# verdict-cache — один батч-SELECT на всех участников вердикта.
SELECT_GRANULES_FOR_LINKER = """
    SELECT m.id::text, m.content, m.metadata->>'entity_name' AS entity_name,
           m.content_hash, n.uid AS namespace
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.id = ANY($1::uuid[])
      AND m.status = 'asserted' AND m.valid_to IS NULL
"""

# Карточка новой гранулы для link_new_granule: uid (граница зоны дедупа
# ключуется uid) + namespace_id (Qdrant-фильтр) + сессия (L1c).
SELECT_NEW_GRANULE_FOR_LINKER = """
    SELECT n.uid AS ns_uid, m.namespace_id::text AS ns_id,
           m.metadata->>'session_id' AS sid, m.project_id
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.id = $1::uuid
      AND m.status = 'asserted' AND m.valid_to IS NULL
"""

# Автосвязь линкера: DO NOTHING — линкер никогда не перезаписывает ручные
# и Тишины рёбра (владение по metadata.source, ADR-019 C).
INSERT_LINKER_RELATION = """
    INSERT INTO relations (source_id, target_id, link_type, description, weight, metadata)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
    ON CONFLICT (source_id, target_id, link_type) WHERE target_id IS NOT NULL
    DO NOTHING
    RETURNING id
"""

# memory_linker_stats (ADR-019 G): рёбра линкера по слоям.
SELECT_LINKER_LINK_STATS = """
    SELECT metadata->>'layer' AS layer, count(*) AS count
    FROM relations
    WHERE metadata->>'source' = 'linker_v3'
    GROUP BY 1
"""

# memory_linker_stats: судьба имён (resolved = target_id+target_name —
# резолв сохраняет имя как происхождение; pending = висячие).
SELECT_LINKER_NAME_STATS = """
    SELECT count(*) FILTER (WHERE target_id IS NOT NULL) AS resolved,
           count(*) FILTER (WHERE target_id IS NULL) AS pending
    FROM relations
    WHERE target_name IS NOT NULL
"""

# ═══════════════════════════════════════════════════════════════
# PROJECTS REGISTRY CRUD (Фаза 5.1, /api/projects; схема — миграция 017)
# ═══════════════════════════════════════════════════════════════

_PROJECT_CARD_COLUMNS = """
    id::text, slug, name, description, kind, status, local_path,
    repo_url, docs_url, homepage_url, default_branch, created_at, updated_at
"""

INSERT_PROJECT = f"""
    INSERT INTO projects (
        slug, name, description, kind, status, local_path,
        repo_url, docs_url, homepage_url, default_branch
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    RETURNING {_PROJECT_CARD_COLUMNS}
"""

# Partial update: NULL-параметр = поле не меняется (PATCH-семантика REST).
UPDATE_PROJECT = f"""
    UPDATE projects SET
        name = COALESCE($2, name),
        description = COALESCE($3, description),
        kind = COALESCE($4, kind),
        status = COALESCE($5, status),
        local_path = COALESCE($6, local_path),
        repo_url = COALESCE($7, repo_url),
        docs_url = COALESCE($8, docs_url),
        homepage_url = COALESCE($9, homepage_url),
        default_branch = COALESCE($10, default_branch),
        updated_at = now()
    WHERE slug = $1
    RETURNING {_PROJECT_CARD_COLUMNS}
"""

SELECT_PROJECT_CARD = f"""
    SELECT {_PROJECT_CARD_COLUMNS}
    FROM projects
    WHERE slug = $1
"""

# asyncpg не исполняет несколько стейтментов одним prepared statement —
# replace-операции разбиты на пары DELETE+INSERT, атомарность даёт
# conn.transaction() в репозитории.
DELETE_PROJECT_LINKS = "DELETE FROM project_links WHERE project_id = $1::uuid"

INSERT_PROJECT_LINKS = """
    INSERT INTO project_links (project_id, link_type, url, title)
    SELECT $1::uuid, t.link_type, t.url, t.title
    FROM unnest($2::text[], $3::text[], $4::text[]) AS t(link_type, url, title)
"""

DELETE_PROJECT_TECHNOLOGIES = "DELETE FROM project_technologies WHERE project_id = $1::uuid"

# Словарь technologies — глобальный: существующие записи не переписываем
# (ON CONFLICT DO NOTHING), привязка через project_technologies.
UPSERT_TECHNOLOGIES = """
    INSERT INTO technologies (name, category, docs_url)
    SELECT t.name, t.category, t.docs_url
    FROM unnest($1::text[], $2::text[], $3::text[]) AS t(name, category, docs_url)
    ON CONFLICT (name) DO NOTHING
"""

INSERT_PROJECT_TECHNOLOGIES = """
    INSERT INTO project_technologies (project_id, technology_id, version, purpose)
    SELECT $1::uuid, tech.id, t.version, t.purpose
    FROM unnest($2::text[], $3::text[], $4::text[]) AS t(name, version, purpose)
    JOIN technologies tech ON tech.name = t.name
"""


# ── Полная карта 3D (PLAN_FULL_MAP_3D M1/M2) ──
# Снапшот /api/map/full: только актуальные гранулы (asserted + valid_to IS
# NULL), рёбра — только резолвленные (target_id IS NOT NULL) между гранулами
# снапшота. Кластерный уровень — таблица clusters (022). ORDER BY id —
# детерминизм columnar-массивов: одинаковый корпус = одинаковые индексы.

# Version-hash и счётчики меты: дешёвые агрегаты одним запросом.
# relations не имеет updated_at (005) — маркер свежести рёбер created_at,
# причём ТОЛЬКО по резолвленным (фикс F4): висячие (target_id IS NULL) в
# карту не входят, их создание не должно инвалилировать снапшот.
# map_layout вынесена в MAP_LAYOUT_VERSION: до применения 024 таблицы нет,
# основной запрос не должен падать (layout_rev=0, layout_at=NULL).
MAP_VERSION_SQL = """
    SELECT
        (SELECT count(*) FROM memories
          WHERE status = 'asserted' AND valid_to IS NULL)              AS node_count,
        (SELECT count(*) FROM relations WHERE target_id IS NOT NULL)   AS edge_count,
        (SELECT count(*) FROM clusters)                                 AS cluster_count,
        (SELECT max(updated_at) FROM memories)                          AS mem_updated,
        (SELECT max(created_at) FROM relations
          WHERE target_id IS NOT NULL)                                  AS rel_created
"""

# Отдельно от MAP_VERSION_SQL: existence map_layout проверяет вызывающий
# (to_regclass), при отсутствии таблицы — DEFAULT'ы.
MAP_LAYOUT_EXISTS_SQL = "SELECT to_regclass('public.map_layout') IS NOT NULL"

MAP_LAYOUT_VERSION_SQL = """
    SELECT max(rev) AS layout_rev, max(updated_at) AS layout_at
    FROM map_layout
"""

MAP_LAYOUT_NEXT_REV_SQL = "SELECT coalesce(max(rev), 0) + 1 FROM map_layout"

# Узлы снапшота: LEFT JOIN map_layout — координаты NULL до первого прогона
# layout_map (сборка подставит сферический fallback, M1).
MAP_NODES_SQL = """
    SELECT m.id::text,
           m.metadata->>'entity_name' AS entity_name,
           m.content,
           n.uid AS namespace,
           m.cluster_id::text,
           m.importance,
           m.frozen,
           ml.x, ml.y, ml.z
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    LEFT JOIN map_layout ml ON ml.node_id = m.id
    WHERE m.status = 'asserted' AND m.valid_to IS NULL
      AND ($1::text IS NULL OR n.uid = $1::text)
      AND ($2::uuid IS NULL OR m.project_id = $2::uuid)
    ORDER BY m.id
"""

# Тот же SELECT до применения 024 (фикс F2 приёмки): map_layout не существует,
# LEFT JOIN падает на parse — литеральные NULL держат форму строк совместимой
# (x/y/z = None → сферический fallback сборки), /full отвечает 200.
MAP_NODES_NO_LAYOUT_SQL = """
    SELECT m.id::text,
           m.metadata->>'entity_name' AS entity_name,
           m.content,
           n.uid AS namespace,
           m.cluster_id::text,
           m.importance,
           m.frozen,
           NULL::real AS x, NULL::real AS y, NULL::real AS z
    FROM memories m
    JOIN namespaces n ON n.id = m.namespace_id
    WHERE m.status = 'asserted' AND m.valid_to IS NULL
      AND ($1::text IS NULL OR n.uid = $1::text)
      AND ($2::uuid IS NULL OR m.project_id = $2::uuid)
    ORDER BY m.id
"""

# Рёбра снапшота: обе стороны обязаны пройти фильтры узлов (иначе разрыв
# индексов). Джойны namespaces ×2 только ради namespace-фильтра — на билде
# под lock это дешевле дублирования логики фильтра в Python.
MAP_EDGES_SQL = """
    SELECT r.source_id::text, r.target_id::text, r.link_type, r.weight
    FROM relations r
    JOIN memories s ON s.id = r.source_id
    JOIN namespaces sn ON sn.id = s.namespace_id
    JOIN memories t ON t.id = r.target_id
    JOIN namespaces tn ON tn.id = t.namespace_id
    WHERE r.target_id IS NOT NULL
      AND s.status = 'asserted' AND s.valid_to IS NULL
      AND t.status = 'asserted' AND t.valid_to IS NULL
      AND ($1::text IS NULL OR (sn.uid = $1::text AND tn.uid = $1::text))
      AND ($2::uuid IS NULL OR (s.project_id = $2::uuid AND t.project_id = $2::uuid))
"""

# Метаданные кластеров снапшота; финальное сужение до используемых — в Python.
MAP_CLUSTERS_SQL = """
    SELECT c.id::text, n.uid AS namespace, c.label, c.member_count
    FROM clusters c
    JOIN namespaces n ON n.id = c.namespace_id
    WHERE ($1::text IS NULL OR n.uid = $1::text)
    ORDER BY c.id
"""

# Вход раскладки layout_map: узлы + рёбра графа (без контента — только
# топология, веса и кластеры для fallback/аналитики).
MAP_LAYOUT_NODES_SQL = """
    SELECT m.id::text, m.cluster_id::text
    FROM memories m
    WHERE m.status = 'asserted' AND m.valid_to IS NULL
    ORDER BY m.id
"""

MAP_LAYOUT_EDGES_SQL = """
    SELECT r.source_id::text, r.target_id::text, r.weight
    FROM relations r
    JOIN memories s ON s.id = r.source_id
    JOIN memories t ON t.id = r.target_id
    WHERE r.target_id IS NOT NULL
      AND s.status = 'asserted' AND s.valid_to IS NULL
      AND t.status = 'asserted' AND t.valid_to IS NULL
"""

# Seeding: сохранённые координаты прошлой раскладки (карта «дышит», а не
# перетасовывается при живом reconciler).
MAP_LAYOUT_EXISTING_SQL = """
    SELECT node_id::text, x, y, z
    FROM map_layout
"""

# Bulk-UPSERT раскладки одним запросом (unnest параллельными массивами):
# rev — глобальный номер прогона, +1 от максимума читает вызывающий.
MAP_LAYOUT_UPSERT_SQL = """
    INSERT INTO map_layout (node_id, x, y, z, rev)
    SELECT u.node_id, u.x, u.y, u.z, $5::int
    FROM unnest($1::uuid[], $2::real[], $3::real[], $4::real[]) AS u(node_id, x, y, z)
    ON CONFLICT (node_id) DO UPDATE SET
        x = EXCLUDED.x, y = EXCLUDED.y, z = EXCLUDED.z,
        rev = EXCLUDED.rev, updated_at = now()
"""
