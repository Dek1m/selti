-- ============================================================
-- 026_edge_lifecycle.sql — V3.5 «Жизнь графа знаний»: жизненный цикл рёбер
-- ============================================================
-- Дата: 2026-09-22
-- Исполнитель: Нора (db-architect)
-- Задача: T1.1 плана V3.5 (Жизнь графа знаний)
--
-- Содержимое:
--   1) relations: used_count, last_used_at, pruned_at — жизнь ребра
--      от создания до отсечения;
--   2) Частичный expression-индекс idx_relations_decay_due — единственный
--      потребитель новых полей с горячим запросом: decay-кампания (beat);
--   3) graph_traverse_full v3.1 — рёбра фильтруются моментом отсечения
--      pruned_at относительно p_as_of; сигнатура и JSONB-контракт — 1:1
--      с версией из 023 (вызовы с 3 и 4 аргументами не меняются);
--   4) Backfill не требуется: новые колонки живут на DEFAULT'ах
--      (metadata-only ALTER, ~106k строк — мгновенно).
--
-- ── РЕШЕНИЕ: pruned_at TIMESTAMPTZ, а НЕ status TEXT + CHECK ──────
-- План (Момо) предлагал оба варианта; выбран «pruned_at NULL = живое».
-- Обоснование:
--   * Bitemporal-симметрия с memories.valid_to (NULL = актуально) —
--     единый идиом схемы: у ноды окно жизни valid_from..valid_to,
--     у ребра — created_at..pruned_at. Два представления одной семантики.
--   * ЕДИНСТВЕННЫЙ фильтр для боевого и исторического пути обхода:
--       (pruned_at IS NULL OR pruned_at > p_as_of)
--     при p_as_of = now() вырождается в «только живые». Статус-колонка
--     потребовала бы различать «боевой vs исторический» сравнением
--     p_as_of с now() — микросекундная гонка (клиент передаёт now() из
--     Python, серверное now() транзакции позже) ошибочно классифицирует
--     боевой запрос как исторический и показывает мёртвые рёбра.
--   * Статус без даты не отвечает на вопрос «было ли ребро живо на дату
--     X» — волны decay между двумя as_of неразличимы. pruned_at даёт
--     честную историю: prune ПОСЛЕ as_of не переписывает её (ребро видно),
--     prune ДО as_of — ребро в эффективном графе той даты отсутствовало.
--     Это та же семантика, по которой traverse скрывает retracted-гранулу
--     позже её valid_to; показывать «зомби-рёбра» рядом с живыми нодами —
--     несогласованная проекция.
--   * Статус + pruned_at вместе = два источника истины с рассинхроном.
--   * Момент отсечения — сам ценный факт: аудит волн decay-кампании.
--   * REWIRE-фильтр «pruned-рёбра не переезжают на наследника» —
--     r.pruned_at IS NULL (эквивалент r.status='active' из плана).
--   * CHECK для монотонного факта не нужен: «нет даты = активен» не
--     принимает мусорных значений по построению.
--
-- ── РЕШЕНИЕ: колонки, а НЕ JSONB metadata ─────────────────────────
-- Новые поля — участники WHERE/индексов горячего пути (decay-кампания,
-- touch при обходе). JSONB переписывается целиком на каждый touch,
-- фильтры по нему — GIN вместо b-tree, статистика планировщика хуже.
-- Типы повторяют идиомы memories 018: access_count INTEGER NOT NULL
-- DEFAULT 0, last_accessed_at TIMESTAMPTZ — единообразие схемы.
--
-- ── Блокировки и идемпотентность ──────────────────────────────────
-- ALTER ADD COLUMN с DEFAULT в PG 11+ не переписывает таблицу
-- (metadata-only); ACCESS EXCLUSIVE — миллисекунды.
-- CREATE INDEX БЕЗ CONCURRENTLY — осознанно: run.py исполняет up-секцию
-- одной транзакцией (conn.execute внутри transaction()), а CONCURRENTLY
-- внутри транзакции PostgreSQL запрещает в принципе. 106k строк —
-- построение индекса < 1 c, ShareLock краток; statement_timeout пула
-- (45 c) на соединение раннера не действует. Паттерн команды —
-- обычный CREATE INDEX IF NOT EXISTS (018/022/023), не вводим новый.
-- Повторный прогон: IF NOT EXISTS / DROP IF EXISTS + CREATE — no-op.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Колонки жизни ребра
-- ════════════════════════════════════════════════════════════

