#!/bin/bash
# ============================================================
# 00-roles.sh — Разделение привилегий: DDL vs DML
# ============================================================
# Запускается один раз при инициализации БД (docker-entrypoint-initdb.d)
# Создаёт:
#   - svc_athene_ai  — роль для приложения (только DML)
# ============================================================

set -e

# Основной пользователь (POSTGRES_USER) уже создан entrypoint'ом
# Создаём прикладного пользователя
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Роль для приложения (только DML)
    CREATE ROLE svc_athene_ai WITH LOGIN
        PASSWORD '${APP_PASSWORD:-changeme}'
        NOBYPASSRLS
        CONNECTION LIMIT 30;

    -- Даём доступ к схеме public (по умолчанию владелец — POSTGRES_USER)
    GRANT USAGE ON SCHEMA public TO svc_athene_ai;
EOSQL

echo "[init] 00-roles.sh completed: svc_athene_ai created"
