-- ============================================================
-- 023_memory_v3_core.sql — Memory V3: наследование графа и time-travel
-- ============================================================
-- Дата: 2026-09-20
-- Исполнитель: Сона (по ADR-019-memory-v3-versions-autolinker.md,
-- фазы V3.0/V3.1; решение B — гибрид: материальный перенос рёбер
-- + проекция time-travel, решение F — GC-стоп-кран)
--
-- Содержимое:
--   1) Индекс idx_memories_superseded_by — HEAD-резолв цепочки и backfill;
--   2) relations.inherited_from UUID → memories(id) ON DELETE SET NULL —
--      колонка происхождения ребра (от какой версии унаследовано при
--      REWIRE); NULL = ребро создано на этой грани, не переносилось;
--      partial-индекс под OR-lookup в graph_traverse_full v3;
--   3) graph_traverse_full v3 — параметр p_as_of TIMESTAMPTZ DEFAULT now():
--        * as_of = now() (дефолт, все текущие клиенты): нода включается
--          только живым окном (valid_to IS NULL == asserted — инвариант
--          схемы: статус меняется ровно при закрытии окна) — ФИКС ДЫРЫ 4
--          (мёртвые версии больше не в выдаче обхода);
--        * as_of в прошлом — исторический граф: окно ноды покрывает as_of;
--          рёбра, перенесённые REWIRE на наследника ПОСЛЕ as_of,
--          проектируются обратно на inherited_from-версию (её окно как раз
--          покрывает as_of). Хранения второго графа нет — окна уже в
--          строках, происхождение в inherited_from.
--      Сигнатура расширена trailing-DEFAULT — вызов с тремя аргументами
--      (queries.py TRAVERSE_FULL) работает без изменений.
--   4) Backfill A — наследование задним числом для существующих superseded:
--      перенос рёбер теми же правилами, что и runtime-REWIRE
--      (queries.py REWIRE_RELATIONS_SOURCE/TARGET) + наследование
--      cluster_id; отчёт RAISE NOTICE в лог миграции.
--
-- Не в миграции (знание приложения, не БД): GC-режимы (config.py —
-- стоп-кран выше любых режимов), metadata-обвязка версий.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Индекс superseded_by
-- ════════════════════════════════════════════════════════════
-- HEAD-резолв цепочки (для кого старая — труп) и backfill-обход.
-- Partial: подавляющее большинство строк не версии — не индексируем.

CREATE INDEX IF NOT EXISTS idx_memories_superseded_by
    ON memories (superseded_by)
    WHERE superseded_by IS NOT NULL;

COMMENT ON INDEX idx_memories_superseded_by IS 'Наследник superseded-версии (ADR-019): HEAD-резолв цепочки и backfill-перенос рёбер.';

-- ════════════════════════════════════════════════════════════
-- 2. relations.inherited_from — происхождение ребра
-- ════════════════════════════════════════════════════════════
-- REWIRE (queries.py) ставит old.id при переносе ребра на наследника.
-- ON DELETE SET NULL: при будущем GC hard-delete трупа перенесённое ребро
-- живёт дальше (источник происхождения теряется, связь — нет);
-- CASCADE здесь был бы миной (дыра 7 наоборот).

ALTER TABLE relations ADD COLUMN IF NOT EXISTS inherited_from UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_relations_inherited_from'
          AND conrelid = 'relations'::regclass
    ) THEN
        ALTER TABLE relations ADD CONSTRAINT fk_relations_inherited_from
            FOREIGN KEY (inherited_from) REFERENCES memories(id)
            ON DELETE SET NULL;
    END IF;
END;
$$;

COMMENT ON COLUMN relations.inherited_from IS 'Происхождение ребра (ADR-019, REWIRE): id версии, с которой ребро перенесено на наследника при supersede. NULL = ребро не переносилось (создано на этой грани). Исторический граф graph_traverse_full(p_as_of) проектирует по нему ребро обратно на старую версию.';

-- Partial-индекс под LATERAL OR-lookup обхода: (source_id = нода OR
-- inherited_from = нода) — BitmapOr по двум индексам, стоимость обхода
-- пропорциональна степени узла, а не корпусу рёбер.
CREATE INDEX IF NOT EXISTS idx_relations_inherited_from
    ON relations (inherited_from)
    WHERE inherited_from IS NOT NULL;

