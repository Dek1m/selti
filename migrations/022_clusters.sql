-- ============================================================
-- 022_clusters.sql — Фаза 2: Level 2 кластеризация (v2, пары из Qdrant ANN)
-- ============================================================
-- Дата: 2026-09-18 (v2 — Нора, по прод-фактам Рэя)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§2.3 Кластеризация Level 2)
--
-- ⚠ ПЕРЕПРИМЕНЕНИЕ (деплой-инструкция Рэю):
--   v1 этой миграции (триграммный assign_clusters) уже применена на проде.
--   Порядок переприменения обновлённой версии:
--     1) UPDATE memories SET cluster_id = NULL WHERE cluster_id IS NOT NULL;
--        (разметка v1 недействительна — пересчитается первым же refresh_clusters)
--     2) DELETE FROM _migrations WHERE name = '022_clusters';  (или аналог)
--     3) применить этот файл (run.py). Старые объекты v1 —
--        assign_clusters(UUID, REAL), granule_trigrams, trigram_similarity —
--        файл дропает сам (DROP IF EXISTS, блок 0). Таблица clusters
--        сохраняется (IF NOT EXISTS): структура не менялась.
--   ⚡ горячий фикс 19.09 (на проде упал min(uuid) — агрегата нет в PG):
--        если v2 уже применена — достаточно выполнить из этого файла ТОЛЬКО
--        CREATE OR REPLACE FUNCTION assign_clusters_from_pairs (§3):
--        полное переприменение НЕ нужно, таблицы/индексы/триггеры не менялись.
--
-- ИСТОРИЯ v1 → v2 (прод-факты 2026-09-18, Рэй):
--   v1 считала близость в SQL: словесные 3-граммы + prefix-фильтр
--   кандидатов (AllPairs/PPJoin) + точный косинус. На живом корпусе
--   фильтр деградировал квадратично (однородные корпуса = частые
--   триграммы в лексикографических префиксах):
--     * dialogue_insights  1991 гранул — 198 с (впритык к soft_time_limit);
--     * project_meta       3319        — 620 с + отказ recursive CTE;
--     * code_knowledge     8130        — temp-разлив «no space left on
--       device» за 67 с.
--   При этом Qdrant (14.6К точек, 4096-dim COSINE) уже хранит те же
--   эмбеддинги и ищет ближайших за миллисекунды — он и есть правильный
--   источник кандидатов.
--
-- АРХИТЕКТУРА v2 — разделение поиска кандидатов и группировки:
--   * Кандидаты — Qdrant ANN (Python, MemoryRepository.refresh_clusters):
--     для каждой asserted-гранулы namespace top-K соседей (score ≥
--     threshold, батч query_batch_points). Пары (a_id, b_id, similarity)
--     заливаются COPY во врем. таблицу pg_temp._cluster_pairs
--     (паттерн _similarity_pairs из 016/merge_similar_granules.py).
--   * Группировка — SQL, assign_clusters_from_pairs: компоненты
--     связности, upsert кластеров, разметка cluster_id, метрики.
--   * Триграммный поиск (granule_trigrams / trigram_similarity /
--     prefix-фильтр) — УДАЛЁН: векторная близость считается там, где
--     лежат векторы.
--
-- КОМПОНЕНТЫ СВЯЗНОСТИ — итеративный min-label, НЕ recursive CTE:
--   recursive CTE с visited-массивом (паттерн 016) квадратичен на плотных
--   компонентах: каждая вершина таскает растущий массив и порождает
--   строки повторного достижения — это и дало отказ CTE/разлив temp на
--   project_meta. Здесь — цикл plpgsql «label propagation»: метка узла =
--   минимальный UUID компоненты; каждая итерация — O(E) GROUP BY,
--   сходимость за O(диаметр графа) итераций (кластеры HNSW top-K имеют
--   малый диаметр), метки строго убывают — зацикливание невозможно.
--
-- Содержимое:
--   0) Дроп объектов v1 (переприменение поверх)
--   1) Таблица clusters + индексы + триггер updated_at
--   2) memories.cluster_id UUID NULL FK → clusters.id (ON DELETE SET NULL)
--      + partial-индекс WHERE cluster_id IS NOT NULL
--   3) assign_clusters_from_pairs(p_namespace_id, p_min_members) —
--      разметка кластеров по готовым парам; идемпотентна, id кластеров
--      детерминированы (uuid(md5('cluster:'||ns||':'||group_key)))
--   4) update_updated_at_column — кластерная разметка не «омолаживает»
--      updated_at гранул
--   5) ALTER ... OWNER TO svc_athene_ai
--
-- Ограничения (сознательные):
--   * coherence = средняя близость по ОБНАРУЖЕННЫМ рёбрам группы (ANN
--     top-K), а не полная попарная матрица — полной в PG больше нет
--     (векторы в Qdrant); для плотной группы метрика совпадает по смыслу,
--     для цепочечной — честно отражает рыхлость рёбер, а не все пары.
--   * member_count кластера протухает при ретракции члена до следующего
--     пересчёта; чистят assign_clusters_from_pairs + orphans_cleanup.
--   * Инвариант «кластер и гранула из одного namespace» гарантируется
--     нормализацией пар в хранимке (JOIN с memories по namespace).
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 0. Дроп объектов v1 (переприменение поверх уже применённой 022)
-- ════════════════════════════════════════════════════════════
-- Таблицы/колонки не трогаем (clusters, memories.cluster_id — те же).

