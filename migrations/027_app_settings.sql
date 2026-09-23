-- ============================================================
-- 027_app_settings.sql — runtime-настройки + профили (Ф1)
-- ============================================================
-- Дата: 2026-09-23
-- Исполнитель: Нора (db-architect)
-- Задача: Ф1 трёхслойной конфигурации (источник истины —
--         docs/SETTINGS_REGISTRY.md, аудит Момо 2026-09-23;
--         схема утверждена Мастером).
--
-- Содержимое:
--   1) app_settings — 97 runtime-ключей реестра §2: текущее значение,
--      тип, группа, русские title/description, канонический дефолт,
--      min/max/enum, флаги is_dangerous / requires_restart;
--   2) app_settings_notify() + trg_app_settings_notify — атомарное
--      оповещение изменений: pg_notify('settings_changed', key) на
--      INSERT / UPDATE / DELETE — кэш API-слоя инвалидируется
--      событием, а не TTL;
--   3) app_settings_profiles — профили-снапшоты значений (values
--      JSONB); builtin-профиль 'default' («Заводские настройки»)
--      сидируется каноническими дефолтами всех 97 ключей;
--   4) Сидинг app_settings: 97 INSERT-строк, ON CONFLICT (key)
--      DO NOTHING — повторный прогон НЕ перетирает ручные правки
--      (значения — бит-в-бит из реестра, русские тексты — в UI).
--
-- ── РЕШЕНИЕ: value/default/min/max/enum — JSONB ───────────────────
-- Ключи пяти типов (int/float/bool/str/json) в одной таблице:
-- типизированные колонки дали бы 5 nullable-пар или 5 таблиц.
-- JSONB хранит любой тип значением (числа — числами JSON, не
-- строками), min/max сравниваются приложением по value_type.
-- Альтернатива TEXT + парсинг отброшена: теряется типизация на
-- уровне хранения (0.7 оставался бы строкой). value_type CHECK —
-- закрытая доменная модель (int/float/bool/str/json), не набор
-- данных: CHECK уместен и защищает от опечатки при будущих сидингах.
--
-- ── РЕШЕНИЕ: БЕЗ CHECK на group_key ───────────────────────────────
-- Группы — данные реестра (11 значений, §2), а не доменная модель:
-- CHECK стал бы третьей точкой синхронизации (реестр, сидинг, БД)
-- и требовал бы ALTER CONSTRAINT на каждую новую группу. Реестр +
-- процедура §8 (SETTINGS_REGISTRY) контролируют набор групп.
--
-- ── РЕШЕНИЕ: профили — блоб {key: value}, а не построчная таблица ──
-- 97 ключей: diff «профиль vs текущее» — одно jsonb-сравнение на лету
-- приложением; построчная таблица (profile_id, key, value) — это
-- 97 строк × N профилей ради отображения, которое считается одним
-- сравнением блобов, плюс JOIN на каждый экран. Профиль читается и
-- пишется всегда целиком (снапшот-семантика: apply = записать весь
-- набор). Валидация ключей/типов/min-max при apply — на стороне API
-- (реестр и резолвер живут в коде, не в БД).
--
-- ── РЕШЕНИЕ: DELETE-ветка триггера — OLD.key ──────────────────────
-- AFTER INSERT OR UPDATE OR DELETE: на DELETE записи NEW не
-- существует (обращение NEW.key — runtime-ошибка plpgsql «record
-- new is not assigned»), ключ берём из OLD. COALESCE(NEW.key,
-- OLD.key) отклонён: вычисление обоих операндов упадёт на DELETE.
-- CASE TG_OP исполняет только нужную ветку. RETURN NULL — для
-- AFTER row-триггера возвращаемое значение игнорируется (документация
-- PG), триггер не может отклонить уже случившуюся мутацию.
--
-- ── РЕШЕНИЕ: без триггера автообновления updated_at ───────────────
-- updated_at/updated_by пишутся API-слоем атомарно одним UPDATE
-- (кто и когда — одна семантика). Автотриггер скрывал бы updated_by
-- от «тихих» UPDATE и рассинхронизовал пару полей.
--
-- ── Права и владелец ──────────────────────────────────────────────
-- run.py применяет миграции от svc_athene_ai (DATABASE_URL, инцидент
-- 021) — новые объекты наследуют владельца автоматически. DO-блок в
-- хвосте переустанавливает владельца явно (стенды, гоняющие раннер
-- от postgres); безусловный ALTER OWNER (паттерн 026) падал бы на
-- ephemeral-стендах без роли — поэтому условный по pg_roles.
--
-- ── Блокировки и идемпотентность ──────────────────────────────────
-- Таблицы новые (живых таблиц не касаемся) — ACCESS EXCLUSIVE только
-- на создание, миллисекунды. CREATE INDEX без CONCURRENTLY осознанно:
-- run.py исполняет up-секцию одной транзакцией, CONCURRENTLY внутри
-- транзакции PG запрещает; индекс строится по ПУСТОЙ таблице до
-- сидинга. Повторный прогон файла целиком — no-op: IF NOT EXISTS /
-- OR REPLACE / DROP TRIGGER IF EXISTS + CREATE / ON CONFLICT DO
-- NOTHING на всех операторах.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Таблица app_settings
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS app_settings (
    key              TEXT PRIMARY KEY,
    value            JSONB NOT NULL,
    value_type       TEXT NOT NULL,
    group_key        TEXT NOT NULL,
    title_ru         TEXT NOT NULL,
    description_ru   TEXT NOT NULL,
    default_value    JSONB NOT NULL,
    min_value        JSONB,
    max_value        JSONB,
    enum_values      JSONB,
    is_dangerous     BOOLEAN NOT NULL DEFAULT false,
    requires_restart BOOLEAN NOT NULL DEFAULT false,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by       TEXT NOT NULL DEFAULT 'seed',

    CONSTRAINT chk_app_settings_value_type
        CHECK (value_type IN ('int', 'float', 'bool', 'str', 'json'))
);

