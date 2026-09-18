-- ============================================================
-- 018_memories_canonical.sql — memories: каноническая гранула (Фаза 0.2)
-- ============================================================
-- Дата: 2026-09-17
-- Исполнитель: Нора (db-architect)
-- План: docs/PLAN_MEMORY_REDESIGN.md (§0.2, решения D2/D3)
--
-- Содержимое:
--   1) Новые колонки memories: project_id, status, valid_from/valid_to,
--      ingested_at, confidence, supersedes/superseded_by, frozen,
--      last_accessed_at, access_count
--   2) FK → projects + self-FK версионирования (supersedes/superseded_by)
--   3) Индексы: GIN metadata, entity_name, project_id, supersedes,
--      valid_to (open), composite (project_id, status)
--   4) Триггер инкремента version при изменении content
--   5) relations: расширение CHECK link_type (+ supersedes, supports,
--      member_of, part_of, describes_cluster) + unique (target_name, link_type)
--   6) БАТЧЕВЫЙ backfill (батчи по 500): slug → project_id, temporal-поля,
--      отчёт о непривязанных slug'ах в _migrate_report
--
-- ВАЖНО (transitive-период для кода):
--   * is_archived ЗДЕСЬ НЕ ДРОПАЕМ — коду нужен рабочий период.
--     Дроп — отдельная заготовка 018b_drop_is_archived.sql
--     («применить ПОСЛЕ перевода кода»).
--   * namespace TEXT ЗДЕСЬ НЕ ТРОГАЕМ.
--     Дроп — отдельная заготовка 018c_drop_namespace_text.sql.
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 1. Новые колонки
-- ════════════════════════════════════════════════════════════

-- Привязка к проекту (D2). NULL = глобальный слой (внепроектное знание).
ALTER TABLE memories ADD COLUMN IF NOT EXISTS project_id UUID;

-- Канонический жизненный статус гранулы (заменяет is_archived в целевой модели)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'asserted'
    CHECK (status IN ('asserted','superseded','retracted','uncertain'));

-- Bitemporal (D3): период валидности факта
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_to   TIMESTAMPTZ;  -- NULL = актуально

-- Когда гранула попала в систему (поглощение; для backfill = created_at)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Уверенность в факте (после вырезания belief-модели — единственная метрика, D7)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS confidence REAL NOT NULL DEFAULT 1.0
    CHECK (confidence >= 0 AND confidence <= 1);

-- Версионирование (D3): однонаправленная цепочка
ALTER TABLE memories ADD COLUMN IF NOT EXISTS supersedes    UUID;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS superseded_by UUID;

-- Ручной freeze для вечных фактов (не затухают, D4)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS frozen BOOLEAN NOT NULL DEFAULT false;

-- Ранжирование по доступу (Фаза 1.2): инкремент при выдаче
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS access_count    INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN memories.project_id IS 'FK → projects.id. NULL = глобальный слой (внепроектное знание).';
COMMENT ON COLUMN memories.status IS 'Жизненный статус: asserted (актуально) / superseded (есть наследник) / retracted (отозвано) / uncertain.';
COMMENT ON COLUMN memories.valid_from IS 'Начало периода валидности факта (bitemporal).';
COMMENT ON COLUMN memories.valid_to IS 'Конец периода валидности. NULL = факт актуален на текущий момент.';
COMMENT ON COLUMN memories.ingested_at IS 'Момент поглощения гранулы в систему (для backfill = created_at).';
COMMENT ON COLUMN memories.confidence IS 'Уверенность в факте, 0..1. Заменяет вырезанную belief-модель (D7).';
COMMENT ON COLUMN memories.supersedes IS 'FK → memories.id: какую гранулу эта версия замещает.';
COMMENT ON COLUMN memories.superseded_by IS 'FK → memories.id: какая гранула замещает эту (если status=superseded).';
COMMENT ON COLUMN memories.frozen IS 'Ручной freeze: вечный факт, не подлежит затуханию (D4).';