-- Счётчик использования (touch при обходе, T1.2). Вход decay-кампании.
ALTER TABLE relations ADD COLUMN IF NOT EXISTS used_count INTEGER NOT NULL DEFAULT 0;

-- Момент последнего использования. NULL = никогда (возраст ребра для
-- decay — created_at, см. индекс §2).
ALTER TABLE relations ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

-- Момент отсечения из эффективного графа (decay-кампания, T1.2).
-- NULL = живое ребро. Воскрешение (повторная встреча в линкере) —
-- UPDATE pruned_at = NULL, used_count = 0; partial unique
-- idx_relations_unique_link не различает живое/pruned — ON CONFLICT
-- для пары продолжает срабатывать, дверь к DO UPDATE-воскрешению открыта.
ALTER TABLE relations ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMPTZ;

COMMENT ON COLUMN relations.used_count IS 'Сколько раз ребро встречено в рабочих обходах графа (touch, T1.2). Вход decay-кампании: слабое + давно не используемое = кандидат в pruned.';
COMMENT ON COLUMN relations.last_used_at IS 'Момент последнего использования ребра. NULL = никогда не использовалось: возраст для decay-кампании считает created_at (COALESCE в idx_relations_decay_due).';
COMMENT ON COLUMN relations.pruned_at IS 'Момент отсечения ребра из эффективного графа (decay, V3.5). NULL = живое. Историческая проекция graph_traverse_full(p_as_of) видит ребро, пока pruned_at IS NULL OR pruned_at > p_as_of — симметрия с окнами валидности нод.';

-- ════════════════════════════════════════════════════════════
-- 2. Индекс decay-кампании
-- ════════════════════════════════════════════════════════════
-- Горячий запрос кампании (beat, T1.2):
--   SELECT ... FROM relations
--   WHERE pruned_at IS NULL
--     AND COALESCE(last_used_at, created_at) < now() - interval '...'
--   ORDER BY COALESCE(last_used_at, created_at)
--   LIMIT batch;
--
-- Expression COALESCE(last_used_at, created_at) — IMMUTABLE (оба
-- timestamptz), допустим в индексе. Ключевой охват: 81k l1c-рёбер
-- НИКОГДА не использовались (last_used_at IS NULL) — их возраст по
-- created_at; индекс по «голому» last_used_at не покрыл бы главную
-- массу кандидатов первого месяца. Partial-предикат pruned_at IS NULL
-- держит индекс размером с живое множество: pruned-строки выпадают
-- сами, переиндексация после каждой волны не нужна.
--
-- Отдельный индекс (last_used_at) WHERE pruned_at IS NULL AND
-- last_used_at IS NOT NULL НЕ создаём: подмножество покрытия этого
-- (диапазон по выражению для NOT NULL-строк = диапазон по last_used_at)
-- — чистая избыточность по правилу «(a,b) покрывает a».
--
-- Под обход (source_id/target_id с фильтром pruned_at IS NULL) новых
-- индексов НЕ заводим: существующие idx_relations_source/target/
-- inherited_from дают lookup по узлу (степень узла мала), предикат
-- отсечения применяется к горсти строк пост-фактум; partial-дубликаты
-- этих индексов ничего не ускоряют.
-- Индекс (pruned_at) WHERE pruned_at IS NOT NULL — только для аналитики
-- отсечённых, запроса нет: не индексируем на всякий случай.

CREATE INDEX IF NOT EXISTS idx_relations_decay_due
    ON relations (COALESCE(last_used_at, created_at))
    WHERE pruned_at IS NULL;

COMMENT ON INDEX idx_relations_decay_due IS 'Кандидаты decay-кампании: живые рёбра, не используемые с даты COALESCE(last_used_at, created_at). NULL-возраст = created_at (81k l1c — первые кандидаты).';