COMMENT ON TABLE app_settings IS 'Runtime-настройки selti (Ф1): 97 ключей реестра docs/SETTINGS_REGISTRY.md §2. Резолв значения: env/compose (явно заданный) > value > default_value (= дефолт config.py). Фундамент (секреты/инфра) здесь не живёт (реестр §3).';
COMMENT ON COLUMN app_settings.key IS 'Имя ключа = имя поля config.py; для beat — schedule.<task_name>';
COMMENT ON COLUMN app_settings.value IS 'Текущее runtime-значение (JSONB: числа — числами, str — JSON-строкой, beat — {type: interval|crontab, ...}). Сидинг = default_value; ручные правки — через /api/settings (запись инвалидирует кэш pg_notify settings_changed).';
COMMENT ON COLUMN app_settings.value_type IS 'Доменная модель типов значения: int | float | bool | str | json';
COMMENT ON COLUMN app_settings.group_key IS 'Группа реестра §2 (search, dedup, lifecycle, cluster, linker, edge, cloud, map, celery, schedule, api_caps) — без CHECK: группы — данные реестра, не модель (см. шапку)';
COMMENT ON COLUMN app_settings.title_ru IS 'Русское название для UI';
COMMENT ON COLUMN app_settings.description_ru IS 'Русское описание-тултип «?» для UI';
COMMENT ON COLUMN app_settings.default_value IS 'Канонический дефолт — бит-в-бит config.py на момент миграции. Reset-to-default и builtin-профиль default строятся из него.';
COMMENT ON COLUMN app_settings.min_value IS 'Нижняя граница (JSONB-число) для int/float; NULL — без ограничения';
COMMENT ON COLUMN app_settings.max_value IS 'Верхняя граница (JSONB-число) для int/float; NULL — без ограничения';
COMMENT ON COLUMN app_settings.enum_values IS 'Список допустимых значений (JSONB-массив) для str-enum; NULL — свободный текст';
COMMENT ON COLUMN app_settings.is_dangerous IS 'Опасный ключ (реестр §1): запись/сброс требует confirm=true в API; UI — бейдж «опасно» + модалка';
COMMENT ON COLUMN app_settings.requires_restart IS 'Применяется при старте процесса (worker/beat/uvicorn): значение в БД меняется, эффект — после рестарта; UI — бейдж «нужен рестарт»';
COMMENT ON COLUMN app_settings.updated_at IS 'Момент последнего изменения value (пишет API вместе с updated_by; сидинг — now() миграции)';
COMMENT ON COLUMN app_settings.updated_by IS 'Автор изменения: seed (миграция) | субъект API (user/agent); аудит без отдельной таблицы';

-- Индекс группировки: единственный не-PK запрос к таблице — выборка
-- записей группы в API (группирует фронт по полю group из GET /api/settings;
-- серверного ?group=-фильтра нет). 97 строк, рост
-- только новыми ключами (реестр §8) — b-tree достаточно, избыточных
-- индексов (title, is_dangerous) не заводим: запросов по ним нет.
CREATE INDEX IF NOT EXISTS idx_app_settings_group_key
    ON app_settings (group_key);

-- ════════════════════════════════════════════════════════════
-- 2. Атомарное оповещение об изменениях (pg_notify)
-- ════════════════════════════════════════════════════════════
-- Канал settings_changed, payload — ключ изменённой строки. Слушает
-- пул API-слоя (Сона, Ф2): событие приходит в той же транзакции, что
-- и мутация, — кэш настроек инвалидируется атомарно, без TTL-гонки
-- «прочитал старое после коммита нового».

CREATE OR REPLACE FUNCTION app_settings_notify()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- DELETE: записи NEW не существует — ключ из OLD; INSERT/UPDATE —
    -- из NEW. CASE исполняет одну ветку: обращение к отсутствующей
    -- записи в невыполняемой ветке не происходит.
    PERFORM pg_notify(
        'settings_changed',
        CASE TG_OP WHEN 'DELETE' THEN OLD.key ELSE NEW.key END
    );
    RETURN NULL;  -- AFTER row-триггер: возвращаемое значение игнорируется
END;
$$;

DROP TRIGGER IF EXISTS trg_app_settings_notify ON app_settings;

CREATE TRIGGER trg_app_settings_notify
    AFTER INSERT OR UPDATE OR DELETE ON app_settings
    FOR EACH ROW
    EXECUTE FUNCTION app_settings_notify();

COMMENT ON FUNCTION app_settings_notify() IS 'Оповещение settings_changed с ключом изменённой настройки (payload ≤ 8000 байт — ключи короткие). DELETE берёт OLD.key: NEW на DELETE не существует. Сидинг 027 шлёт 97 событий — безвредно: слушателя в момент миграции нет.';

-- ════════════════════════════════════════════════════════════
-- 3. Таблица app_settings_profiles
-- ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS app_settings_profiles (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    is_builtin  BOOLEAN NOT NULL DEFAULT false,
    values      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at  TIMESTAMPTZ,

    CONSTRAINT uq_app_settings_profiles_name UNIQUE (name)
);

