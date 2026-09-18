-- ============================================================
-- 022_clusters.sql — ЗАГОТОВКА Фазы 2: Level 2 кластеризация
-- ============================================================
-- Дата: 2026-09-18
-- Исполнитель: Нора (db-architect)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§2.3 Кластеризация Level 2)
--
-- ⚠ ЗАГОТОВКА Фазы 2 — применяется Рэем в деплой-окно Фазы 2,
--   из автопрогона до кода Ф2 НЕ применять. Алфавитно файл идёт
--   после 021 — номер корректен; INSERT INTO _migrations выполнит
--   run.py при автоприменении (здесь только владелец объектов).
--
-- Содержимое:
--   1) Таблица clusters (id, namespace_id FK, label, summary,
--      member_count, coherence, last_computed_at, created/updated)
--      + индексы (namespace_id), (member_count DESC) + триггер updated_at
--   2) memories.cluster_id UUID NULL FK → clusters.id (ON DELETE SET NULL)
--      + partial-индекс WHERE cluster_id IS NOT NULL
--   3) assign_clusters(p_namespace_id, p_threshold) — РАЗМЕТКА кластеров
--      групп ≥ 2 близких гранул. Обёртка алгоритма merge_similar_granules
--      (016/020: recursive CTE компонент связности), но в отличие от него
--      НЕ мержит и НЕ удаляет гранулы — только создаёт/обновляет кластеры,
--      проставляет cluster_id, member_count/coherence/last_computed_at.
--      Идемпотентна: id кластера детерминирован (md5 от namespace +
--      минимального члена группы) — повторный пересчёт даёт те же id.
--   4) Хелперы granule_trigrams / trigram_similarity — текстовая близость.
--   5) update_updated_at_column — семантическое расширение: чисто
--      кластерная разметка не «омолаживает» updated_at гранул.
--   6) ALTER ... OWNER TO svc_athene_ai — владелец функций/таблиц
--      (урок инцидента 021: автопрогон run.py от svc_athene_ai падал
--      на функции, принадлежащей не-svc-роли).
--
-- МЕХАНИЗМ БЛИЗОСТИ (находка Фазы 2, см. отчёт):
--   merge_similar_granules (016/020) близость в SQL НЕ считает: пары
--   (source_id, target_id, similarity) заливает внешний скрипт из Qdrant
--   в таблицу _similarity_pairs; единственный SQL-путь расчёта пар —
--   find_similar_pairs_pgvector — дропнут миграцией 020 (pgvector выпилен
--   011, колонки embedding нет). На канонической схеме без Qdrant
--   пороговой близости в PG НЕ СУЩЕСТВУЕТ.
--   Решение: assign_clusters считает близость сам — косинус множеств
--   словесных 3-грамм (lowercase, ё→е, [a-zа-я0-9]{3,}), чистый SQL
--   без расширений (pg_trgm НЕ используем: не везде установлен и его
--   семантика показателей не совпадает с «средней попарной близостью
--   0..1»). Кандидаты вместо O(N²) cross join — prefix-фильтр (AllPairs/
--   PPJoin): пара с косинусом ≥ T обязана делить триграмму в
--   лексикографических префиксах длины ⌊(1−T²)·|X|⌋+1 обоих множеств
--   (вывод: |A∩B| ≥ T²·|A| и ≥ T²·|B| при cos ≥ T; если пересечение
--   префиксов пусто, получаем x > y и y > x — противоречие). Претагер
--   БЕЗ ПОТЕРЬ: ни одна пара сверх порога не пропущена.
--
-- Ограничения (сознательные):
--   * Сложность候选ных пар ~ Σ|prefix(X)| — на однородных корпусах
--     (частые триграммы в префиксах) деградирует, но остаётся дешевле
--     полного N²/2; задача ночная (beat, §2.2), не интерактивная.
--   * member_count кластера протухает при ретракции члена до следующего
--     пересчёта; живость чистит assign_clusters + orphans_cleanup (§2.2).
--   * Инвариант «кластер и гранула из одного namespace» гарантируется
--     assign_clusters (FK этого не выражает).
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Таблица clusters
-- ════════════════════════════════════════════════════════════
-- Level 2 иерархии: группа ≥ 2 близких гранул одного namespace.
-- summary — LLM-саммари кластера (Фаза 4, консолидация); сейчас NULL.