DROP FUNCTION IF EXISTS assign_clusters(UUID, REAL);
DROP FUNCTION IF EXISTS trigram_similarity(TEXT[], TEXT[]);
DROP FUNCTION IF EXISTS granule_trigrams(TEXT);

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

COMMENT ON TABLE clusters IS 'Level 2: кластеры близких гранул (порог cosine из Qdrant ANN, per-namespace). Создаются/пересчитываются assign_clusters_from_pairs; НЕ мержат гранулы.';
COMMENT ON COLUMN clusters.id IS 'PK. Для кластеров assign_clusters_from_pairs — детерминирован: uuid(md5(namespace_id + group_key)), повтор пересчёта = тот же id. DEFAULT — только для ручных вставок.';
COMMENT ON COLUMN clusters.namespace_id IS 'FK → namespaces.id. Кластер всегда в пределах одного namespace.';
COMMENT ON COLUMN clusters.label IS 'Авто-метка: топ-термин состава (частотное значимое слово) или cluster-<hash8>.';
COMMENT ON COLUMN clusters.summary IS 'LLM-саммари кластера — заполняется Фазой 4 (консолидация), сейчас NULL.';
COMMENT ON COLUMN clusters.member_count IS 'Число членов (гранул с cluster_id = id). Пересчитывается assign_clusters_from_pairs.';
COMMENT ON COLUMN clusters.coherence IS 'Средняя близость (cosine, Qdrant) по обнаруженным рёбрам группы, 0..1. Ниже порога у пар-«мостов» цепочек — сигнал рыхлости кластера.';
COMMENT ON COLUMN clusters.last_computed_at IS 'Момент последнего пересчёта assign_clusters_from_pairs.';

-- Индексы:
--   (namespace_id)        — WHERE namespace_id = $1: состав/чистка кластеров
--                           namespace (шаг чистки) и тул memory_cluster_list;
--   (member_count DESC)   — ORDER BY member_count DESC LIMIT n: обзор
--                           крупнейших кластеров namespace (Level 2 дашборд).
CREATE INDEX IF NOT EXISTS idx_clusters_namespace
    ON clusters (namespace_id);

CREATE INDEX IF NOT EXISTS idx_clusters_member_count
    ON clusters (member_count DESC);

-- Триггер updated_at (общий хелпер с 001; расширяется в §4)
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

COMMENT ON COLUMN memories.cluster_id IS 'FK → clusters.id (Level 2). NULL = гранула вне кластеров (одиночка). Инвариант «тот же namespace, что у кластера» гарантирует assign_clusters_from_pairs.';

-- Индекс под запросы состава кластера: WHERE cluster_id = $1 (карточка
-- кластера) и NOT EXISTS-подзапрос чистки assign_clusters_from_pairs.
-- Partial: одиночек (NULL) большинство — индекс не хранит их вовсе.
CREATE INDEX IF NOT EXISTS idx_memories_cluster_id
    ON memories (cluster_id)
    WHERE cluster_id IS NOT NULL;