COMMENT ON TABLE app_settings_profiles IS 'Профили-снапшоты настроек: values — блоб {key: value} всех runtime-ключей (97). Построчная таблица отклонена: diff считается на лету одним jsonb-сравнением, профиль пишется/читается всегда целиком (снапшот-семантика). Валидация при apply (ключи/типы/min-max/enum) — на стороне API.';
COMMENT ON COLUMN app_settings_profiles.name IS 'Системное имя профиля (unique); name=''default'' зарезервирован за builtin';
COMMENT ON COLUMN app_settings_profiles.description IS 'Человекочитаемое описание профиля для UI';
COMMENT ON COLUMN app_settings_profiles.is_builtin IS 'true — системный профиль (сидируется миграцией, API не даёт удалять/переименовывать)';
COMMENT ON COLUMN app_settings_profiles.values IS 'Снапшот {key: value} всех runtime-ключей. builtin default = default_value всех 97 ключей app_settings (канонические дефолты config.py)';
COMMENT ON COLUMN app_settings_profiles.created_at IS 'Момент создания профиля';
COMMENT ON COLUMN app_settings_profiles.applied_at IS 'Момент последнего apply профиля к app_settings (NULL — не применялся)';

-- ════════════════════════════════════════════════════════════
-- 4. Сидинг app_settings — 97 runtime-ключей реестра §2
-- ════════════════════════════════════════════════════════════
-- Один INSERT на группу (структура = разделы реестра §2.1–§2.11).
-- value = default_value (runtime-слой стартует с канонических
-- дефолтов). ON CONFLICT (key) DO NOTHING: повторный прогон и
-- будущие ре-сидинги не перетирают ручные правки value.
-- Колонки: key, value, value_type, group_key, title_ru,
-- description_ru, default_value, min_value, max_value, enum_values,
-- is_dangerous, requires_restart.

INSERT INTO app_settings
    (key, value, value_type, group_key, title_ru, description_ru,
     default_value, min_value, max_value, enum_values,
     is_dangerous, requires_restart)
