-- ============================================================================
-- 004_infrastructure.sql
-- Добавление namespace infrastructure в CHECK-констрейнт
-- ============================================================================

-- UP --------------------------------------------------------------------------

-- Обновляем CHECK-констрейнт на таблице memories
ALTER TABLE memories DROP CONSTRAINT IF EXISTS chk_namespace;
ALTER TABLE memories ADD CONSTRAINT chk_namespace
    CHECK (namespace IN (
        'default',
        'user_facts',
        'code_knowledge',
        'dialogue_insights',
        'project_meta',
        'infrastructure'
    ));

-- DOWN ------------------------------------------------------------------------
-- ALTER TABLE memories DROP CONSTRAINT IF EXISTS chk_namespace;
-- ALTER TABLE memories ADD CONSTRAINT chk_namespace
--     CHECK (namespace IN (
--         'default',
--         'user_facts',
--         'code_knowledge',
--         'dialogue_insights',
--         'project_meta'
--     ));