-- ════════════════════════════════════════════════════════════
-- 3. graph_traverse_full v3
-- ════════════════════════════════════════════════════════════
-- Отличия от 021:
--   * фильтр окон нод (дыра 4): якорь и каждый шаг проверяют
--     valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of);
--     при as_of = now() вырождается в «только живые»;
--   * проекция inherited_from: eff_endpoint = CASE WHEN у ребра есть
--     происхождение И его физический конец «моложе» as_of (перенос ещё
--     не случился) THEN inherited_from ELSE физический конец END —
--     время привязки ребра к концу = valid_from этого конца, а не
--     created_at ребра (UPDATE-перенос created_at не меняет);
--   * JOIN рёбер ноды: физические (source_id = нода) ∪ унаследованные
--     (inherited_from = нода, физически на наследнике) — LATERAL,
--     индексные lookup'ы;
--   * LANGUAGE sql сохранён (тело парсится при CREATE — битые ссылки
--     ловит миграция, а не первый вызов с прода).
--
-- Контракт JSONB (ключи нод/рёбер) — 1:1 с 021, Python-слой без изменений.

DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[]);
DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ);

CREATE OR REPLACE FUNCTION graph_traverse_full(
    p_start_id   UUID,
    p_depth      INT        DEFAULT 3,
    p_link_types TEXT[]     DEFAULT NULL,
    p_as_of      TIMESTAMPTZ DEFAULT now()
)
RETURNS TABLE(
    nodes JSONB,
    edges JSONB
)
LANGUAGE sql
STABLE
AS $$
WITH RECURSIVE graph_walk AS (
    -- Якорь: стартовая нода, только если её окно валидности покрывает as_of
    SELECT
        p_start_id        AS node_id,
        0                 AS depth,
        ARRAY[p_start_id] AS path
    WHERE EXISTS (
        SELECT 1 FROM memories m
        WHERE m.id = p_start_id
          AND m.valid_from <= p_as_of
          AND (m.valid_to IS NULL OR m.valid_to > p_as_of)
    )
    UNION
    -- Шаг обхода: вперёд по эффективному source → target с проекцией
    -- происхождения; нода-цель обязана быть живой на as_of (окно)
    SELECT
        eff.eff_target,
        gw.depth + 1,
        gw.path || eff.eff_target
    FROM graph_walk gw
    JOIN LATERAL (
        SELECT
            CASE WHEN r.inherited_from IS NOT NULL AND src.valid_from > p_as_of
                 THEN r.inherited_from ELSE r.source_id END AS eff_source,
            CASE WHEN r.inherited_from IS NOT NULL AND tgt.valid_from > p_as_of
                 THEN r.inherited_from ELSE r.target_id END AS eff_target,
            r.link_type
        FROM relations r
        JOIN memories src ON src.id = r.source_id
        JOIN memories tgt ON tgt.id = r.target_id
        WHERE r.target_id IS NOT NULL
          AND (r.source_id = gw.node_id OR r.inherited_from = gw.node_id)
          AND (p_link_types IS NULL OR r.link_type = ANY(p_link_types))
    ) eff ON eff.eff_source = gw.node_id
    WHERE gw.depth < p_depth
      AND NOT eff.eff_target = ANY(gw.path)
      AND EXISTS (
          SELECT 1 FROM memories m
          WHERE m.id = eff.eff_target
            AND m.valid_from <= p_as_of
            AND (m.valid_to IS NULL OR m.valid_to > p_as_of)
      )
),
uniq_nodes AS (
    -- Узел с несколькими путями достижимости → одна строка
    -- с минимальной глубиной (DISTINCT ON, как в 021)
    SELECT DISTINCT ON (node_id)
        node_id,
        depth
    FROM graph_walk
    ORDER BY node_id, depth
),
edge_proj AS (
    -- Рёбра с эффективными концами на as_of: перенесённые после as_of
    -- остаются на inherited_from-версии (историческое место ребра).
    -- Предфильтр по факту касания обхода — физических ИЛИ унаследованных
    -- концов — держит выборку индексной, финальный фильтр ниже
    -- перепроверяет эффективные концы.
    SELECT
        r.id AS rel_id,
        r.link_type,
        r.description,
        r.weight,
        CASE WHEN r.inherited_from IS NOT NULL AND src.valid_from > p_as_of
             THEN r.inherited_from ELSE r.source_id END AS eff_source,
        CASE WHEN r.inherited_from IS NOT NULL AND tgt.valid_from > p_as_of
             THEN r.inherited_from ELSE r.target_id END AS eff_target
    FROM relations r
    JOIN memories src ON src.id = r.source_id
    JOIN memories tgt ON tgt.id = r.target_id
    WHERE r.target_id IS NOT NULL
      AND (r.source_id IN (SELECT node_id FROM uniq_nodes)
           OR r.target_id IN (SELECT node_id FROM uniq_nodes)
           OR r.inherited_from IN (SELECT node_id FROM uniq_nodes))
)
SELECT
    -- Ноды: namespace — uid через JOIN namespaces
    (
        SELECT coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'id',         m.id,
                    'content',    left(m.content, 200),
                    'namespace',  n.uid,
                    'importance', m.importance,
                    'depth',      un.depth
                )
                ORDER BY un.depth, m.id
            ),
            '[]'::jsonb
        )
        FROM uniq_nodes un
        JOIN memories m ON m.id = un.node_id
        JOIN namespaces n ON n.id = m.namespace_id
    ) AS nodes,
    -- Рёбра: оба эффективных конца в пределах обхода
    (
        SELECT coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'id',          e.rel_id,
                    'source_id',   e.eff_source,
                    'target_id',   e.eff_target,
                    'link_type',   e.link_type,
                    'description', e.description,
                    'weight',      e.weight
                )
                ORDER BY e.rel_id
            ),
            '[]'::jsonb
        )
        FROM edge_proj e
        WHERE e.eff_source IN (SELECT node_id FROM uniq_nodes)
          AND e.eff_target IN (SELECT node_id FROM uniq_nodes)
    ) AS edges;