CREATE TABLE IF NOT EXISTS clusters (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id     UUID NOT NULL REFERENCES namespaces(id) ON DELETE RESTRICT,
    label            TEXT NOT NULL,
    summary          TEXT,
    member_count     INTEGER NOT NULL DEFAULT 0,
    coherence        REAL,
    last_computed_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_clusters_member_count CHECK (member_count >= 0),
    CONSTRAINT chk_clusters_coherence    CHECK (coherence IS NULL OR (coherence >= 0 AND coherence <= 1))
);

COMMENT ON TABLE clusters IS 'Level 2: кластеры близких гранул (threshold-кластеризация по namespace). Создаются/пересчитываются assign_clusters; НЕ мержат гранулы.';
COMMENT ON COLUMN clusters.id IS 'PK. Для кластеров assign_clusters — детерминирован: uuid(md5(namespace_id + group_key)), повтор пересчёта = тот же id. DEFAULT — только для ручных вставок.';
COMMENT ON COLUMN clusters.namespace_id IS 'FK → namespaces.id. Кластер всегда в пределах одного namespace.';
COMMENT ON COLUMN clusters.label IS 'Авто-метка: топ-термин состава (частотное значимое слово) или cluster-<hash8>.';
COMMENT ON COLUMN clusters.summary IS 'LLM-саммари кластера — заполняется Фазой 4 (консолидация), сейчас NULL.';
COMMENT ON COLUMN clusters.member_count IS 'Число членов (гранул с cluster_id = id). Пересчитывается assign_clusters.';
COMMENT ON COLUMN clusters.coherence IS 'Средняя ПОЛНАЯ попарная близость членов (косинус триграмм), 0..1. Ниже порога у пар-«мостов» цепочек — сигнал рыхлости кластера.';
COMMENT ON COLUMN clusters.last_computed_at IS 'Момент последнего пересчёта assign_clusters.';

-- Индексы:
--   (namespace_id)        — WHERE namespace_id = $1: состав/чистка кластеров
--                           namespace (assign_clusters шаг 7) и будущий тул
--                           memory_cluster_list (Фаза 3.2);
--   (member_count DESC)   — ORDER BY member_count DESC LIMIT n: обзор
--                           крупнейших кластеров namespace (Level 2 дашборд).
CREATE INDEX IF NOT EXISTS idx_clusters_namespace
    ON clusters (namespace_id);

CREATE INDEX IF NOT EXISTS idx_clusters_member_count
    ON clusters (member_count DESC);

-- Триггер updated_at (общий хелпер с 001; расширяется в §5)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_clusters_updated_at'
          AND tgrelid = 'clusters'::regclass
    ) THEN
        CREATE TRIGGER trg_clusters_updated_at
            BEFORE UPDATE ON clusters
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    END IF;
END;
$$;

-- ════════════════════════════════════════════════════════════
-- 2. Колонка memories.cluster_id + partial-индекс
-- ════════════════════════════════════════════════════════════
-- Принадлежность кластеру одним полем (D-решение §2.3 плана:
-- relation member_of для кластеров НЕ используем).

ALTER TABLE memories ADD COLUMN IF NOT EXISTS cluster_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memories_cluster'
          AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT fk_memories_cluster
            FOREIGN KEY (cluster_id) REFERENCES clusters(id)
            ON DELETE SET NULL;
    END IF;
END;
$$;

COMMENT ON COLUMN memories.cluster_id IS 'FK → clusters.id (Level 2). NULL = гранула вне кластеров (одиночка). Инвариант «тот же namespace, что у кластера» гарантирует assign_clusters.';

-- Индекс под запросы состава кластера: WHERE cluster_id = $1 (карточка
-- кластера, Фаза 3.2) и NOT EXISTS-подзапрос чистки assign_clusters.
-- Partial: одиночек (NULL) большинство — индекс не хранит их вовсе.
CREATE INDEX IF NOT EXISTS idx_memories_cluster_id
    ON memories (cluster_id)
    WHERE cluster_id IS NOT NULL;

