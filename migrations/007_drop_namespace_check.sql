-- 007_drop_namespace_check.sql
-- ============================================================
-- Убираем CHECK constraintchk_namespace — теперь namespace
-- управляется через таблицу namespaces (реестр)
-- ============================================================

ALTER TABLE memories DROP CONSTRAINT IF EXISTS chk_namespace;

-- Обновляем комментарий
COMMENT ON COLUMN memories.namespace IS 'Строковый ID namespace. Ссылка на namespaces.uid через namespace_id';