-- ════════════════════════════════════════════════════════════
-- 3. assign_clusters_from_pairs — разметка кластеров namespace
-- ════════════════════════════════════════════════════════════
-- ВХОД: pg_temp._cluster_pairs (a_id UUID, b_id UUID, similarity REAL) —
-- пары близости, залитые вызывающей стороной (Python COPY, батчами).
-- Порог здесь НЕ проверяется: пары уже отфильтрованы по score ≥ threshold
-- на стороне Qdrant (score_threshold в query_batch_points).
--
-- Алгоритм:
--   шаг 0  снять прежнюю разметку namespace (cluster_id = NULL);
--   шаг 1  нормализация пар: канонический фильтр обеих сторон
--          (namespace + status='asserted' + valid_to IS NULL), a < b
--          (least/greatest), DISTINCT — защита от грязных входов и
--          расхождений PG ↔ Qdrant payload;
--   шаг 2  компоненты связности: итеративный min-label (label
--          propagation, см. шапку — почему не recursive CTE);
--          group_key = минимальный UUID компоненты;
--   шаг 3  фильтр member_count ≥ p_min_members (по умолчанию 2 —
--          одиночные вершины кластером не считаются);
--   шаг 4  метрики групп: member_count, coherence (avg similarity по
--          рёбрам группы), label (топ-термин, без стоп-слов);
--   шаг 5  upsert кластеров с ДЕТЕРМИНИРОВАННЫМ id
--          uuid(md5('cluster:' + namespace + ':' + group_key)) —
--          повтор пересчёта переиспользует тот же кластер, без дублей;
--   шаг 6  разметка членов (UPDATE memories.cluster_id);
--   шаг 7  удаление опустевших кластеров namespace;
--   шаг 8  возврат TABLE(cluster_id, member_count, coherence).
-- Служебные temp-таблицы _cluster_* создаются ON COMMIT DROP и
-- пересоздаются DROP+CREATE — безопасно при повторных вызовах
-- в одной сессии; имена в зарезервированной зоне "_".

CREATE OR REPLACE FUNCTION assign_clusters_from_pairs(
    p_namespace_id  UUID,
    p_min_members   INT DEFAULT 2
)
RETURNS TABLE(cluster_id UUID, member_count INT, coherence REAL)
LANGUAGE plpgsql
AS $$
DECLARE
    v_changed INT;
BEGIN
    IF p_min_members IS NULL OR p_min_members < 1 THEN
        RAISE EXCEPTION 'p_min_members должен быть ≥ 1, получено %', p_min_members;
    END IF;

    -- шаг 0: пересчёт с чистого листа
    -- (колонки квалифицированы алиасом: имена совпадают с OUT-параметрами
    --  функции, plpgsql иначе роняет «неоднозначная ссылка»)
    UPDATE memories AS m
    SET cluster_id = NULL
    WHERE m.namespace_id = p_namespace_id
      AND m.cluster_id IS NOT NULL;

    -- шаг 1: нормализация пар (канонический фильтр + a < b + дедуп)
    DROP TABLE IF EXISTS _cluster_norm;
    CREATE TEMP TABLE _cluster_norm ON COMMIT DROP AS
    SELECT DISTINCT least(t.a_id, t.b_id)   AS a_id,
                    greatest(t.a_id, t.b_id) AS b_id,
                    t.similarity
    FROM pg_temp._cluster_pairs t
    JOIN memories ma ON ma.id = t.a_id
    JOIN memories mb ON mb.id = t.b_id
    WHERE ma.namespace_id = p_namespace_id AND mb.namespace_id = p_namespace_id
      AND ma.status = 'asserted' AND mb.status = 'asserted'
      AND ma.valid_to IS NULL    AND mb.valid_to IS NULL
      AND t.similarity >= 0 AND t.similarity <= 1
      AND t.a_id <> t.b_id;

    -- нет пар — нет кластеров: подчистить опустевшие и вернуть 0 строк
    IF NOT EXISTS (SELECT 1 FROM _cluster_norm) THEN
        DELETE FROM clusters c
        WHERE c.namespace_id = p_namespace_id
          AND NOT EXISTS (SELECT 1 FROM memories m WHERE m.cluster_id = c.id);
        RETURN;
    END IF;

    -- шаг 2a: симметризованные рёбра (материализуем — цикл их перечитывает)
    DROP TABLE IF EXISTS _cluster_edges;
    CREATE TEMP TABLE _cluster_edges ON COMMIT DROP AS
    SELECT a_id AS src, b_id AS dst FROM _cluster_norm
    UNION
    SELECT b_id, a_id FROM _cluster_norm;

    CREATE INDEX IF NOT EXISTS idx__cluster_edges_src ON _cluster_edges (src);

    -- шаг 2b: компоненты связности — min-label propagation.
    -- Метка узла стартует с самого узла; каждая итерация протягивает
    -- минимальную метку соседей; сходимость — метки перестали убывать.
    DROP TABLE IF EXISTS _cluster_groups;
    CREATE TEMP TABLE _cluster_groups ON COMMIT DROP AS
    SELECT node_id AS member_id, node_id AS group_key
    FROM (SELECT DISTINCT src AS node_id FROM _cluster_edges) nodes;

    LOOP
        WITH candidates AS (
            -- ⚠ агрегата min(UUID) в PostgreSQL НЕТ (инцидент 19.09:
            -- UndefinedFunctionError на проде). Каст в text: каноничная
            -- lowercase-hex форма UUID лексикографически эквивалентна
            -- побайтовому uuid-порядку, значит min(text)::uuid даёт ровно
            -- тот же минимальный UUID компоненты — детерминизм min-label
            -- сохраняется; сравнение c.new_label < t.group_key ниже —
            -- нативное uuid, консистентно с текстовым порядком.
            SELECT e.dst AS node_id, min(g.group_key::text)::UUID AS new_label
            FROM _cluster_edges e
            JOIN _cluster_groups g ON g.member_id = e.src
            GROUP BY e.dst
        )
        UPDATE _cluster_groups t
        SET group_key = c.new_label
        FROM candidates c
        WHERE t.member_id = c.node_id
          AND c.new_label < t.group_key;
        GET DIAGNOSTICS v_changed = ROW_COUNT;
        EXIT WHEN v_changed = 0;
    END LOOP;

    -- шаг 3+4: метрики групп ≥ p_min_members + карта кластеров
    DROP TABLE IF EXISTS _cluster_map;
    CREATE TEMP TABLE _cluster_map ON COMMIT DROP AS
    WITH member_counts AS (
        SELECT group_key, count(*)::INT AS member_count
        FROM _cluster_groups
        GROUP BY group_key
        HAVING count(*) >= p_min_members
    ),
    edge_avg AS (
        -- coherence: средняя близость рёбер внутри группы
        SELECT g.group_key, round(avg(n.similarity)::numeric, 4)::REAL AS coherence
        FROM _cluster_norm n
        JOIN _cluster_groups g ON g.member_id = n.a_id
        JOIN _cluster_groups h ON h.member_id = n.b_id AND h.group_key = g.group_key
        GROUP BY g.group_key
    ),
    terms AS (
        -- слова-кандидаты метки: длина ≥ 5, без служебных
        SELECT gr.group_key, w.word
        FROM _cluster_groups gr
        JOIN member_counts mc ON mc.group_key = gr.group_key
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
        SELECT mc.group_key, mc.member_count, ea.coherence,
               md5('cluster:' || p_namespace_id::text || ':' || mc.group_key::text) AS h
        FROM member_counts mc
        LEFT JOIN edge_avg ea ON ea.group_key = mc.group_key
    )
    SELECT hs.group_key, hs.member_count, hs.coherence, hs.h,
           (substr(hs.h, 1, 8) || '-' || substr(hs.h, 9, 4) || '-' || substr(hs.h, 13, 4)
            || '-' || substr(hs.h, 17, 4) || '-' || substr(hs.h, 21, 12))::UUID AS cid,
           coalesce(tt.word, 'cluster-' || substr(hs.h, 1, 8)) AS label
    FROM hashed hs
    LEFT JOIN top_terms tt ON tt.group_key = hs.group_key;

    -- шаг 5: upsert кластеров (детерминированный id — конфликт сам с собой
    -- при пересчёте, чужих коллизий нет: md5 от namespace+group_key)
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