-- ════════════════════════════════════════════════════════════
-- 3. Хелперы текстовой близости (без расширений)
-- ════════════════════════════════════════════════════════════

-- Множество 3-грамм слов: lowercase, ё→е, токены [a-zа-я0-9]{3,}.
-- Возвращает ОТСОРТИРОВАННЫЙ массив — порядок нужен prefix-фильтру.
CREATE OR REPLACE FUNCTION granule_trigrams(p_content TEXT)
RETURNS TEXT[]
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT coalesce(array_agg(DISTINCT tg ORDER BY tg), ARRAY[]::TEXT[])
    FROM (
        SELECT substr(m.word[1], gs.i, 3) AS tg
        FROM regexp_matches(translate(lower(p_content), 'ё', 'е'), '[a-zа-я0-9]{3,}', 'g') AS m(word)
        CROSS JOIN LATERAL generate_series(1, length(m.word[1]) - 2) AS gs(i)
    ) grams;
$$;

COMMENT ON FUNCTION granule_trigrams(TEXT) IS 'Множество словесных 3-грамм текста (lowercase, ё→е), лексикографически отсортированное. Основа текстовой близости assign_clusters.';

-- Косинус множеств триграмм: |A∩B| / sqrt(|A|·|B|), 0..1.
-- Входы — массивы из granule_trigrams (уникальные элементы).
CREATE OR REPLACE FUNCTION trigram_similarity(p_a TEXT[], p_b TEXT[])
RETURNS REAL
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT CASE
        WHEN cardinality(p_a) = 0 OR cardinality(p_b) = 0 THEN 0::REAL
        ELSE round(
            ((SELECT count(*)::REAL / sqrt(cardinality(p_a)::REAL * cardinality(p_b)::REAL)
              FROM (SELECT unnest(p_a) INTERSECT SELECT unnest(p_b)) AS inter))::numeric,
            4)::REAL
    END;
$$;

COMMENT ON FUNCTION trigram_similarity(TEXT[], TEXT[]) IS 'Косинус множеств триграмм (0..1). Согласован с порогом assign_clusters и метрикой coherence.';

-- ════════════════════════════════════════════════════════════
-- 4. assign_clusters — разметка кластеров namespace
-- ════════════════════════════════════════════════════════════
-- Алгоритм (переиспользует пороговую кластеризацию 016/020, мержа НЕТ):
--   шаг 0  снять прежнюю разметку namespace (cluster_id = NULL);
--   шаг 1  множества триграмм актуальных гранул
--          (status='asserted' AND valid_to IS NULL — канонический фильтр);
--   шаг 2  пары близости: prefix-фильтр кандидатов (без потерь,
--          см. вывод в шапке) + точный косинус ≥ p_threshold;
--   шаг 3  компоненты связности пар (recursive CTE, паттерн 016);
--          группа идентифицируется минимальным UUID состава;
--   шаг 4  метрики групп: member_count, coherence (полная попарная),
--          label (топ-термин, без стоп-слов);
--   шаг 5  upsert кластеров с ДЕТЕРМИНИРОВАННЫМ id
--          uuid(md5('cluster:' + namespace + ':' + group_key)) —
--          повтор пересчёта переиспользует тот же кластер, без дублей;
--   шаг 6  разметка членов (UPDATE memories.cluster_id);
--   шаг 7  удаление опустевших кластеров namespace;
--   шаг 8  возврат TABLE(cluster_id, member_count, coherence).
-- Служебные temp-таблицы _cluster_* создаются ON COMMIT DROP и
-- пересоздаются DROP+CREATE — безопасно при повторных вызовах
-- в одной транзакции; имена в зарезервированной зоне "_".

CREATE OR REPLACE FUNCTION assign_clusters(
    p_namespace_id UUID,
    p_threshold    REAL DEFAULT 0.92
)
RETURNS TABLE(cluster_id UUID, member_count INT, coherence REAL)
LANGUAGE plpgsql
AS $$
DECLARE
    -- доля префикса prefix-фильтра: ⌊(1−T²)·|X|⌋+1 триграмм
    v_prefix_pow REAL := 1.0 - p_threshold * p_threshold;
