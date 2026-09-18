-- ============================================================
-- 019_project_contexts.sql — project_contexts (Фаза 0.3)
-- ============================================================
-- Дата: 2026-09-17
-- Исполнитель: Нора (db-architect)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§0.3, решение D9 — «облачко знаний»)
--
-- Содержимое:
--   1) project_contexts — материализованные снапшоты контекста проекта
--      (предпосылка тула memory_context, Фаза 6)
--   2) project_context_snapshot(p_project_id) — выборка топ-гранул проекта
--      по паттерну list_with_count (014): row_number() + квоты per namespace
--
-- Квоты per namespace (project_meta ×10, code_knowledge ×15,
-- dialogue_insights ×5, infrastructure ×5, прочие ×5). p_limit_per_ns —
-- верхняя граница (LEAST), по умолчанию 15 — не расширяет именованные квоты.
--
-- ВАЖНО: функция ссылается на is_archived (существует после 018).
--   При дропе is_archived (018b) — пересоздать с предикатом status-семантики.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Таблица project_contexts
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS project_contexts (
    project_id    UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    content       TEXT,
    sections      JSONB DEFAULT '{}'::jsonb,
    granule_count INT,
    computed_at   TIMESTAMPTZ,
    metadata      JSONB DEFAULT '{}'::jsonb
);

COMMENT ON TABLE project_contexts IS 'Материализованные снапшоты контекста проекта. sections: {stack, adr, code_knowledge, insights, infra, prose}.';
COMMENT ON COLUMN project_contexts.project_id IS 'FK → projects.id. Один снапшот на проект (upsert).';
COMMENT ON COLUMN project_contexts.sections IS 'Секции снапшота (стек, ADR/решения, топ-гранулы кода, диалоги, инфра, проза от Тиши).';
COMMENT ON COLUMN project_contexts.computed_at IS 'Момент расчёта снапшота (для инвалидации по TTL/периоду).';

-- ════════════════════════════════════════════════════════════
-- 2. Хранимка: project_context_snapshot
-- ════════════════════════════════════════════════════════════
-- Выбирает топ-гранулы проекта (project_id) с квотами на namespace.
-- Сортировка внутри namespace: importance DESC, updated_at DESC.
--
-- Row_number() OVER (PARTITION BY namespace_id) + CASE-квота в WHERE
-- даёт «TOP-N на группу» одним проходом (по паттерну list_with_count).
-- ============================================================

DROP FUNCTION IF EXISTS project_context_snapshot(UUID, INT);

CREATE OR REPLACE FUNCTION project_context_snapshot(
    p_project_id   UUID,
    p_limit_per_ns INT DEFAULT 15
)
RETURNS TABLE(
    content    TEXT,
    namespace  TEXT,
    importance INT,
    updated_at TIMESTAMPTZ
)
LANGUAGE sql
STABLE
AS $$
    WITH ranked AS (
        SELECT
            m.content,
            n.uid AS namespace,
            m.importance,
            m.updated_at,
            row_number() OVER (
                PARTITION BY m.namespace_id
                ORDER BY m.importance DESC, m.updated_at DESC
            ) AS rn
        FROM memories m
        JOIN namespaces n ON n.id = m.namespace_id
        WHERE m.project_id = p_project_id
          AND m.status = 'asserted'
          AND m.is_archived = false
    )
    SELECT content, namespace, importance, updated_at
    FROM ranked
    WHERE rn <= LEAST(
        p_limit_per_ns,
        CASE namespace
            WHEN 'project_meta'      THEN 10
            WHEN 'code_knowledge'    THEN 15
            WHEN 'dialogue_insights' THEN 5
            WHEN 'infrastructure'    THEN 5
            ELSE 5
        END
    )
    ORDER BY importance DESC, updated_at DESC;
$$;

COMMENT ON FUNCTION project_context_snapshot(UUID, INT) IS 'Топ-гранулы проекта (квоты per namespace) для снапшота облачка знаний. Возвращает (content, namespace, importance, updated_at).';

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- DROP FUNCTION IF EXISTS project_context_snapshot(UUID, INT);
-- DROP TABLE IF EXISTS project_contexts;