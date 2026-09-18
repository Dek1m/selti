-- ============================================================
-- 017_projects_registry.sql — Реестр проектов (Фаза 0.1)
-- ============================================================
-- Дата: 2026-09-17
-- Исполнитель: Нора (db-architect)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§0.1, решение D1, D10)
--
-- Содержимое:
--   1) projects             — реестр проектов (UUID PK, slug UNIQUE)
--   2) technologies         — справочник технологий
--   3) project_technologies — связь проект ↔ технология (M:N)
--   4) project_links        — внешние ссылки проекта
--   5) Сиды: selti, akame, albedo, mia, belle, zcode-local, argenta-team
--
-- ВАЖНО (D10): pgvector выпилен миграцией 011 — векторных колонок
-- здесь НЕ создаём. Семантический поиск проектов — коллекция
-- `projects` в Qdrant (payload {slug, name, kind, status}), отдельно.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 0. Хелпер: автообновление updated_at
--    (уже создан в 001, здесь CREATE OR REPLACE для самодостаточности)
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ════════════════════════════════════════════════════════════
-- 1. Таблица projects
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS projects (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug           TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    description    TEXT,
    kind           TEXT NOT NULL
                   CHECK (kind IN ('code','infra','domain','workspace','org')),
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','archived','frozen')),
    local_path     TEXT,
    repo_url       TEXT,
    docs_url       TEXT,
    homepage_url   TEXT,
    default_branch TEXT NOT NULL DEFAULT 'main',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE projects IS 'Реестр проектов Argenta Team. kind = code/infra/domain/workspace/org; status = active/archived/frozen.';
COMMENT ON COLUMN projects.slug IS 'Строковый идентификатор проекта (snake_case). Источник привязки: memories.metadata->>''project_id''.';
COMMENT ON COLUMN projects.kind IS 'Тип проекта: code (репозиторий), infra, domain, workspace (окружение), org (организация).';
COMMENT ON COLUMN projects.status IS 'Жизненный цикл: active/archived/frozen (frozen — вечные факты без затухания).';

-- ════════════════════════════════════════════════════════════
-- 2. Индексы projects (под запросы Фазы 5: фильтры /api/projects)
-- ════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_projects_kind       ON projects (kind);
CREATE INDEX IF NOT EXISTS idx_projects_status     ON projects (status);
-- local_path — матч ZCode-хука (memory_context): ${ZCODE_PROJECT_DIR} → slug
CREATE INDEX IF NOT EXISTS idx_projects_local_path ON projects (local_path);

-- ════════════════════════════════════════════════════════════
-- 3. Триггер автообновления updated_at
-- ════════════════════════════════════════════════════════════
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_projects_updated_at'
          AND tgrelid = 'projects'::regclass
    ) THEN
        CREATE TRIGGER trg_projects_updated_at
            BEFORE UPDATE ON projects
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    END IF;
END;
$$;

-- ════════════════════════════════════════════════════════════
-- 4. Таблица technologies
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS technologies (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name     TEXT NOT NULL UNIQUE,
    category TEXT CHECK (category IN ('lang','framework','db','tool','service')),
    docs_url TEXT
);

COMMENT ON TABLE technologies IS 'Справочник технологий. category: lang/framework/db/tool/service.';

-- ════════════════════════════════════════════════════════════
-- 5. Таблица project_technologies (M:N)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS project_technologies (
    project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    technology_id UUID NOT NULL REFERENCES technologies(id) ON DELETE CASCADE,
    version       TEXT,
    purpose       TEXT,
    PRIMARY KEY (project_id, technology_id)
);

COMMENT ON TABLE project_technologies IS 'Связь проект ↔ технология. version — используемая версия, purpose — зачем используется (Фаза 6: секция «стек» снапшота).';

-- ════════════════════════════════════════════════════════════
-- 6. Таблица project_links
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS project_links (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    link_type  TEXT NOT NULL
               CHECK (link_type IN ('repo','ci','docs','board','monitoring','adr','other')),
    url        TEXT NOT NULL,
    title      TEXT
);

COMMENT ON TABLE project_links IS 'Внешние ссылки проекта: repo, ci, docs, board, monitoring, adr, other.';

-- ════════════════════════════════════════════════════════════
-- 7. Сиды (idempotent — ON CONFLICT DO NOTHING)
-- ════════════════════════════════════════════════════════════
INSERT INTO projects (slug, name, kind, local_path, repo_url) VALUES
    ('selti',        'selti',        'code',      'E:\Projects\Python\selti',  'https://github.com/Dek1m/selti'),
    ('akame',        'akame',        'code',      'E:\Projects\Python\akame',  NULL),
    ('albedo',       'albedo',       'code',      'E:\Projects\Python\albedo', NULL),
    ('mia',          'mia',          'code',      'E:\Projects\Python\mia',    'https://github.com/Dek1m/mia'),
    ('belle',        'belle',        'code',      'E:\Projects\Python\belle',  NULL),
    ('zcode-local',  'zcode-local',  'workspace', NULL,                        NULL),
    ('argenta-team', 'argenta-team', 'org',       NULL,                        NULL)
ON CONFLICT (slug) DO NOTHING;

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
-- DROP TABLE IF EXISTS project_links;
-- DROP TABLE IF EXISTS project_technologies;
-- DROP TABLE IF EXISTS technologies;
-- DROP INDEX IF EXISTS idx_projects_kind;
-- DROP INDEX IF EXISTS idx_projects_status;
-- DROP INDEX IF EXISTS idx_projects_local_path;
-- DROP TABLE IF EXISTS projects;