BEGIN
    IF p_threshold IS NULL OR p_threshold <= 0 OR p_threshold > 1 THEN
        RAISE EXCEPTION 'p_threshold должен быть в (0, 1], получено %', p_threshold;
    END IF;

    -- шаг 0: пересчёт с чистого листа
    -- (колонки квалифицированы алиасом: имена совпадают с OUT-параметрами
    --  функции, plpgsql иначе роняет «неоднозначная ссылка»)
    UPDATE memories AS m
    SET cluster_id = NULL
    WHERE m.namespace_id = p_namespace_id
      AND m.cluster_id IS NOT NULL;

    -- шаг 1: множества триграмм актуальных гранул namespace
    DROP TABLE IF EXISTS _cluster_gramsets;
    CREATE TEMP TABLE _cluster_gramsets ON COMMIT DROP AS
    SELECT id, grams
    FROM (
        SELECT m.id, granule_trigrams(m.content) AS grams
        FROM memories m
        WHERE m.namespace_id = p_namespace_id
          AND m.status = 'asserted'
          AND m.valid_to IS NULL
    ) t
    WHERE cardinality(grams) > 0;

    -- шаг 2: пары близости (prefix-фильтр → точный косинус)
    DROP TABLE IF EXISTS _cluster_pairs;
    CREATE TEMP TABLE _cluster_pairs ON COMMIT DROP AS
    WITH prefix_index AS (
        SELECT g.id, g.grams,
               (floor(v_prefix_pow * cardinality(g.grams)) + 1)::INT AS prefix_len
        FROM _cluster_gramsets g
    ),
    prefix_items AS (
        SELECT pi.id, pi.grams, u.tg
        FROM prefix_index pi
        CROSS JOIN LATERAL unnest(pi.grams[1:pi.prefix_len]) AS u(tg)
    ),
    candidates AS (
        SELECT DISTINCT pa.id AS aid, pb.id AS bid
        FROM prefix_items pa
        JOIN prefix_items pb ON pb.tg = pa.tg AND pa.id < pb.id
    ),
    scored AS (
        SELECT c.aid, c.bid, trigram_similarity(a.grams, b.grams) AS sim
        FROM candidates c
        JOIN _cluster_gramsets a ON a.id = c.aid
        JOIN _cluster_gramsets b ON b.id = c.bid
    )
    SELECT aid, bid, sim
    FROM scored
    WHERE sim >= p_threshold;

    -- нет пар — нет кластеров: подчистить опустевшие и вернуть 0 строк
    IF NOT EXISTS (SELECT 1 FROM _cluster_pairs) THEN
        DELETE FROM clusters c
        WHERE c.namespace_id = p_namespace_id
          AND NOT EXISTS (SELECT 1 FROM memories m WHERE m.cluster_id = c.id);
        RETURN;
    END IF;

    -- шаг 3: компоненты связности (паттерн 016, без depth-cap:
    -- полный охват гарантирует старт из каждого узла, cycles режет visited)
    DROP TABLE IF EXISTS _cluster_groups;
    CREATE TEMP TABLE _cluster_groups ON COMMIT DROP AS
    WITH RECURSIVE
    all_nodes AS (
        SELECT aid AS node_id FROM _cluster_pairs
        UNION
        SELECT bid FROM _cluster_pairs
    ),
    all_edges AS (
        SELECT aid AS from_node, bid AS to_node FROM _cluster_pairs
        UNION
        SELECT bid, aid FROM _cluster_pairs
    ),
    components AS (
        SELECT node_id,
               node_id AS component_root,
               ARRAY[node_id] AS visited
        FROM all_nodes
        UNION ALL
        SELECT e.to_node,
               c.component_root,
               c.visited || e.to_node
        FROM components c
        JOIN all_edges e ON e.from_node = c.node_id
        WHERE NOT e.to_node = ANY(c.visited)
    ),
    group_keys AS (
        SELECT node_id AS member_id,
               (array_agg(component_root ORDER BY component_root::text))[1] AS group_key
        FROM components
        GROUP BY node_id
    )
    SELECT member_id, group_key
    FROM group_keys;

    -- шаг 4: метрики групп + шаг 5 (карта кластеров с детерминированным id)
    DROP TABLE IF EXISTS _cluster_map;
    CREATE TEMP TABLE _cluster_map ON COMMIT DROP AS
    WITH pairwise AS (
        -- полная попарная близость внутри групп (не только рёбра порога)
        SELECT ga.group_key, trigram_similarity(a.grams, b.grams) AS sim
        FROM _cluster_groups ga
        JOIN _cluster_groups gb
          ON gb.group_key = ga.group_key AND gb.member_id > ga.member_id
        JOIN _cluster_gramsets a ON a.id = ga.member_id
        JOIN _cluster_gramsets b ON b.id = gb.member_id
    ),
    member_counts AS (
        -- члены считаются по _cluster_groups БЕЗ join с парами:
        -- иначе fan-out размножает строки и count завышается
        SELECT group_key, count(*)::INT AS member_count
        FROM _cluster_groups
        GROUP BY group_key
    ),
    pairwise_avg AS (
        -- avg по уникальным парам (pairwise: ga.id < gb.id — без дублей)
        SELECT group_key, round(avg(sim)::numeric, 4)::REAL AS coherence
        FROM pairwise
        GROUP BY group_key
    ),
    group_metrics AS (
        SELECT mc.group_key, mc.member_count, pa.coherence
        FROM member_counts mc
        LEFT JOIN pairwise_avg pa ON pa.group_key = mc.group_key
    ),
    terms AS (
        -- слова-кандидаты метки: длина ≥ 5, без служебных
        SELECT gr.group_key, w.word
        FROM _cluster_groups gr
        JOIN memories m ON m.id = gr.member_id
        CROSS JOIN LATERAL (
            SELECT lower(m2.word[1]) AS word
            FROM regexp_matches(translate(lower(m.content), 'ё', 'е'), '[a-zа-я0-9]{5,}', 'g') AS m2(word)
        ) w
        WHERE w.word NOT IN (
            'которые','который','которая','которое','которых','чтобы','потому','тогда','только','почему','впрочем',
            'больше','меньше','вместе','всегда','никогда','сейчас','однако','оказалось','окажется','является','являются',
            'может','могут','должен','должна','должны','делает','делают','сделать','получить','находится',
            'используется','используются','данный','данная','данное','этого','этой','этому','этими','всего',
            'about','after','before','because','between','would','could','should','there','their','these','those',
            'other','which','where','while','being','under','again','further','since'
        )
    ),
    top_terms AS (
        -- топ-термин: max частота, затем длина, затем алфавит (детерминизм)
        SELECT DISTINCT ON (group_key) group_key, word
        FROM (
            SELECT t.group_key, t.word, count(*) AS cnt
            FROM terms t
            GROUP BY t.group_key, t.word
        ) f
        ORDER BY group_key, cnt DESC, length(word) DESC, word
    ),
    hashed AS (
        SELECT gm.group_key, gm.member_count, gm.coherence,
               md5('cluster:' || p_namespace_id::text || ':' || gm.group_key::text) AS h
        FROM group_metrics gm
    )
    SELECT hs.group_key, hs.member_count, hs.coherence, hs.h,
           (substr(hs.h, 1, 8) || '-' || substr(hs.h, 9, 4) || '-' || substr(hs.h, 13, 4)
            || '-' || substr(hs.h, 17, 4) || '-' || substr(hs.h, 21, 12))::UUID AS cid,
           coalesce(tt.word, 'cluster-' || substr(hs.h, 1, 8)) AS label
    FROM hashed hs
    LEFT JOIN top_terms tt ON tt.group_key = hs.group_key;

    -- шаг 5: upsert кластеров (id конфликтует сам с собой при пересчёте)
    INSERT INTO clusters (id, namespace_id, label, summary, member_count, coherence, last_computed_at)
    SELECT cm.cid, p_namespace_id, cm.label, NULL, cm.member_count, cm.coherence, now()
    FROM _cluster_map cm
    ON CONFLICT (id) DO UPDATE SET
        label           = EXCLUDED.label,
        member_count    = EXCLUDED.member_count,
        coherence       = EXCLUDED.coherence,
        last_computed_at = now();

    -- шаг 6: разметка членов
    UPDATE memories m
    SET cluster_id = cm.cid
    FROM _cluster_map cm
    JOIN _cluster_groups gr ON gr.group_key = cm.group_key
    WHERE m.id = gr.member_id;

    -- шаг 7: чистка опустевших кластеров namespace
    DELETE FROM clusters c
    WHERE c.namespace_id = p_namespace_id
      AND NOT EXISTS (SELECT 1 FROM memories m WHERE m.cluster_id = c.id);

    -- шаг 8: возврат пересчитанных кластеров
    RETURN QUERY
    SELECT c.id, c.member_count, c.coherence
    FROM clusters c
    WHERE c.namespace_id = p_namespace_id
    ORDER BY c.member_count DESC, c.id;
