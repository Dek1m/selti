-- ============================================================
-- 013_rename_db_and_grant_rights.sql
-- Переименование БД athene_memory → memory + выдача прав
-- ============================================================
-- ВНИМАНИЕ: Эта миграция выполняется ОТДЕЛЬНО от основного
-- процесса миграций. Она требует суперпользователя (postgres)
-- и выполняется outside транзакции.
--
-- Причины:
--   - ALTER DATABASE ... RENAME TO не поддерживается в транзакции
--   - GRANT ALL PRIVILEGES ON DATABASE аналогично
--
-- Запуск:
--   docker exec postgres psql -U postgres -d athene_memory -f /path/to/013_rename_db_and_grant_rights.sql
--
-- Или через psql напрямую:
--   psql -h localhost -U postgres -d athene_memory -f 013_rename_db_and_grant_rights.sql
--
-- ROLLBACK:
--   -- Переименовать обратно
--   ALTER DATABASE memory RENAME TO athene_memory;
--
--   -- Отозвать права (если нужно)
--   REVOKE ALL PRIVILEGES ON DATABASE memory FROM svc_athene_ai;
--   REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM svc_athene_ai;
--   REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM svc_athene_ai;
--   REVOKE CREATE ON SCHEMA public FROM svc_athene_ai;
--
--   -- Вернуть владельца таблиц (если нужно)
--   ALTER TABLE memories OWNER TO postgres;
--   ALTER TABLE relations OWNER TO postgres;
--   ALTER TABLE namespaces OWNER TO postgres;
--   ALTER TABLE resource_hashes OWNER TO postgres;
--   ALTER TABLE _migrations OWNER TO postgres;
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 0. Завершить активные подключения к БД
-- ════════════════════════════════════════════════════════════
-- PostgreSQL не позволит переименовать БД, пока есть
-- активные подключения (кроме текущего). Завершаем их.

SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'athene_memory'
  AND pid <> pg_backend_pid();

-- ════════════════════════════════════════════════════════════
-- 1. Переименование БД
-- ════════════════════════════════════════════════════════════
-- После этого все конфиги должны ссылаться на 'memory'
-- (docker-compose.yml уже настроен через ${POSTGRES_DB})

ALTER DATABASE athene_memory RENAME TO memory;

-- ════════════════════════════════════════════════════════════
-- 2. Выдача прав svc_athene_ai
-- ════════════════════════════════════════════════════════════
-- Пользователь svc_athene_ai уже существует (создан ранее),
-- но не имеет прав на таблицы. Выдаём полный доступ.

GRANT ALL PRIVILEGES ON DATABASE memory TO svc_athene_ai;

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO svc_athene_ai;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO svc_athene_ai;

-- Право на создание объектов в schema public
-- (нужно для будущих миграций и временных таблиц)
GRANT CREATE ON SCHEMA public TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- 3. Default privileges для будущих таблиц
-- ════════════════════════════════════════════════════════════
-- Без этого права новые таблицы (созданные через миграции)
-- снова не будут доступны svc_athene_ai.

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO svc_athene_ai;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON SEQUENCES TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- 4. Обновление владельца существующих таблиц
-- ════════════════════════════════════════════════════════════
-- Владелец таблицы = postgres (по умолчанию при создании БД).
-- Передаём владение svc_athene_ai для согласованности.

ALTER TABLE memories OWNER TO svc_athene_ai;
ALTER TABLE relations OWNER TO svc_athene_ai;
ALTER TABLE namespaces OWNER TO svc_athene_ai;
ALTER TABLE resource_hashes OWNER TO svc_athene_ai;
ALTER TABLE _migrations OWNER TO svc_athene_ai;

-- ════════════════════════════════════════════════════════════
-- 5. Проверка (выполнить вручную после миграции)
-- ════════════════════════════════════════════════════════════
-- Раскомментируйте нужные строки для верификации:

-- -- Проверка прав на чтение
-- SELECT has_table_privilege('svc_athene_ai', 'memories', 'SELECT');
-- SELECT has_table_privilege('svc_athene_ai', 'relations', 'SELECT');
-- SELECT has_table_privilege('svc_athene_ai', 'namespaces', 'SELECT');

-- -- Проверка прав на запись
-- SELECT has_table_privilege('svc_athene_ai', 'memories', 'INSERT');
-- SELECT has_table_privilege('svc_athene_ai', 'memories', 'UPDATE');
-- SELECT has_table_privilege('svc_athene_ai', 'memories', 'DELETE');

-- -- Проверка владельца таблиц
-- SELECT tablename, tableowner
-- FROM pg_tables
-- WHERE schemaname = 'public';

-- -- Проверка имени БД
-- SELECT current_database();