VALUES
    -- ── §2.1 Поиск и ранжирование — search (10) ──
    ('search_default_threshold', '0.7', 'float', 'search',
     'Порог релевантности поиска',
     'Минимальный балл схожести, ниже которого гранулы не попадают в выдачу, если клиент не задал свой порог.',
     '0.7', '0', '1', NULL, false, false),
    ('hybrid_search_enabled', 'true', 'bool', 'search',
     'Гибридный поиск',
     'Включает гибридный поиск Фазы 1 (плотный вектор + полнотекстовый канал со слиянием RRF). Выключение откатывает на чистый векторный путь Qdrant — фича-флаг отката.',
     'true', NULL, NULL, NULL, false, false),
    ('hybrid_prefetch', '100', 'int', 'search',
     'Предвыборка кандидатов',
     'Сколько кандидатов набирается в каждом канале до слияния RRF. Больше — точнее ранжирование, дороже запрос.',
     '100', '10', '1000', NULL, false, false),
    ('rrf_k', '60', 'int', 'search',
     'Коэффициент RRF',
     'Константа сглаживания формулы слияния рангов: score = Σ 1/(k + rank). Меньше k — сильнее вес верхних позиций.',
     '60', '1', '500', NULL, false, false),
    ('mmr_lambda', '0.7', 'float', 'search',
     'Баланс MMR',
     'Баланс релевантности и разнообразия выдачи при MMR-переранжировании: 1.0 — чистая релевантность, ниже — больше разнообразия.',
     '0.7', '0', '1', NULL, false, false),
    ('recency_decay_rate', '0.995', 'float', 'search',
     'Затухание свежести (дефолт)',
     'Ежедневный множитель веса гранулы в ранжировании для неймспейсов без своего override. 0.995 ≈ −0.5% в день.',
     '0.995', '0.9', '1.0', NULL, false, false),
    ('recency_decay_rates',
     '{"default": 0.995, "user_facts": 0.999, "project_meta": 0.998, "code_knowledge": 0.995, "dialogue_insights": 0.99, "infrastructure": 0.993}',
     'json', 'search',
     'Затухание по неймспейсам',
     'Индивидуальные скорости затухания свежести на неймспейс. Факты о пользователе живут дольше (0.999), инсайты разговоров устаревают быстрее (0.99).',
     '{"default": 0.995, "user_facts": 0.999, "project_meta": 0.998, "code_knowledge": 0.995, "dialogue_insights": 0.99, "infrastructure": 0.993}',
     NULL, NULL, NULL, false, false),
    ('importance_multipliers',
     '{"default": 1.0, "user_facts": 1.2, "project_meta": 1.1, "code_knowledge": 1.0, "dialogue_insights": 0.8, "infrastructure": 1.0}',
     'json', 'search',
     'Приоритет неймспейсов',
     'Множитель важности гранулы в ранжировании по её неймспейсу: факты пользователя (1.2) всплывают выше разговорного контента (0.8).',
     '{"default": 1.0, "user_facts": 1.2, "project_meta": 1.1, "code_knowledge": 1.0, "dialogue_insights": 0.8, "infrastructure": 1.0}',
     NULL, NULL, NULL, false, false),
    ('search_activation_enabled', 'false', 'bool', 'search',
     'Ассоциативный поиск',
     'Включает стратегию `activation` тула memory_search: seed-гранулы расширяются соседями по графу связей (Personalized PageRank). До включения стратегия возвращает внятную ошибку.',
     'false', NULL, NULL, NULL, false, false),
    ('search_activation_seed_limit', '10', 'int', 'search',
     'Лимит seed-гранул',
     'Сколько лучших прямых попаданий берётся как затравка для ассоциативного расширения.',
     '10', '4', '30', NULL, false, false),

    -- ── §2.2 Дедупликация — dedup (3) ──
    ('dedup_enabled', 'true', 'bool', 'dedup',
     'Дедупликация записей',
     'При записи гранула сравнивается с существующими по смысловой близости; дубль не создаётся. Выключение допускает дубли — включать осознанно.',
     'true', NULL, NULL, NULL, true, false),
    ('dedup_threshold', '0.95', 'float', 'dedup',
     'Порог дедупликации',
     'Косинусная близость, выше которой новая гранула считается дублем существующей.',
     '0.95', '0.5', '1.0', NULL, false, false),
    ('dedup_thresholds',
     '{"default": 0.95, "user_facts": 0.90, "dialogue_insights": 0.85, "code_knowledge": 0.95, "project_meta": 0.90, "infrastructure": 0.95}',
     'json', 'dedup',
     'Пороги по неймспейсам',
     'Индивидуальные пороги дедупликации: для разговорных инсайтов планка ниже (0.85 — формулировки варьируются сильнее), для кода — выше.',
     '{"default": 0.95, "user_facts": 0.90, "dialogue_insights": 0.85, "code_knowledge": 0.95, "project_meta": 0.90, "infrastructure": 0.95}',
     NULL, NULL, NULL, false, false),

    -- ── §2.3 Жизненный цикл и GC — lifecycle (7) ──
    ('supersession_confidence_factor', '0.9', 'float', 'lifecycle',
     'Наследование уверенности',
     'При создании новой версии гранулы уверенность наследуется с этим множителем (cap 0..1): каждое перепрохождение факта через систему стоит части уверенности.',
     '0.9', '0', '1', NULL, false, false),
    ('confidence_decay_floor', '0.1', 'float', 'lifecycle',
     'Пол затухания уверенности',
     'Ниже этого уровня ежедневное затухание останавливается: гранула не выродится в ноль, а станет кандидатом на ревизию (mark_stale).',
     '0.1', '0', '0.5', NULL, false, false),
    ('stale_threshold', '0.3', 'float', 'lifecycle',
     'Порог устаревания',
     'Уверенность ниже порога + нет доступа `stale_days` дней → гранула помечается устаревшей и попадает в очередь ревизии.',
     '0.3', '0', '1', NULL, false, false),
    ('stale_days', '30', 'int', 'lifecycle',
     'Дней без доступа',
     'Сколько дней гранула должна не запрашиваться, чтобы считаться заброшенной при упавшей уверенности.',
     '30', '7', '365', NULL, false, false),
    ('gc_purge_enabled', 'false', 'bool', 'lifecycle',
     'Мастер-кран физического удаления',
     'False — физическое удаление знаний невозможно в принципе (полная история сохраняется всегда). True ОТКРЫВАЕТ hard delete устаревших версий. Включать только осознанно после бэкапа.',
     'false', NULL, NULL, NULL, true, false),
    ('gc_mode', '"disabled"', 'str', 'lifecycle',
     'Режим GC',
     'disabled — ничего не удаляется (только отчёт кандидатов); hard — физическое удаление superseded-версий старше retention (работает только при включённом мастер-кране); soft — зарезервирован будущими фазами.',
     '"disabled"', NULL, NULL, '["disabled", "soft", "hard"]', true, false),
    ('gc_retention_days', '90', 'int', 'lifecycle',
     'Срок хранения версий',
     'Сколько дней после замены версии GC в режиме hard держит superseded-копию перед физическим удалением.',
     '90', '7', '3650', NULL, true, false),

    -- ── §2.4 Кластеризация — cluster (3) ──
    ('cluster_threshold', '0.92', 'float', 'cluster',
     'Порог близости кластеров',
     'Минимальная близость эмбеддингов (score в Qdrant) для попадания соседа в кандидаты кластера при ночной разметке.',
     '0.92', '0.5', '1.0', NULL, false, false),
    ('cluster_top_k', '10', 'int', 'cluster',
     'Соседей на гранулу',
     'Сколько ближайших соседей рассматривается для каждой гранулы при сборке кластеров. Больше — крупнее кластеры, дольше расчёт.',
     '10', '3', '50', NULL, false, false),
    ('cluster_min_members', '2', 'int', 'cluster',
     'Минимум участников',
     'Группы меньше этого размера кластером не считаются (остаются одиночными вершинами).',
     '2', '2', '10', NULL, false, false),

    -- ── §2.5 Линкер — linker (20) ──
    ('linker_enabled', 'true', 'bool', 'linker',
     'Автолинкинг',
     'Мастер-выключатель автолинкинга новых гранул. Выключение останавливает построение новых связей знаний — включать осознанно.',
     'true', NULL, NULL, NULL, true, false),
    ('linker_l1a_enabled', 'true', 'bool', 'linker',
     'Слой L1a (синонимы)',
     'Автосвязи «related_to» по ANN-поиску синонимов эмбеддингов. Выключается при риске шума связей (флаг отката ADR-019.1).',
     'true', NULL, NULL, NULL, false, false),
    ('linker_l1c_enabled', 'true', 'bool', 'linker',
     'Слой L1c (совместные упоминания)',
     'Связи между гранулами, встречавшимися в одном контексте (co-occurrence).',
     'true', NULL, NULL, NULL, false, false),
    ('linker_l2_manual', 'true', 'bool', 'linker',
     'Ручной режим L2',
     'True — «серую зону» близости разбирает человек-агент тулами memory_linker_review/verdict; False — очередь отдаётся LLM-воркеру. Режим назначен приказом Мастера 20.09.',
     'true', NULL, NULL, NULL, false, false),
    ('linker_synonym_threshold', '0.80', 'float', 'linker',
     'Порог синонимии L1a',
     'Нижняя граница «серой зоны»: ниже — тишина (шум), выше начинается auto-related_to. Должен быть ниже вердиктного порога.',
     '0.80', '0.5', '0.95', NULL, false, false),
    ('linker_verdict_threshold', '0.85', 'float', 'linker',
     'Порог LLM-вердикта',
     'Верхняя граница auto-слоя: от этого порога до порога дедупликации пару связывает только явный вердикт (LLM или человек).',
     '0.85', '0.7', '0.99', NULL, false, false),
    ('linker_ann_limit', '10', 'int', 'linker',
     'Соседей ANN на гранулу',
     'Верхний кап кандидатов синонимии из векторного поиска на одну новую гранулу.',
     '10', '3', '50', NULL, false, false),
    ('linker_top_k', '5', 'int', 'linker',
     'Кандидатов в L2-промпте',
     'Сколько пар-кандидатов попадает в один LLM-запрос вердикта.',
     '5', '1', '20', NULL, false, false),
    ('linker_cooccurrence_cap', '10', 'int', 'linker',
     'Кап co-occurrence рёбер',
     'Максимум L1c-рёбер на гранулу (приоритет свежим соседям) — защита от разрастания графа.',
     '10', '1', '100', NULL, false, false),
    ('linker_reconciler_batch', '500', 'int', 'linker',
     'Батч резолва имён',
     'Сколько «висячих» ссылок name_reconciler обрабатывает за итерацию кампании.',
     '500', '50', '5000', NULL, false, false),
    ('linker_reconciler_dry_run', 'true', 'bool', 'linker',
     'Резолв имён: сухой режим',
     'True — кампания только строит отчёт, ничего не переписывает. False — боевой резолв ссылок. Переключать после ручной проверки первого отчёта.',
     'true', NULL, NULL, NULL, true, false),
    ('linker_l2_batch', '20', 'int', 'linker',
     'Размер L2-батча',
     'Сколько элементов серой зоны обрабатывается за прогон воркера/агента.',
     '20', '1', '200', NULL, false, false),
    ('linker_l2_max_attempts', '3', 'int', 'linker',
     'Попыток L2-вердикта',
     'Сколько раз сбойный элемент очереди возвращается в обработку, прежде чем отбрасывается с WARNING.',
     '3', '1', '10', NULL, false, false),
    ('linker_l1c_gate_min', '0.30', 'float', 'linker',
     'Гейт L1c по косинусу',
     'Ребро co-occurrence живёт только при косинусной близости пары ≥ порога (одна сессия ≠ смысловая близость). 0.0 — гейт выключен.',
     '0.30', '0', '0.9', NULL, false, false),
    ('linker_l1c_prune_batch', '256', 'int', 'linker',
     'Батч чистки L1c-истории',
     'Размер пакета исторических пар при one-off кампании перепроверки co-occurrence гейтом.',
     '256', '32', '2048', NULL, false, false),
    ('linker_verdict_cache_ttl', '2592000', 'int', 'linker',
     'Кеш вердиктов (сек)',
     'Сколько секунд хранится вердикт по паре (30 дней) — повторный разбор той же пары не тратит LLM.',
     '2592000', '3600', '7776000', NULL, false, false),
    ('linker_llm_base_url', '""', 'str', 'linker',
     'URL LLM-провайдера L2',
     'Адрес OpenAI-совместимого API для LLM-вердиктов. Пусто = L2-автоматика отключена (очередь копится для ручного разбора). Изменение требует пересоздания клиента (рестарт).',
     '""', NULL, NULL, NULL, false, true),
    ('linker_llm_model', '"glm-4.7-flash"', 'str', 'linker',
     'Модель L2-вердикта',
     'Имя модели LLM для вердиктов серой зоны. Применяется при рестарте (пересоздание клиента).',
     '"glm-4.7-flash"', NULL, NULL, NULL, false, true),
    ('linker_llm_timeout', '10.0', 'float', 'linker',
     'Таймаут LLM (сек)',
     'Сколько секунд ждётся ответ LLM на один вердикт. Применяется при рестарте.',
     '10.0', '1', '120', NULL, false, true),
    ('linker_llm_retries', '1', 'int', 'linker',
     'Ретраев на LLM-запрос',
     'Сколько повторных попыток делается при сбое LLM-запроса. Применяется при рестарте.',
     '1', '0', '5', NULL, false, true),

    -- ── §2.6 Рёбра графа — edge (15) ──
    ('edge_lifecycle_enabled', 'false', 'bool', 'edge',
     'Жизнь рёбер: мастер-флаг',
     'Включает цикл жизни рёбер: затухание неиспользуемых связей, усиление используемых, отсечение мёртвых. False — всё молчит (до стенд-репетиции).',
     'false', NULL, NULL, NULL, true, false),
    ('edge_reinforcement_enabled', 'true', 'bool', 'edge',
     'Усиление рёбер',
     'Касание ребра при использовании увеличивает его вес (w += (1−w)×α) — частые связи крепнут. Работает только при включённой жизни рёбер.',
     'true', NULL, NULL, NULL, false, false),
    ('edge_decay_lambda', '0.02', 'float', 'edge',
     'Скорость затухания рёбер',
     'λ в формуле w_eff = w·exp(−λ·дней): 0.02 ≈ ребро без использования теряет ~2% веса в день.',
     '0.02', '0', '1', NULL, false, false),
    ('edge_decay_lambda_min', '0.002', 'float', 'edge',
     'Насыщение частых рёбер',
     'Нижний предел эффективной λ: часто используемые ребра затухают медленнее (λ/(1+used_count), но не ниже предела).',
     '0.002', '0', '0.1', NULL, false, false),
    ('edge_decay_floor', '0.05', 'float', 'edge',
     'Порог отсечения ребра',
     'Эффективный вес ниже порога → ребро-кандидат на отсечение кампанией (пишется pruned_at, не DELETE).',
     '0.05', '0', '0.5', NULL, false, false),
    ('edge_prune_min_age_days', '30', 'int', 'edge',
     'Возраст отсечения',
     'Кандидат на отсечение — ребро старше этого возраста (молодые связи дают шанс проявиться).',
     '30', '7', '365', NULL, false, false),
    ('edge_prune_dry_run', 'true', 'bool', 'edge',
     'Отсечение: сухой режим',
     'True — кампания только считает кандидатов и пишет отчёт. False — боевой режим: рёбра помечаются pruned_at. Включать после ревизии отчёта.',
     'true', NULL, NULL, NULL, true, false),
    ('edge_reinforce_alpha', '0.2', 'float', 'edge',
     'Сила касания',
     'Насколько одно использование подтягивает вес ребра: w += (1−w)×α, cap 1.0.',
     '0.2', '0', '1', NULL, false, false),
    ('edge_reinforce_flow_min', '0.001', 'float', 'edge',
     'Порог потока касания',
     'При ассоциативном поиске ребро считается «использованным», если через него прошёл поток ≥ порога и оба конца в топ-K выдачи.',
     '0.001', '0', '0.1', NULL, false, false),
    ('traverse_activation_enabled', 'false', 'bool', 'edge',
     'Ассоциативный обход графа',
     'Включает strategy="activation" обхода и поиска: PPR-распространение по живому графу. До включения — внятная ошибка вместо тихого fallback.',
     'false', NULL, NULL, NULL, false, false),
    ('ppr_damping', '0.85', 'float', 'edge',
     'Демпфинг PPR',
     'Вероятность продолжить блуждание по графу на каждом шаге Personalized PageRank. Классика 0.85.',
     '0.85', '0.5', '0.99', NULL, false, false),
    ('traverse_activation_iterations', '25', 'int', 'edge',
     'Итераций PPR',
     'Число итераций power iteration. 25 даёт точность топ-3 ±0.02 (0.85^25≈0.017); больше — точнее, дольше (~+1.1 мс/106k рёбер за 25).',
     '25', '5', '100', NULL, false, false),
    ('traverse_activation_top_k', '50', 'int', 'edge',
     'Топ-K активации',
     'Сколько узлов возвращается ассоциативным расширением сверх seed-выдачи.',
     '50', '10', '500', NULL, false, false),
    ('traverse_symmetric_link_types', '["related_to"]', 'json', 'edge',
     'Симметричные типы связей',
     'Типы рёбер, по которым PPR ходит в обе стороны (related_to не имеет стрелки). Направленные (depends_on, contradicts, supersedes...) не симметрируются.',
     '["related_to"]', NULL, NULL, NULL, false, false),
    ('traverse_max_nodes', '500', 'int', 'edge',
     'Кап обхода графа',
     'Жёсткий предел узлов одного обхода графа — защита от тяжёлых запросов (Фаза 1.5).',
     '500', '50', '5000', NULL, false, false),

    -- ── §2.7 Облачко знаний — cloud (2) ──
    ('context_cache_ttl', '3600', 'int', 'cloud',
     'TTL облачка (сек)',
     'Время жизни Redis-кеша снапшота «облачка знаний» проекта и dirty-флага. Держать ≥ периода beat-пересборки rebuild_contexts.',
     '3600', '60', '86400', NULL, false, false),
    ('cloud_recency_half_life_days', '30', 'int', 'cloud',
     'Полураспад свежести облачка',
     'За сколько дней гранула теряет половину веса при отборе кандидатов в облачко — свежие решения всплывают над древними.',
     '30', '7', '365', NULL, false, false),

    -- ── §2.8 Карта — map (13) ──
    ('map_layout_bbox', '1000', 'int', 'map',
     'Полусторона куба карты',
     'Координаты узлов нормируются в куб [−bbox, +bbox]³. Задаёт масштаб 3D-карты.',
     '1000', '100', '10000', NULL, false, false),
    ('map_min_dist', '50.0', 'float', 'map',
     'Мин. дистанция узлов',
     'Сила расталкивания пар узлов при релаксации (в единицах bbox) — узлы не слипаются.',
     '50.0', '1', '500', NULL, false, false),
    ('map_relax_iterations', '8', 'int', 'map',
     'Итераций релаксации',
     'Итерации раскладки с ранним выходом при стабилизации. Больше — ровнее карта, дольше сборка.',
     '8', '1', '100', NULL, false, false),
    ('map_drl_timeout', '120.0', 'float', 'map',
     'Таймаут DrL (сек)',
     'Лимит субпроцесса алгоритма DrL — сегфолт-щит (фикс F1): не уложился — откат на сферу.',
     '120.0', '10', '600', NULL, false, false),
    ('map_meta_ttl', '60', 'int', 'map',
     'TTL меты карты (сек)',
     'Время жизни Redis-кеша меты карты — цель «<50 мс на запрос».',
     '60', '5', '3600', NULL, false, false),
    ('map_snapshot_ttl', '86400', 'int', 'map',
     'TTL снапшота карты (сек)',
     'Время жизни gzip-снапшота текущей версии карты в Redis (сутки).',
     '86400', '600', '604800', NULL, false, false),
    ('map_stale_ttl', '300', 'int', 'map',
     'TTL устаревших снапшотов (сек)',
     'Сколько секунд держится устаревшая версия снапшота после выхода новой.',
     '300', '30', '86400', NULL, false, false),
    ('map_build_wait_seconds', '60.0', 'float', 'map',
     'Ожидание сборки (сек)',
     'Сколько запрос ждёт конкурента под build-lock, прежде чем отдать предыдущий снапшот.',
     '60.0', '5', '600', NULL, false, false),
    ('map_preview_chars', '180', 'int', 'map',
     'Длина превью узла',
     'Сколько символов контента гранулы попадает в preview узла карты (обрезка по границе слова + «…»).',
     '180', '40', '1000', NULL, false, false),
    ('map_name_chars', '80', 'int', 'map',
     'Длина имени узла',
     'Обрезка entity_name для тултипа узла.',
     '80', '20', '300', NULL, false, false),
    ('galactic_max_nodes', '20000', 'int', 'map',
     'Лимит узлов Galactic',
     'Защитный порог масштаба раскладки (прод-OOM 20.09: пик >3 ГБ при лимите 512M): выше — раскладка пропускается с WARNING, карта остаётся на сфере.',
     '20000', '1000', '200000', NULL, false, false),
    ('galactic_max_edges', '150000', 'int', 'map',
     'Лимит рёбер Galactic',
     'Аналогично узлам: предел числа рёбер для боевой раскладки.',
     '150000', '1000', '2000000', NULL, false, false),
    ('galactic_max_clusters', '3000', 'int', 'map',
     'Лимит кластеров Galactic',
     'Предел числа кластеров в раскладке.',
     '3000', '100', '50000', NULL, false, false),

    -- ── §2.9 Планировщик: воркер — celery (9; concurrency — налету через
    -- pool_grow/pool_shrink broadcast, остальные — requires_restart) ──
    ('celery_worker_concurrency', '4', 'int', 'celery',
     'Процессов воркера',
     'Сколько задач воркер исполняет параллельно. Больше — выше пропускная способность, больше память (лимит контейнера 512M!).',
     '4', '1', '8', NULL, false, false),
    ('celery_worker_prefetch_multiplier', '1', 'int', 'celery',
     'Предвыборка задач',
     'Сколько задач воркер берёт себе впрок. 1 = честное распределение между воркерами (fairness).',
     '1', '1', '10', NULL, false, true),
    ('celery_worker_max_tasks_per_child', '1000', 'int', 'celery',
     'Задач до перезапуска чилда',
     'Воркер-процесс перезапускается после стольких задач — защита от утечек памяти.',
     '1000', '100', '100000', NULL, false, true),
    ('celery_worker_max_memory_per_child', '200000', 'int', 'celery',
     'Память чилда до перезапуска (КБ)',
     'Перезапуск воркер-процесса при достижении этого RSS (200 МБ) — вторая ступень OOM-защиты.',
     '200000', '50000', '500000', NULL, false, true),
    ('task_soft_time_limit', '240', 'int', 'celery',
     'Soft-лимит задачи (сек)',
     'Секунды до SoftTimeLimitExceeded — задача получает шанс корректно завершиться.',
     '240', '30', '3600', NULL, false, true),
    ('task_time_limit', '300', 'int', 'celery',
     'Hard-лимит задачи (сек)',
     'Жёсткое убийство задачи по таймауту. Должен быть > soft-лимита.',
     '300', '60', '7200', NULL, false, true),
    ('task_default_retry_delay', '30', 'int', 'celery',
     'Задержка ретрая (сек)',
     'Базовая пауза перед повтором упавшей задачи (задачи могут переопределять).',
     '30', '1', '600', NULL, false, true),
    ('task_max_retries', '5', 'int', 'celery',
     'Максимум ретраев',
     'Сколько раз упавшая задача повторяется по умолчанию.',
     '5', '0', '20', NULL, false, true),
    ('result_expires', '3600', 'int', 'celery',
     'Жизнь результатов (сек)',
     'Сколько секунд в Redis хранятся результаты задач, потом чистятся автоматически.',
     '3600', '300', '86400', NULL, false, true),

    -- ── §2.10 Планировщик: beat-расписания — schedule (13, все requires_restart) ──
    -- Схема значения (реестр §2.10): {"type": "interval", "seconds": N} |
    -- {"type": "crontab", "minute": ..., "hour": ..., "day_of_week": ...}
    ('schedule.update_worker_stats', '{"type": "interval", "seconds": 30}', 'json', 'schedule',
     'Статистика воркера',
     'Как часто собирается статистика процессов воркера (метрики Мая).',
     '{"type": "interval", "seconds": 30}', NULL, NULL, NULL, false, true),
    ('schedule.update_business_metrics', '{"type": "interval", "seconds": 3600}', 'json', 'schedule',
     'Бизнес-метрики',
     'Период пересчёта агрегированных бизнес-метрик.',
     '{"type": "interval", "seconds": 3600}', NULL, NULL, NULL, false, true),
    ('schedule.rebuild_contexts', '{"type": "interval", "seconds": 3600}', 'json', 'schedule',
     'Пересборка облачков',
     'Как часто переписываются грязные снапшоты «облачка знаний». Держать ≤ context_cache_ttl.',
     '{"type": "interval", "seconds": 3600}', NULL, NULL, NULL, false, true),
    ('schedule.refresh_clusters', '{"type": "crontab", "minute": "0", "hour": "2", "day_of_week": null}', 'json', 'schedule',
     'Пересчёт кластеров',
     'Ежедневная разметка кластеров (после неё идут decay и отсечения).',
     '{"type": "crontab", "minute": "0", "hour": "2", "day_of_week": null}', NULL, NULL, NULL, false, true),
    ('schedule.layout_map', '{"type": "crontab", "minute": "30", "hour": "2", "day_of_week": null}', 'json', 'schedule',
     'Раскладка карты',
     'Ежедневный инкремент galactic_layout новых гранул (сразу после кластеров).',
     '{"type": "crontab", "minute": "30", "hour": "2", "day_of_week": null}', NULL, NULL, NULL, false, true),
    ('schedule.confidence_decay', '{"type": "crontab", "minute": "0", "hour": "3", "day_of_week": null}', 'json', 'schedule',
     'Затухание уверенности',
     'Ежедневное физическое затухание confidence по неймспейсам.',
     '{"type": "crontab", "minute": "0", "hour": "3", "day_of_week": null}', NULL, NULL, NULL, false, true),
    ('schedule.edge_prune', '{"type": "crontab", "minute": "30", "hour": "3", "day_of_week": null}', 'json', 'schedule',
     'Отсечение рёбер',
     'Ежедневная кампания жизни рёбер (после decay, до mark-stale).',
     '{"type": "crontab", "minute": "30", "hour": "3", "day_of_week": null}', NULL, NULL, NULL, false, true),
    ('schedule.mark_stale', '{"type": "crontab", "minute": "0", "hour": "4", "day_of_week": null}', 'json', 'schedule',
     'Пометка устаревших',
     'Ежедневная пометка заброшенных гранул по порогам stale_*.',
     '{"type": "crontab", "minute": "0", "hour": "4", "day_of_week": null}', NULL, NULL, NULL, false, true),
    ('schedule.gc_superseded', '{"type": "crontab", "minute": "0", "hour": "5", "day_of_week": "sun"}', 'json', 'schedule',
     'GC версий',
     'Еженедельная сборка мусора superseded-версий (воскресенье, низкая нагрузка).',
     '{"type": "crontab", "minute": "0", "hour": "5", "day_of_week": "sun"}', NULL, NULL, NULL, false, true),
    ('schedule.orphans_cleanup', '{"type": "crontab", "minute": "30", "hour": "5", "day_of_week": "sun"}', 'json', 'schedule',
     'Чистка сирот',
     'Еженедельная чистка сиротских сущностей.',
     '{"type": "crontab", "minute": "30", "hour": "5", "day_of_week": "sun"}', NULL, NULL, NULL, false, true),
    ('schedule.linker_name_reconciler', '{"type": "interval", "seconds": 3600}', 'json', 'schedule',
     'Резолв имён',
     'Как часто кампания подшивает «висячие» ссылки к реальным гранулам.',
     '{"type": "interval", "seconds": 3600}', NULL, NULL, NULL, false, true),
    ('schedule.linker_co_occurrence', '{"type": "interval", "seconds": 3600}', 'json', 'schedule',
     'Co-occurrence-слой',
     'Как часто пересчитывается слой L1c (не чаще раза в час, ADR-019 C L3).',
     '{"type": "interval", "seconds": 3600}', NULL, NULL, NULL, false, true),
    ('schedule.linker_l2_verdicts', '{"type": "interval", "seconds": 300}', 'json', 'schedule',
     'L2-вердикты',
     'Как часто воркер разбирает очередь серой зоны (очередь маленькая — можно часто).',
     '{"type": "interval", "seconds": 300}', NULL, NULL, NULL, false, true),

    -- ── §2.11 Лимиты API — api_caps (2) ──
    ('max_search_limit', '100', 'int', 'api_caps',
     'Кап размера выдачи',
     'Жёсткий потолок `limit` поискового запроса через REST — защита REST-слоя от тяжёлых выборок.',
     '100', '10', '500', NULL, false, false),
    ('max_graph_depth', '10', 'int', 'api_caps',
     'Кап глубины графа',
     'Максимальная глубина обхода графа через REST.',
     '10', '1', '20', NULL, false, false)