END;
$$;

COMMENT ON FUNCTION assign_clusters(UUID, REAL) IS 'Разметка кластеров Level 2 по namespace (порог косинуса словесных 3-грамм, default 0.92). НЕ мержит гранулы. Идемпотентна: детерминированные id кластеров. Возвращает (cluster_id, member_count, coherence).';

-- ════════════════════════════════════════════════════════════
-- 5. update_updated_at_column — семантическое расширение
-- ════════════════════════════════════════════════════════════
-- Было (001/003): любое UPDATE дёргает updated_at. Проблема: ночная
-- assign_clusters перезаписывает cluster_id у тысяч гранул — без этого
-- фикса ВСЕ кластеризованные гранулы «омолаживались» бы и всплывали
-- в сортировках свежести (project_context_snapshot: ORDER BY importance,
-- updated_at; веб-морда Фазы 5).
-- Стало: изменение ТОЛЬКО кластерной разметки updated_at не трогает;
-- любое содержательное изменение — как раньше. Для таблиц без cluster_id
-- вычитание несуществующего ключа — no-op, семантика прежняя.

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    IF (to_jsonb(NEW) - 'updated_at' - 'cluster_id') IS NOT DISTINCT FROM
       (to_jsonb(OLD) - 'updated_at' - 'cluster_id') THEN
        RETURN NEW;
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════
-- 6. Владелец объектов — svc_athene_ai
-- ════════════════════════════════════════════════════════════
-- Инцидент 021: run.py применяет миграции от svc_athene_ai; функция,
-- созданная чужой ролью, роняла автопрогон. Здесь — владелец ВСЕХ
-- объектов, создаваемых миграцией. Роль существует с 013.