COMMENT ON FUNCTION assign_clusters_from_pairs(UUID, INT) IS 'Разметка кластеров Level 2 по namespace из готовых пар близости (pg_temp._cluster_pairs, заливает Python из Qdrant ANN). Порог пары уже применён на стороне Qdrant. НЕ мержит гранулы. Идемпотентна: детерминированные id кластеров uuid(md5(namespace:group_key)). Возвращает (cluster_id, member_count, coherence).';

-- ════════════════════════════════════════════════════════════
-- 4. update_updated_at_column — семантическое расширение
-- ════════════════════════════════════════════════════════════
-- Было (001/003): любое UPDATE дёргает updated_at. Проблема: ночная
-- assign_clusters_from_pairs перезаписывает cluster_id у тысяч гранул —
-- без этого фикса ВСЕ кластеризованные гранулы «омолаживались» бы и
-- всплывали в сортировках свежести (project_context_snapshot:
-- ORDER BY importance, updated_at; веб-морда Фазы 5).
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
-- 5. Владелец объектов — svc_athene_ai
-- ════════════════════════════════════════════════════════════
-- Инцидент 021: run.py применяет миграции от svc_athene_ai; функция,
-- созданная чужой ролью, роняла автопрогон. Здесь — владелец ВСЕХ
-- объектов, создаваемых миграцией. Роль существует с 013.

ALTER TABLE clusters OWNER TO svc_athene_ai;

ALTER FUNCTION assign_clusters_from_pairs(UUID, INT) OWNER TO svc_athene_ai;
ALTER FUNCTION update_updated_at_column() OWNER TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- ВНИМАНИЕ: откат сносит разметку кластеров и сами кластеры
-- (summary Фазы 4 ещё не существует — терять нечего).
--
-- DROP FUNCTION IF EXISTS assign_clusters_from_pairs(UUID, INT);
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
