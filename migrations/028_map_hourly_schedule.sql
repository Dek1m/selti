-- ============================================================
-- 028_map_hourly_schedule.sql — часовой цикл карты (приказ Мастера 27.09)
-- ============================================================
-- Дата: 2026-09-27
-- Исполнитель: Сона (programmer)
-- Задача: карта обновляется РАЗ В ЧАС, ПОСЛЕ прохода линкера.
--
-- Проблема: schedule.linker_co_occurrence тикал interval-ом, раскладка
-- schedule.layout_map — crontab 02:30 (инкремент galactic_layout). Свежие
-- рёбра L1c (например 16:40) до UI доезжали только следующей ночью.
--
-- Новый канон (дефолт реестра §2.10 уже обновлён вместе с сидом 027):
--   linker_co_occurrence → crontab :00 каждого часа (фиксированная минута
--                          вместо плавающего interval — детерминированный
--                          порядок относительно раскладки)
--   layout_map           → crontab :10 каждого часа (DrL-rebuild c
--                          dirty-гейтом: линкер ничего не создал — no-op;
--                          задача слота сменена galactic_layout →
--                          layout_map, см. celery_app.SCHEDULE_TASKS)
--
-- Перенос Прод-значений: 027 сидирован ON CONFLICT DO NOTHING и на проде
-- уже применён — изменение его файла БД не тронет. Этот апдейт-сидинг
-- переносит value только с канонического сида 027 (jsonb-containment):
-- ручные правки через /api/settings неприкосновенны. default_value/
-- title/description актуализируются безусловно (канон, «Reset to
-- default» и UI-подписи берут их оттуда).
--
-- Идемпотентность: повторный прогон — no-op по условию WHERE на value;
-- безусловные UPDATE пишут те же значения. pg_notify от триггера
-- trg_app_settings_notify долетит до beat-пула — RuntimeScheduler
-- перечитает расписание в течение 30 с, рестарт контейнера не нужен.
-- ============================================================

-- Канонические метаданные (совпадают бит-в-бит с обновлённым сидом 027)
UPDATE app_settings SET
    default_value = '{"type": "crontab", "minute": "10", "hour": "*", "day_of_week": null}'::jsonb,
    title_ru = 'Раскладка карты',
    description_ru = 'Часовой пересчёт layout_map (DrL, :10) после прохода линкера co_occurrence (:00) — карта свежая раз в час; без изменений (не dirty) — no-op + прогрев снапшота.',
    updated_at = now(),
    updated_by = 'seed:028'
WHERE key = 'schedule.layout_map';

UPDATE app_settings SET
    default_value = '{"type": "crontab", "minute": "0", "hour": "*", "day_of_week": null}'::jsonb,
    title_ru = 'Co-occurrence-слой',
    description_ru = 'Проход L1c в :00 каждого часа (фиксированная минута — за ним в :10 едет пересчёт карты; не чаще раза в час, ADR-019 C L3).',
    updated_at = now(),
    updated_by = 'seed:028'
WHERE key = 'schedule.linker_co_occurrence';

-- Перенос самих значений (только с канонического сида 027)
UPDATE app_settings SET
    value = '{"type": "crontab", "minute": "10", "hour": "*", "day_of_week": null}'::jsonb,
    updated_at = now(),
    updated_by = 'seed:028'
WHERE key = 'schedule.layout_map'
  AND value @> '{"type": "crontab", "minute": "30", "hour": "2"}'::jsonb;

UPDATE app_settings SET
    value = '{"type": "crontab", "minute": "0", "hour": "*", "day_of_week": null}'::jsonb,
    updated_at = now(),
    updated_by = 'seed:028'
WHERE key = 'schedule.linker_co_occurrence'
  AND value @> '{"type": "interval", "seconds": 3600}'::jsonb;

-- DOWN: откат к суточному ритму 027 (раскладка 02:30 = galactic-инкремент
-- до возврата задачи слота в celery_app; коммит, меняющий SCHEDULE_TASKS,
-- откатывается вместе с этим файлом)
--
-- UPDATE app_settings SET
--     value = '{"type": "crontab", "minute": "30", "hour": "2", "day_of_week": null}'::jsonb,
--     default_value = '{"type": "crontab", "minute": "30", "hour": "2", "day_of_week": null}'::jsonb,
--     title_ru = 'Раскладка карты',
--     description_ru = 'Ежедневный инкремент galactic_layout новых гранул (сразу после кластеров).',
--     updated_at = now(), updated_by = 'rollback:028'
-- WHERE key = 'schedule.layout_map';
-- UPDATE app_settings SET
--     value = '{"type": "interval", "seconds": 3600}'::jsonb,
--     default_value = '{"type": "interval", "seconds": 3600}'::jsonb,
--     title_ru = 'Co-occurrence-слой',
--     description_ru = 'Как часто пересчитывается слой L1c (не чаще раза в час, ADR-019 C L3).',
--     updated_at = now(), updated_by = 'rollback:028'
-- WHERE key = 'schedule.linker_co_occurrence';
