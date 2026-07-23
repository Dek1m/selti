#!/bin/bash
# ============================================================
# 00-roles.sh — Разделение привилегий: DDL vs DML
# ============================================================
# Запускается один раз при инициализации БД (docker-entrypoint-initdb.d)
# Создаёт:
#   - athena_ddl  — роль для миграций (может менять схему)
#   - athena_app  — роль для приложения (только DML)
# ============================================================

set -e

# Основной пользователь (POSTGRES_USER) уже создан entrypoint'ом
# Создаём прикладного пользователя
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Роль для миграций (создаётся автоматически из POSTGRES_USER)
    -- Всё, что нужно — дать права на схему

    -- Роль для приложения (только DML)
    CREATE ROLE athena_app WITH LOGIN
        PASSWORD '${APP_PASSWORD:-athena_app_change_me}'
        NOBYPASSRLS
        CONNECTION LIMIT 30;

    -- Даём доступ к схеме public (по умолчанию владелец — POSTGRES_USER)
    GRANT USAGE ON SCHEMA public TO athena_app;
EOSQL

echo "[init] 00-roles.sh completed: athena_app created"
