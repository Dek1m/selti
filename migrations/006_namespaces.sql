-- 006_namespaces.sql
-- ============================================================
-- Реестр namespaces — убираем хардкод
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Таблица namespaces
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS namespaces (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid         TEXT NOT NULL UNIQUE,       -- строковый ID: 'code_knowledge', 'user_facts' и т.д.
    name        TEXT NOT NULL,              -- отображаемое имя
    description TEXT DEFAULT '',            -- промпт/описание: какие записи заносятся в этот namespace
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE namespaces IS 'Реестр namespace-ов. Каждый namespace — логическая изоляция данных. Описание содержит промпт какие записи туда заносятся.';
COMMENT ON COLUMN namespaces.uid IS 'Уникальный строковый идентификатор namespace (snake_case)';
COMMENT ON COLUMN namespaces.description IS 'Описание/промпт: какие записи заносятся в этот namespace';

-- Триггер автообновления updated_at
CREATE OR REPLACE FUNCTION update_namespaces_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_namespaces_updated_at'
          AND tgrelid = 'namespaces'::regclass
    ) THEN
        CREATE TRIGGER trg_namespaces_updated_at
            BEFORE UPDATE ON namespaces
            FOR EACH ROW
            EXECUTE FUNCTION update_namespaces_updated_at_column();
    END IF;
END;
$$;

-- ════════════════════════════════════════════════════════════
-- 2. Сидер: 5 дефолтных namespace + default
-- ════════════════════════════════════════════════════════════
INSERT INTO namespaces (uid, name, description) VALUES
    ('default',             'Default',              'Общие записи, не отнесённые к другому namespace'),
    ('user_facts',          'User Facts',           'Профили, характеры, предпочтения, привычки, факты о пользователе'),
    ('code_knowledge',      'Code Knowledge',       'Гранулы кода: модули, классы, функции, SQL-запросы, зависимости, архитектурные паттерны'),
    ('dialogue_insights',   'Dialogue Insights',    'Инсайты и договорённости из диалогов: неочевидные выводы, контекст, паттерны взаимодействия'),
    ('project_meta',        'Project Meta',         'Архитектурные решения (ADR), статус проекта, технические решения, риски, требования'),
    ('infrastructure',      'Infrastructure',       'Серверы, контейнеры, сети, API-эндпоинты, порты, ОС, тома — инфраструктурные факты')
ON CONFLICT (uid) DO NOTHING;

-- ════════════════════════════════════════════════════════════
-- 3. FK namespace_id в таблице memories
-- ════════════════════════════════════════════════════════════
ALTER TABLE memories ADD COLUMN IF NOT EXISTS namespace_id UUID;

-- Заполняем namespace_id из существующего текстового namespace
UPDATE memories m
SET namespace_id = n.id
FROM namespaces n
WHERE m.namespace = n.uid
  AND m.namespace_id IS NULL;

-- Делаем NOT NULL (после миграции всех данных)
ALTER TABLE memories ALTER COLUMN namespace_id SET NOT NULL;

-- FK на namespaces
ALTER TABLE memories
    ADD CONSTRAINT fk_memories_namespace
    FOREIGN KEY (namespace_id) REFERENCES namespaces(id)
    ON DELETE RESTRICT;

-- ════════════════════════════════════════════════════════════
-- 4. Индексы
-- ════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_memories_namespace_id ON memories (namespace_id);
CREATE INDEX IF NOT EXISTS idx_namespaces_uid ON namespaces (uid);

-- ════════════════════════════════════════════════════════════
-- 5. DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- ALTER TABLE memories DROP CONSTRAINT IF EXISTS fk_memories_namespace;
-- ALTER TABLE memories DROP COLUMN IF EXISTS namespace_id;
-- DROP INDEX IF EXISTS idx_namespaces_uid;
-- DROP INDEX IF EXISTS idx_memories_namespace_id;
-- DROP TRIGGER IF EXISTS trg_namespaces_updated_at ON namespaces;
-- DROP FUNCTION IF EXISTS update_namespaces_updated_at_column();
-- DROP TABLE IF EXISTS namespaces;
