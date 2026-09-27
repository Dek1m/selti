-- ============================================================
-- 029_map_night_full_layout.sql — ночной полный DrL (Мастер 27.09)
-- ============================================================
-- Дата: 2026-09-27
-- Исполнитель: Сона (programmer)
-- Задача: полный точный DrL на ВСЕХ узлах — раз в сутки, без
-- огрублений/сэмплирования (именно наглядность полной карты уже
-- отловила реальный баг).
--
-- Что добавляется: ключ schedule.layout_map_full — beat-запись
-- layout-map-full (layout_map с args=[True], force-прогон) на
-- crontab 03:15. Слот выбран в окне «после confidence_decay 03:00,
-- до edge_prune 03:30» и разнесён с часовым layout_map :10 —
-- rebuild-лок в MapService разводит исполнение (два DrL-потомка
-- в контейнере 1G = взаимный OOM).
--
-- Канон: дефолт реестра §2.10 и сид 027 уже несут строку (027 на
-- проде применён — правка его файла прод-БД не тронет, переносит
-- эту миграция). INSERT идемпотентен (ON CONFLICT DO NOTHING):
-- ручные значения через /api/settings неприкосновенны.
--
-- pg_notify от триггера trg_app_settings_notify долетит до
-- beat-пула — RuntimeScheduler подхватит запись в течение 30 с,
-- рестарт контейнера не нужен.
-- ============================================================

INSERT INTO app_settings
    (key, value, value_type, group_key, title_ru, description_ru,
     default_value, min_value, max_value, enum_values,
     is_dangerous, requires_restart)
VALUES
    ('schedule.layout_map_full', '{"type": "crontab", "minute": "15", "hour": "3", "day_of_week": null}', 'json', 'schedule',
     'Ночной полный DrL',
     'Раз в сутки безусловный полный точный DrL на всех узлах (force): слот 03:15 — после confidence_decay 03:00, до edge_prune 03:30, разнесён с часовым :10 (rebuild-лок разводит их без гонки за память).',
     '{"type": "crontab", "minute": "15", "hour": "3", "day_of_week": null}', NULL, NULL, NULL, false, true)
ON CONFLICT (key) DO NOTHING;

-- DOWN: ключ убирается, beat-запись исчезает после перечитывания
-- расписания RuntimeScheduler'ом (30 с). Полный DrL остаётся
-- доступен ручным force-запуском layout_map --args '[true]'.
--
-- DELETE FROM app_settings WHERE key = 'schedule.layout_map_full';
