-- 008_add_importance.sql
-- ============================================================
-- Добавляем колонку importance для оценки важности гранул.
-- Шкала 1-5: 1 — мелочь, 5 — критично.
-- Дефолт 3 — существующие гранулы получают среднюю важность.
-- ============================================================

ALTER TABLE memories ADD COLUMN importance int NOT NULL DEFAULT 3;

-- DOWN
-- ALTER TABLE memories DROP COLUMN importance;