-- ════════════════════════════════════════════════════════════
-- 3. graph_traverse_full v3.1 — фильтр отсечённых рёбер
-- ════════════════════════════════════════════════════════════
-- Тело — версия 023 (as_of + проекция inherited_from), отличия ТОЛЬКО
-- предикатом (r.pruned_at IS NULL OR r.pruned_at > p_as_of) в двух
-- местах: рекурсивный шаг (LATERAL-lookup рёбер ноды) и edge_proj
-- (сбор рёбер ответа). Сигнатура, LANGUAGE sql, STABLE, JSONB-контракт
-- (ключи нод/рёбер) — без изменений: Python-слой (queries.py
-- TRAVERSE_FULL) не требует правок.
--
-- Семантика: при p_as_of = now() (все текущие клиенты) pruned_at > now()
-- невозможно — остаются только живые рёбра (боевой путь фильтрует
-- отсечённые). При as_of в прошлом ребро видно, если оно было живо на
-- эту дату: волна decay не переписывает историю задним числом.

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
    -- происхождения; нода-цель обязана быть живой на as_of (окно);
    -- ребро обязано быть живым на as_of (не отсечено до p_as_of)
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
          AND (r.pruned_at IS NULL OR r.pruned_at > p_as_of)
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
    -- остаются на inherited_from-версии (историческое место ребра);
    -- отсечённые до as_of в эффективный граф даты не входят.
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
      AND (r.pruned_at IS NULL OR r.pruned_at > p_as_of)
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

COMMENT ON FUNCTION graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ) IS 'Обход графа от start_id на дату p_as_of (по умолчанию now() — эффективный граф: живые ноды и НЕ отсечённые рёбра). Нода входит, если окно валидности покрывает as_of; ребро — если pruned_at IS NULL OR pruned_at > p_as_of; рёбра, перенесённые REWIRE после as_of, проектируются на inherited_from-версию. Узел с несколькими путями — один раз (минимальная глубина).';

-- Владелец объектов — svc_athene_ai (run.py применяет миграции от этой роли,
-- инцидент 021)
ALTER FUNCTION graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ) OWNER TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- 4. Backfill — НЕ ТРЕБУЕТСЯ
-- ════════════════════════════════════════════════════════════
-- Существующим ~106k рёбер остаются на дефолтах: used_count = 0,
-- last_used_at = NULL (возраст = created_at), pruned_at = NULL (живое).
-- ALTER с DEFAULT — metadata-only, rewrite строк нет, прогон мгновенный.
-- Ни одного UPDATE по строкам в этой миграции.

-- DOWN: откат миграции (ручной; см. примечание)
-- ════════════════════════════════════════════════════════════
-- Примечание: секция закомментирована по образцу 022/023 — run.py --down
-- исполняет текст после маркера «-- DOWN» и при раскомментированном
-- коде выполнит его; авто-отката нет и не должно быть (деплой push→main).
-- Для ручного отката на реплике дампа — раскомментировать:
--
-- 1) Функцию вернуть к версии 023 (тело — из 023_memory_v3_core.sql §3,
--    БЕЗ предиката pruned_at; сигнатуры совпадают):
-- DROP FUNCTION IF EXISTS graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ);
-- CREATE OR REPLACE FUNCTION graph_traverse_full(...) AS $$ ... $$;
-- ALTER FUNCTION graph_traverse_full(UUID, INT, TEXT[], TIMESTAMPTZ)
--     OWNER TO svc_athene_ai;
--
-- 2) Объекты схемы:
-- DROP INDEX IF EXISTS idx_relations_decay_due;
-- ALTER TABLE relations DROP COLUMN IF EXISTS pruned_at;
-- ALTER TABLE relations DROP COLUMN IF EXISTS last_used_at;
-- ALTER TABLE relations DROP COLUMN IF EXISTS used_count;
--
-- Потери при откате: used_count/last_used_at — накопительная статистика
-- (не восстанавливается, некритична); pruned_at — разметка decay-кампании:
-- до первой волны пусто (нечего терять), после волн — повторная кампания
-- пересчитает отсечение, но уже отсечённые рёбра «воскреснут» в
-- эффективном графе до её запуска. Откат после первой волны — только по
-- решению Мастера, с осознанием этого эффекта.
