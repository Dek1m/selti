-- 005_relations.sql
-- ============================================================
-- Таблица relations для графа знаний
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Таблица relations
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS relations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_id       UUID REFERENCES memories(id) ON DELETE SET NULL,
    target_name     TEXT,  -- entity_name для soft-resolve (target может быть в другом namespace)
    link_type       TEXT NOT NULL,
    description     TEXT,
    weight          FLOAT NOT NULL DEFAULT 1.0,
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_link_type CHECK (link_type IN (
        -- Кодовые
        'depends_on', 'used_by',
        'extends', 'implements',
        'contains', 'contained_by',
        'calls', 'called_by',
        -- Общие
        'related_to', 'contradicts', 'solves', 'tested_by',
        'implements_adr', 'references',
        'follows', 'precedes',
        'alternative_to', 'causes', 'prevents',
        -- Инфраструктурные
        'runs_on', 'exposes', 'mounts',
        -- Cross-namespace
        'derived_from', 'motivates',
        'informs', 'informed_by', 'connected_to'
    ))
);

COMMENT ON TABLE relations IS 'Граф связей между гранулами памяти. Каждая связь — направленное ребро от source_id к target_id.';
COMMENT ON COLUMN relations.source_id IS 'Исходящая гранула (откуда идёт связь)';
COMMENT ON COLUMN relations.target_id IS 'Целевая гранула (куда идёт связь). NULL если target не найден.';
COMMENT ON COLUMN relations.target_name IS 'entity_name целевой гранулы для soft-resolve через metadata';
COMMENT ON COLUMN relations.link_type IS 'Тип связи: depends_on, used_by, related_to, solves и т.д.';
COMMENT ON COLUMN relations.weight IS 'Вес связи (1.0 по умолчанию). Используется для взвешенного обхода.';

-- ════════════════════════════════════════════════════════════
-- 2. Индексы
-- ════════════════════════════════════════════════════════════

-- Исходящие связи: "какие гранулы зависят от этой?"
CREATE INDEX IF NOT EXISTS idx_relations_source
    ON relations (source_id);

-- Входящие связи: "от какие гранулы ссылаются на эту?"
CREATE INDEX IF NOT EXISTS idx_relations_target
    ON relations (target_id)
    WHERE target_id IS NOT NULL;

-- Поиск по entity_name (для soft-resolve)
CREATE INDEX IF NOT EXISTS idx_relations_target_name
    ON relations (target_name)
    WHERE target_name IS NOT NULL;

-- Фильтрация по типу связи
CREATE INDEX IF NOT EXISTS idx_relations_type
    ON relations (link_type);

-- Уникальность: одна связь одного типа между двумя гранулами
CREATE UNIQUE INDEX IF NOT EXISTS idx_relations_unique_link
    ON relations (source_id, target_id, link_type)
    WHERE target_id IS NOT NULL;

-- ════════════════════════════════════════════════════════════
-- 3. DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- DROP INDEX IF EXISTS idx_relations_unique_link;
-- DROP INDEX IF EXISTS idx_relations_type;
-- DROP INDEX IF EXISTS idx_relations_target_name;
-- DROP INDEX IF EXISTS idx_relations_target;
-- DROP INDEX IF EXISTS idx_relations_source;
-- DROP TABLE IF EXISTS relations;