$$;

COMMENT ON FUNCTION graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ) IS 'Обход графа от start_id на дату p_as_of (по умолчанию now() — эффективный граф актуальных версий, мёртвых нет). Нода входит, если её окно валидности (valid_from..valid_to) покрывает as_of; рёбра, перенесённые REWIRE после as_of, проектируются на inherited_from-версию. Узел с несколькими путями — один раз (минимальная глубина).';

-- ════════════════════════════════════════════════════════════
-- 4. Backfill A — наследование для существующих superseded (12 строк)
-- ════════════════════════════════════════════════════════════
-- Те же правила, что и runtime-REWIRE в pg_repository.create_version
-- (queries.py REWIRE_RELATIONS_SOURCE/TARGET): переносим рёбра с живой
-- второй стороной, supersedes не трогаем, дубликаты не создаём, мёртвые
-- концы остаются на трупах (история). 12 пар — два set-based UPDATE,
-- без курсоров. Идемпотентно: повторный прогон не находит кандидатов.

DO $backfill$
DECLARE
    v_moved_src INT;
    v_moved_tgt INT;
    v_clusters  INT;
BEGIN
    UPDATE relations r
    SET source_id = s.superseded_by,
        inherited_from = s.id
    FROM memories s
    WHERE r.source_id = s.id
      AND s.status = 'superseded' AND s.superseded_by IS NOT NULL
      AND r.link_type <> 'supersedes'
      AND (r.target_id IS NULL OR EXISTS (
          SELECT 1 FROM memories m
          WHERE m.id = r.target_id
            AND m.status = 'asserted' AND m.valid_to IS NULL))
      AND NOT EXISTS (
          SELECT 1 FROM relations x
          WHERE x.source_id = s.superseded_by
            AND x.link_type = r.link_type
            AND x.target_id IS NOT DISTINCT FROM r.target_id);
    GET DIAGNOSTICS v_moved_src = ROW_COUNT;

    UPDATE relations r
    SET target_id = s.superseded_by,
        inherited_from = s.id
    FROM memories s
    WHERE r.target_id = s.id
      AND s.status = 'superseded' AND s.superseded_by IS NOT NULL
      AND r.link_type <> 'supersedes'
      AND EXISTS (
          SELECT 1 FROM memories m
          WHERE m.id = r.source_id
            AND m.status = 'asserted' AND m.valid_to IS NULL)
      AND NOT EXISTS (
          SELECT 1 FROM relations x
          WHERE x.target_id = s.superseded_by
            AND x.source_id = r.source_id
            AND x.link_type = r.link_type);
    GET DIAGNOSTICS v_moved_tgt = ROW_COUNT;

    UPDATE memories nw
    SET cluster_id = old.cluster_id
    FROM memories old
    WHERE old.superseded_by = nw.id
      AND old.cluster_id IS NOT NULL
      AND nw.cluster_id IS NULL;
    GET DIAGNOSTICS v_clusters = ROW_COUNT;

    RAISE NOTICE '023 backfill A: rewired % outgoing + % incoming relation(s) (dead-end edges kept on superseded as history), inherited % cluster_id(s)',
        v_moved_src, v_moved_tgt, v_clusters;