ALTER TABLE clusters OWNER TO svc_athene_ai;

ALTER FUNCTION granule_trigrams(TEXT) OWNER TO svc_athene_ai;
ALTER FUNCTION trigram_similarity(TEXT[], TEXT[]) OWNER TO svc_athene_ai;
ALTER FUNCTION assign_clusters(UUID, REAL) OWNER TO svc_athene_ai;
ALTER FUNCTION update_updated_at_column() OWNER TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- ВНИМАНИЕ: откат сносит разметку кластеров и сами кластеры
-- (summary Фазы 4 ещё не существует — терять нечего).
--
-- DROP FUNCTION IF EXISTS assign_clusters(UUID, REAL);
-- DROP FUNCTION IF EXISTS trigram_similarity(TEXT[], TEXT[]);
-- DROP FUNCTION IF EXISTS granule_trigrams(TEXT);
--
-- DROP INDEX IF EXISTS idx_memories_cluster_id;
-- ALTER TABLE memories DROP CONSTRAINT IF EXISTS fk_memories_cluster;
-- ALTER TABLE memories DROP COLUMN IF EXISTS cluster_id;
--
-- DROP TRIGGER IF EXISTS trg_clusters_updated_at ON clusters;
-- DROP TABLE IF EXISTS clusters;
--
-- -- Восстановить простое тело update_updated_at_column (до 022):
-- CREATE OR REPLACE FUNCTION update_updated_at_column()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = now();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