-- ════════════════════════════════════════════════════════════
-- 2. Foreign Keys (idempotent через pg_constraint)
-- ════════════════════════════════════════════════════════════
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memories_project'
          AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT fk_memories_project
            FOREIGN KEY (project_id) REFERENCES projects(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memories_supersedes'
          AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT fk_memories_supersedes
            FOREIGN KEY (supersedes) REFERENCES memories(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memories_superseded_by'
          AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT fk_memories_superseded_by
            FOREIGN KEY (superseded_by) REFERENCES memories(id)
            ON DELETE SET NULL;
    END IF;
END;
$$;

-- ════════════════════════════════════════════════════════════
-- 3. Индексы (под ключевые запросы Фаз 1–3)
-- ════════════════════════════════════════════════════════════

-- GIN jsonb_path_ops: быстрые фильтры по metadata->>'...'
CREATE INDEX IF NOT EXISTS idx_memories_metadata_gin
    ON memories USING gin (metadata jsonb_path_ops);

-- Expression-index: убивает seq scan поиска по entity_name
CREATE INDEX IF NOT EXISTS idx_memories_entity_name
    ON memories ((metadata->>'entity_name'))
    WHERE is_archived = false;

-- Привязка к проекту (контекст, фильтры project_id)
CREATE INDEX IF NOT EXISTS idx_memories_project_id
    ON memories (project_id)
    WHERE project_id IS NOT NULL;

-- Версионирование: "кто замещает эту гранулу"
CREATE INDEX IF NOT EXISTS idx_memories_supersedes
    ON memories (supersedes)
    WHERE supersedes IS NOT NULL;

-- Актуальные гранулы (valid_to IS NULL) — fast-path фильтра актуальности
CREATE INDEX IF NOT EXISTS idx_memories_valid_to_open
    ON memories (valid_to)
    WHERE valid_to IS NULL;

-- Композит для контекст-выборок по проекту + статусу
CREATE INDEX IF NOT EXISTS idx_memories_project_status
    ON memories (project_id, status)
    WHERE is_archived = false;

-- ════════════════════════════════════════════════════════════
-- 4. Триггер инкремента version при изменении content
-- ════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION bump_version_on_content_change()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.content IS DISTINCT FROM OLD.content THEN
        NEW.version = COALESCE(OLD.version, 0) + 1;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_memories_version_bump'
          AND tgrelid = 'memories'::regclass
    ) THEN
        CREATE TRIGGER trg_memories_version_bump
            BEFORE UPDATE ON memories
            FOR EACH ROW
            EXECUTE FUNCTION bump_version_on_content_change();
    END IF;
END;
$$;

-- ════════════════════════════════════════════════════════════
-- 5. relations: расширение CHECK link_type + unique индекс
-- ════════════════════════════════════════════════════════════
-- Полный список = 005_relations.sql + новые типы версионирования/иерархии.
-- Idempotent: DROP + ADD (повторный прогон корректен).
ALTER TABLE relations DROP CONSTRAINT IF EXISTS chk_link_type;

ALTER TABLE relations ADD CONSTRAINT chk_link_type CHECK (link_type IN (
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
    'informs', 'informed_by', 'connected_to',
    -- Версионирование / иерархия (новые, Фаза 0.2)
    'supersedes', 'supports', 'member_of', 'part_of', 'describes_cluster'
));

COMMENT ON CONSTRAINT chk_link_type ON relations IS 'Допустимые типы связей (005 + supersedes/supports/member_of/part_of/describes_cluster).';

-- Уникальность soft-resolve связи: (target_name, link_type) для ненайдённых целей
CREATE UNIQUE INDEX IF NOT EXISTS idx_relations_target_name_type
    ON relations (target_name, link_type)
    WHERE target_id IS NULL;

-- ════════════════════════════════════════════════════════════
-- 6. БАТЧЕВЫЙ backfill (батчи по 500, один DO-блок = одна транзакция)
-- ════════════════════════════════════════════════════════════
-- 6.1 Маркер «не обработано»: valid_from/ingested_at ≠ created_at.
--     Новые колонки только что добавлены с DEFAULT now(), поэтому
--     старые строки различаются от created_at. После обработки —
--     идентичны, повторный прогон = 0 изменений (идемпотентность).
-- ════════════════════════════════════════════════════════════

-- 6.2 Отчётная таблица непривязанных slug'ов
CREATE TABLE IF NOT EXISTS _migrate_report (
    slug          TEXT PRIMARY KEY,
    granule_count BIGINT NOT NULL DEFAULT 0,
    reported_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE _migrate_report IS 'Аудит-отчёт backfill метаданных (миграция 018): slug-и из metadata->>''project_id'', не привязанные к projects.';

DO $migration$
DECLARE
    v_batch   CONSTANT INT := 500;
    v_updated BIGINT;
BEGIN
    -- Висячий цикл: на каждой итерации обрабатываем до 500 «не тронутых» строк,
    -- пока ROW_COUNT не станет нулевым. Защита от бесконечного цикла —
    -- детерминированный маркер (valid_from/ingested_at приводятся к created_at).
    LOOP
        WITH batch AS (
            SELECT m.id, p.id AS project_id
            FROM memories m
            LEFT JOIN projects p ON p.slug = m.metadata->>'project_id'
            WHERE m.valid_from   IS DISTINCT FROM m.created_at
               OR m.ingested_at IS DISTINCT FROM m.created_at
            ORDER BY m.id
            LIMIT v_batch
        )
        UPDATE memories m
        SET
            project_id = b.project_id,
            status      = 'asserted',
            valid_from  = m.created_at,
            ingested_at = m.created_at,
            confidence  = 1.0
        FROM batch b
        WHERE m.id = b.id;

        GET DIAGNOSTICS v_updated = ROW_COUNT;
        EXIT WHEN v_updated = 0;
    END LOOP;
END;
$migration$;

-- 6.2b Перенос мягкого удаления: is_archived=true → status='retracted', valid_to=now()
--     Backfill выше выставил status='asserted' ВСЕМ строкам (включая ранее
--     удалённые). Без этого переноса после дропа is_archived (018b) мягко
--     удалённые записи «воскреснут» и попадут в поиск. Идемпотентно.
UPDATE memories
SET status = 'retracted', valid_to = now()
WHERE is_archived = true
  AND status = 'asserted';

-- 6.3 Отчёт о непривязанных slug'ах (всё, кроме known + sentinel 'unknown')
INSERT INTO _migrate_report (slug, granule_count)
SELECT m.metadata->>'project_id' AS slug, count(*) AS granule_count
FROM memories m
LEFT JOIN projects p ON p.slug = m.metadata->>'project_id'
WHERE m.project_id IS NULL
  AND m.metadata->>'project_id' IS NOT NULL
  AND m.metadata->>'project_id' <> 'unknown'
  AND p.id IS NULL
GROUP BY m.metadata->>'project_id'
ON CONFLICT (slug) DO UPDATE SET granule_count = EXCLUDED.granule_count;

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- Backfill — additive, откат не требуется (данные уйдут вместе с колонками).
-- DROP TABLE IF EXISTS _migrate_report;
--
-- DROP INDEX IF EXISTS idx_relations_target_name_type;
-- ALTER TABLE relations DROP CONSTRAINT IF EXISTS chk_link_type;
-- ALTER TABLE relations ADD CONSTRAINT chk_link_type CHECK (link_type IN (
--     'depends_on','used_by','extends','implements','contains','contained_by','calls','called_by',
--     'related_to','contradicts','solves','tested_by','implements_adr','references',
--     'follows','precedes','alternative_to','causes','prevents',
--     'runs_on','exposes','mounts',
--     'derived_from','motivates','informs','informed_by','connected_to'
-- ));
--
-- DROP TRIGGER IF EXISTS trg_memories_version_bump ON memories;
-- DROP FUNCTION IF EXISTS bump_version_on_content_change();
--
-- DROP INDEX IF EXISTS idx_memories_metadata_gin;
-- DROP INDEX IF EXISTS idx_memories_entity_name;
-- DROP INDEX IF EXISTS idx_memories_project_id;
-- DROP INDEX IF EXISTS idx_memories_supersedes;
-- DROP INDEX IF EXISTS idx_memories_valid_to_open;
-- DROP INDEX IF EXISTS idx_memories_project_status;
--
-- ALTER TABLE memories DROP CONSTRAINT IF EXISTS fk_memories_project;
-- ALTER TABLE memories DROP CONSTRAINT IF EXISTS fk_memories_supersedes;
-- ALTER TABLE memories DROP CONSTRAINT IF EXISTS fk_memories_superseded_by;
--
-- ALTER TABLE memories DROP COLUMN IF EXISTS project_id;
-- ALTER TABLE memories DROP COLUMN IF EXISTS status;
-- ALTER TABLE memories DROP COLUMN IF EXISTS valid_from;
-- ALTER TABLE memories DROP COLUMN IF EXISTS valid_to;
-- ALTER TABLE memories DROP COLUMN IF EXISTS ingested_at;
-- ALTER TABLE memories DROP COLUMN IF EXISTS confidence;
-- ALTER TABLE memories DROP COLUMN IF EXISTS supersedes;
-- ALTER TABLE memories DROP COLUMN IF EXISTS superseded_by;
-- ALTER TABLE memories DROP COLUMN IF EXISTS frozen;
-- ALTER TABLE memories DROP COLUMN IF EXISTS last_accessed_at;
-- ALTER TABLE memories DROP COLUMN IF EXISTS access_count;