END;
$backfill$;

-- ════════════════════════════════════════════════════════════
-- 5. Владелец объектов — svc_athene_ai (инцидент 021: run.py
--    применяет миграции от этой роли)
-- ════════════════════════════════════════════════════════════

ALTER FUNCTION graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ) OWNER TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- 5b. get_relations_unified v2 (014) — обнажает inherited_from.
--     Приёмка V3.1 (В2): REWIRE ставит метку происхождения в таблице,
--     но главный путь чтения рёбер (memory_get_relations) её не отдавал.
--     Тело 014 + одна колонка; вызовы с (UUID, TEXT) совместимы.
-- ════════════════════════════════════════════════════════════

DROP FUNCTION IF EXISTS get_relations_unified(UUID, TEXT);

CREATE OR REPLACE FUNCTION get_relations_unified(
    p_memory_id UUID,
    p_link_type  TEXT DEFAULT NULL
)
RETURNS TABLE(
    id             UUID,
    source_id      UUID,
    target_id      UUID,
    target_name    TEXT,
    link_type      TEXT,
    description    TEXT,
    weight         FLOAT,
    metadata       JSONB,
    created_at     TIMESTAMPTZ,
    direction      TEXT,
    inherited_from UUID
)
LANGUAGE sql
STABLE
AS $$
    -- Исходящие связи: source = p_memory_id
    SELECT
        r.id, r.source_id, r.target_id, r.target_name,
        r.link_type, r.description, r.weight, r.metadata,
        r.created_at,
        'outgoing'::text AS direction,
        r.inherited_from
    FROM relations r
    WHERE r.source_id = p_memory_id
      AND (p_link_type IS NULL OR r.link_type = p_link_type)

    UNION ALL

    -- Входящие связи: target = p_memory_id
    SELECT
        r.id, r.source_id, r.target_id, r.target_name,
        r.link_type, r.description, r.weight, r.metadata,
        r.created_at,
        'incoming'::text AS direction,
        r.inherited_from
    FROM relations r
    WHERE r.target_id = p_memory_id
      AND (p_link_type IS NULL OR r.link_type = p_link_type)

    ORDER BY created_at DESC;
$$;

ALTER FUNCTION get_relations_unified(UUID, TEXT) OWNER TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- ВНИМАНИЕ: откат НЕ возвращает перенесённые рёбра на трупы и не снимает
-- унаследованные cluster_id — это потеря данных задним числом. Backfill
-- необратим по построению (id наследника в рёбрах актуальнее).
-- Рёбра с inherited_from можно вернуть вручную:
--   UPDATE relations SET source_id = inherited_from
--   WHERE inherited_from IS NOT NULL AND ...  (анализ по случаю)
--
-- DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ);
-- (тело трёхаргументной версии из 021 — см. 021_phase1_search_fixes.sql DOWN)
--
-- DROP INDEX IF EXISTS idx_relations_inherited_from;
-- ALTER TABLE relations DROP CONSTRAINT IF EXISTS fk_relations_inherited_from;
-- ALTER TABLE relations DROP COLUMN IF EXISTS inherited_from;
-- DROP INDEX IF EXISTS idx_memories_superseded_by;
