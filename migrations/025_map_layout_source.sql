-- ============================================================
-- 025_map_layout_source.sql — Происхождение координат map_layout
-- ============================================================
-- Дата: 2026-09-22
-- Исполнитель: Сона (требование Мастера: ручные координаты гранулы
-- при создании, перманентные против пересевов раскладки)
--
-- Содержимое:
--   1) map_layout.source TEXT NOT NULL DEFAULT 'galactic'
--      + CHECK ('galactic' | 'manual'):
--        galactic — строки таски galactic_layout (спираль/балдж/гало);
--        manual — координаты Мастера из memory_store(position={x,y,z}):
--        подъём существующей звезды на новое место или посадка новой.
--        Обратная совместимость: DEFAULT — все строки 024 остаются
--        galactic, ни один существующий запрос не меняется.
--   2) CHECK rev ослаблен rev >= 1 → rev >= 0: manual-строки пишутся
--      с rev = 0 — ручная позиция НЕ поколение раскладки. Тогда
--      MAX(rev) (version-hash снапшота, ETag /api/map/full) не дёргается
--      от ручных переносов: клиенты не перекачивают карту из-за
--      подвинутой звезды. rev NOT NULL — INSERT обязан указать 0 явно.
--   3) Индекс по source НЕ нужен: единственный producer-запрос по нему —
--      force-пересев DELETE WHERE source <> 'manual' (сносит большинство
--      строк — seq scan неизбежен и корректен), selective-чтений по source
--      нет (снапшот читает таблицу целиком через LEFT JOIN).
--
-- Защита ручных координат (знание кода, не БД): galactic_layout force=True
-- сносит только source <> 'manual' (DELETE вместо TRUNCATE); инкремент
-- (force=False) manual-строки считает размещёнными и не пересеивает.
-- ============================================================

ALTER TABLE map_layout ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'galactic';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_map_layout_source'
          AND conrelid = 'map_layout'::regclass
    ) THEN
        ALTER TABLE map_layout ADD CONSTRAINT chk_map_layout_source
            CHECK (source IN ('galactic', 'manual'));
    END IF;
END;
$$;

-- rev >= 0 вместо rev >= 1: поколениями нумеруются только galactic-прогоны,
-- manual живёт вне нумерации (rev = 0), все строки 024 уже rev >= 1.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_map_layout_rev'
          AND conrelid = 'map_layout'::regclass
    ) THEN
        ALTER TABLE map_layout DROP CONSTRAINT chk_map_layout_rev;
    END IF;
    ALTER TABLE map_layout ADD CONSTRAINT chk_map_layout_rev CHECK (rev >= 0);
END;
$$;

COMMENT ON COLUMN map_layout.source IS 'Происхождение координат: galactic — таска galactic_layout (пересевы force сносят их DELETE source <> manual); manual — ручные координаты memory_store(position), перманентны — переживают любой пересев, инкремент их не пересеивает.';
COMMENT ON COLUMN map_layout.rev IS 'Номер прогона galactic_layout: MAX(rev)+1 на каждом прогоне, одинаковый во всех строках прогона. manual-строки пишутся с rev = 0 — вне поколений, MAX(rev) и version-hash снапшота от ручных переносов не меняются. Входит в version-hash снапшота — смена раскладки инвалидирует ETag.';

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- BEFORE: manual-строки несовместимы со старым CHECK — сначала
-- UPDATE map_layout SET x = 0, y = 0, z = 0 WHERE source = 'manual'
-- (или DELETE ... WHERE source = 'manual'), затем:
--   ALTER TABLE map_layout DROP CONSTRAINT IF EXISTS chk_map_layout_source;
--   ALTER TABLE map_layout DROP COLUMN IF EXISTS source;
--   ALTER TABLE map_layout DROP CONSTRAINT chk_map_layout_rev;
--   ALTER TABLE map_layout ADD CONSTRAINT chk_map_layout_rev CHECK (rev >= 1);