ON CONFLICT (key) DO NOTHING;

-- ════════════════════════════════════════════════════════════
-- 5. Builtin-профиль «Заводские настройки»
-- ════════════════════════════════════════════════════════════
-- values собирается из самой app_settings (jsonb_object_agg по
-- default_value) — единственный источник канонических дефолтов,
-- рассинхрон «профиль ≠ дефолты» невозможен по построению.
-- ON CONFLICT (name) DO NOTHING: повторный прогон не перетирает
-- снапшот (и ручные профили). applied_at = NULL — не применялся.

INSERT INTO app_settings_profiles (name, description, is_builtin, values)
SELECT
    'default',
    'Заводские настройки — канонические дефолты config.py (все 97 runtime-ключей реестра SETTINGS_REGISTRY).',
    true,
    COALESCE(jsonb_object_agg(key, default_value), '{}'::jsonb)
FROM app_settings
ON CONFLICT (name) DO NOTHING;

-- ════════════════════════════════════════════════════════════
-- 6. Владелец объектов — svc_athene_ai
-- ════════════════════════════════════════════════════════════
-- На проде run.py работает от svc_athene_ai — объекты уже его;
-- блок перестраховывает стенды, гоняющие миграции от postgres
-- (безусловный ALTER OWNER из 026 падает на стенде без роли,
-- поэтому здесь условие по pg_roles). Sequence identity-колонки
-- принадлежат таблице и переходят вместе с ней.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_athene_ai') THEN
        ALTER TABLE app_settings OWNER TO svc_athene_ai;
        ALTER TABLE app_settings_profiles OWNER TO svc_athene_ai;
        ALTER FUNCTION app_settings_notify() OWNER TO svc_athene_ai;
    END IF;
END;
$$;

-- DOWN: откат миграции (ручной; см. примечание)
-- ════════════════════════════════════════════════════════════
-- Примечание: секция закомментирована по образцу 022/023/026 —
-- run.py --down исполняет текст после маркера «-- DOWN», авто-отката
-- нет и не должно быть (деплой push→main). Для ручного отката на
-- реплике дампа — раскомментировать:
--
-- DROP TRIGGER IF EXISTS trg_app_settings_notify ON app_settings;
-- DROP FUNCTION IF EXISTS app_settings_notify();
-- DROP TABLE IF EXISTS app_settings_profiles;
-- DROP TABLE IF EXISTS app_settings;
--
-- Потери при откате: app_settings.value — ручные runtime-правки,
-- сделанные после сидинга (не восстанавливаются; default_value и так
-- в config.py); app_settings_profiles — созданные пользователем
-- профили (builtin default пересоздастся ре-сидингом). Обе таблицы
-- не имеют FK-потребителей — обратной цепочки нет.
