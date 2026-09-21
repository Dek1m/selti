-- ============================================================
-- 024_map_layout.sql — Полная карта 3D: серверная раскладка узлов
-- ============================================================
-- Дата: 2026-09-21
-- Исполнитель: Сона (по docs/PLAN_FULL_MAP_3D.md, фаза M2, §2.2)
--
-- Содержимое:
--   1) Таблица map_layout(node_id PK → memories ON DELETE CASCADE,
--      x/y/z REAL, rev INTEGER, updated_at) — координаты таски
--      layout_map (igraph DrL dim=3 + min-distance-релаксация +
--      нормировка в куб [-map_layout_bbox, map_layout_bbox]³);
--   2) Индекс rev — быстрый MAX(rev) (глобальный номер прогона) для
--      version-hash снапшота /api/map/full;
--   3) OWNER svc_athene_ai (инцидент 021: run.py применяет миграции
--      от этой роли).
--
-- Семантика rev: каждый прогон layout_map пишет rev = MAX(rev)+1 во все
-- строки прогона. Версия снапшота включает layout_rev — новая раскладка
-- инвалидирует ETag клиентов без изменения данных графа.
--
-- Строки гранул, выпавших из актуального множества (superseded/retracted),
-- остаются в таблице как история: /api/map/full их не джойнит (LEFT JOIN
-- только актуальных), физическую чистку делает GC-цепочка memories
-- (ON DELETE CASCADE).
-- ============================================================

CREATE TABLE IF NOT EXISTS map_layout (
    node_id    UUID PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    z          REAL NOT NULL,
    rev        INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_map_layout_rev CHECK (rev >= 1)
);

COMMENT ON TABLE map_layout IS 'Серверная 3D-раскладка узлов Полной карты (PLAN_FULL_MAP_3D M2). Пишет beat-таска layout_map (igraph DrL dim=3, weights=|weight|, seeding старых координат); читает снапшот /api/map/full.';
COMMENT ON COLUMN map_layout.node_id IS 'FK → memories.id. Раскладываются только актуальные гранулы; строки переживают супрессию (история), удаляются каскадом с гранулой.';
COMMENT ON COLUMN map_layout.x IS 'Координата X, целочисленный масштаб куба [-map_layout_bbox, map_layout_bbox] (config, дефолт ±1000).';
COMMENT ON COLUMN map_layout.y IS 'Координата Y, см. x.';
COMMENT ON COLUMN map_layout.z IS 'Координата Z, см. x.';
COMMENT ON COLUMN map_layout.rev IS 'Номер прогона layout_map: MAX(rev)+1 на каждом прогоне, одинаковый во всех строках прогона. Входит в version-hash снапшота — смена раскладки инвалидирует ETag.';
COMMENT ON COLUMN map_layout.updated_at IS 'Момент записи строки (прогон layout_map). MAX — layout_at в /api/map/meta.';

-- MAX(rev) на каждом вычислении version-hash снапшота и меты: индекс
-- вместо seq scan, таблица растёт вместе с корпусом.
CREATE INDEX IF NOT EXISTS idx_map_layout_rev ON map_layout (rev);

COMMENT ON INDEX idx_map_layout_rev IS 'Глобальный номер последнего прогона раскладки: MAX(rev) в version-hash /api/map/full.';

ALTER TABLE map_layout OWNER TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- DOWN: откат миграции
-- ════════════════════════════════════════════════════════════
-- DROP TABLE IF EXISTS map_layout;
