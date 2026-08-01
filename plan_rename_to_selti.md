# План: Переименование athena-memory → selti

## Решения

| Компонент | Было | Стало |
|-----------|------|-------|
| Сервис (docker-compose) | selti | selti |
| Контейнер сервиса | athena-memory | ${SERVICE_NAME} |
| Воркер (сервис) | celery-worker | celery-worker |
| Контейнер воркера | athena-celery-worker | ${SERVICE_NAME}-worker |
| Flower (сервис) | flower | flower |
| Контейнер Flower | athena-flower | ${SERVICE_NAME}-flower |
| Redis (сервис) | redis | redis |
| Контейнер Redis | athena-redis | redis |
| PostgreSQL (сервис) | postgres | postgres |
| Контейнер PostgreSQL | athena-postgres | postgres |
| Qdrant (сервис) | qdrant | qdrant |
| Контейнер Qdrant | athena-qdrant | qdrant |
| Сеть | athena-net | app-net |
| БД | athene_memory | ${POSTGRES_DB:-memory} |
| Пользователь PG (суперпользователь) | athena | postgres |
| Пароль PG (суперпользователь) | athena | postgres |
| Приложение PG | athena_app | svc_athene_ai |
| Пароль приложения | athena_app_change_me | ${SELTI_DB_PASSWORD} |
| MCP сервер (config.py) | athena-memory | ${SERVICE_NAME} |
| MCP URL (opencode.json) | http://athena-memory:8000/mcp/ | http://${SERVICE_NAME}:8000/mcp/ |

### Env-переменные (из .env)

| Переменная | Значение | Описание |
|------------|----------|----------|
| `SERVICE_NAME` | `selti` | Имя сервиса, используется в container_name |
| `POSTGRES_DB` | `memory` | Имя БД |
| `POSTGRES_USER` | `postgres` | Суперпользователь PG |
| `POSTGRES_PASSWORD` | `postgres` | Пароль суперпользователя |
| `SELTI_DB_PASSWORD` | *(секрет)* | Пароль svc_athene_ai |

## Зависимости

```
Шаг 1: .env — переменные окружения (пароль приложения)
  │
  ├── Шаг 2: docker-compose.yml (контейнеры + сеть + env_file)
  │     │
  │     ├── Шаг 3a: config.py (MCP сервер name + DATABASE_URL)
  │     │     │
  │     │     └── Шаг 4: opencode.json (MCP клиент URL)
  │     │
  │     └── Шаг 3b: postgres/init/00-roles.sh (роли PG)
  │
  └── Шаг 5: deploy.sh + CI/CD (имена контейнеров)

Шаг 6: миграция БД (athene_memory → memory) + выдача прав — независим, но логически после шага 1
```

## Пошаговый план

### Шаг 1: .env — переменные окружения

- **Файлы:** `.env.example`, `.env` (на сервере)
- **Действия:**
  1. Добавить переменную `SERVICE_NAME=selti`
  2. Добавить переменную `SELTI_DB_PASSWORD=<пароль svc_athene_ai>`
  3. Заменить `DATABASE_URL=postgresql+asyncpg://athena:athena@localhost:5432/athene_memory` → `DATABASE_URL=postgresql+asyncpg://svc_athene_ai:${SELTI_DB_PASSWORD}@localhost:5432/memory`
  4. Заменить `MCP_SERVER_NAME=athena-memory` → `MCP_SERVER_NAME=${SERVICE_NAME}`
  5. На сервере: обновить `.env` аналогично (добавить `SERVICE_NAME`, `SELTI_DB_PASSWORD`)
- **Проверка:** `grep SERVICE_NAME .env && grep SELTI_DB_PASSWORD .env` — переменные существуют
- **Rollback:** `git checkout .env.example`
- **Время:** 5 мин

> **Важно:** Пароль приложения НЕ должен быть в docker-compose.yml. Только в `.env`.

### Шаг 2: docker-compose.yml — контейнеры и сеть

- **Файлы:** `docker-compose.yml`
- **Действия:**
  1. Заменить `container_name: athena-memory` → `container_name: ${SERVICE_NAME}`
  2. Заменить `container_name: athena-celery-worker` → `container_name: ${SERVICE_NAME}-worker`
  3. Заменить `container_name: athena-flower` → `container_name: ${SERVICE_NAME}-flower`
  4. Заменить `container_name: athena-redis` → `container_name: redis`
  5. Заменить `container_name: athena-postgres` → `container_name: postgres`
  6. Заменить `container_name: athena-qdrant` → `container_name: qdrant`
  7. Заменить сеть `athena-net` → `app-net` (все сервисы + секция networks)
  8. В сервисе selti:
     - Добавить `env_file: .env`
     - Добавить `SERVICE_NAME: ${SERVICE_NAME}` в environment
  9. В сервисе celery-worker:
     - Добавить `env_file: .env`
     - Добавить `SERVICE_NAME: ${SERVICE_NAME}` в environment
     - `DATABASE_URL=postgresql+asyncpg://svc_athene_ai:${SELTI_DB_PASSWORD}@postgres:5432/${POSTGRES_DB}`
  10. В сервисе flower:
     - Добавить `env_file: .env`
  11. В сервисе postgres:
     - `POSTGRES_USER: postgres` (суперпользователь для инициализации)
     - `POSTGRES_PASSWORD: postgres` (пароль суперпользователя)
     - `POSTGRES_DB: ${POSTGRES_DB:-memory}`
     - Добавить `env_file: .env`
  12. В healthcheck postgres: `pg_isready -U postgres -d ${POSTGRES_DB:-memory}`
- **Проверка:** `docker compose config` (валидация YAML)
- **Rollback:** `git checkout docker-compose.yml`
- **Время:** 10 мин

> **Важно:** `POSTGRES_USER` и `POSTGRES_PASSWORD` — это credentials для создания БД при инициализации контейнера. Пароль `svc_athene_ai` хранится в `.env` как `SELTI_DB_PASSWORD`. Все переменные подставляются из `.env` через `${...}`.

### Шаг 3a: config.py — MCP сервер name

- **Файлы:** `memory_server/config.py`
- **Действия:**
  1. Строка 16: `database_url: str = "postgresql+asyncpg://athena:athena@localhost:5432/athene_memory"` → `database_url: str = "postgresql+asyncpg://svc_athene_ai:changeme@localhost:5432/memory"`
  2. Строка 31: `mcp_server_name: str = "athena-memory"` → `mcp_server_name: str = os.getenv("SERVICE_NAME", "selti")`
  3. Добавить `import os` если ещё нет
- **Проверка:** `python -c "from memory_server.config import settings; print(settings.mcp_server_name)"` → `selti`
- **Rollback:** `git checkout memory_server/config.py`
- **Время:** 5 мин

### Шаг 3b: postgres/init/00-roles.sh — роли PostgreSQL

- **Файлы:** `postgres/init/00-roles.sh`
- **Действия:**
  1. Заменить `CREATE ROLE athena_app` → `CREATE ROLE svc_athene_ai`
  2. Заменить `GRANT USAGE ON SCHEMA public TO athena_app` → `GRANT USAGE ON SCHEMA public TO svc_athene_ai`
  3. Обновить комментарий в шапке файла
- **Проверка:** валидация bash-синтаксиса (`bash -n postgres/init/00-roles.sh`)
- **Rollback:** `git checkout postgres/init/00-roles.sh`
- **Время:** 5 мин

### Шаг 4: opencode.json — MCP клиент

- **Файлы:** `opencode.json` (в проекте akame, на сервере: `~/.config/opencode/opencode.json`)
- **Действия:**
  1. Найти MCP сервер `athena-memory` в секции `mcp`
  2. Заменить ключ сервера: `"athena-memory"` → `"selti"`
  3. Заменить URL: `"http://athena-memory:8000/mcp/"` → `"http://selti:8000/mcp/"`
  4. **НЕ МЕНЯТЬ** промпты агентов — opencode сам подставляет префикс `{server_name}_tool_name`
- **Проверка:** `opencode` подключается к selti, тулы доступны как `selti_memory_search`, `selti_hash_get` и т.д.
- **Rollback:** `git checkout opencode.json`
- **Время:** 5 мин

### Шаг 5: deploy.sh + CI/CD — имена контейнеров

- **Файлы:** `deploy.sh`, `.github/workflows/auto-deploy.yml`
- **Действия:**
  1. В `deploy.sh`:
     - `COMPOSE_PROJECT="athena-memory"` → `COMPOSE_PROJECT="selti"`
     - `SERVICE_NAME="memory-server"` → `SERVICE_NAME="selti"`
  2. В `.github/workflows/auto-deploy.yml`:
     - Строка 22: `docker compose build memory-server celery-worker` → `docker compose build selti celery-worker`
     - Строка 23: `docker compose up -d memory-server celery-worker` → `docker compose up -d selti celery-worker`
     - Строка 25: `docker compose ps memory-server celery-worker` → `docker compose ps selti celery-worker`
- **Проверка:** YAML валидация CI-файла
- **Rollback:** `git checkout deploy.sh .github/workflows/auto-deploy.yml`
- **Время:** 5 мин

### Шаг 6: Миграция БД (athene_memory → memory) + выдача прав svc_athene_ai

- **Файлы:** новый SQL-скрипт в `migrations/`
- **Действия:**
  1. Создать скрипт `migrations/013_rename_db_and_grant_rights.sql`:
     ```sql
     -- Выполнить от postgres (суперпользователь)
     
     -- 1. Переименование БД: athene_memory → memory
     -- ВНИМАНИЕ: требует остановки всех подключений к БД
     ALTER DATABASE athene_memory RENAME TO memory;
     
     -- 2. Выдача прав svc_athene_ai (существующий пользователь)
     GRANT ALL PRIVILEGES ON DATABASE memory TO svc_athene_ai;
     GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO svc_athene_ai;
     GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO svc_athene_ai;
     GRANT CREATE ON SCHEMA public TO svc_athene_ai;
     ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO svc_athene_ai;
     ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO svc_athene_ai;
     
     -- 3. Обновление владельца таблиц (опционально)
     ALTER TABLE memories OWNER TO svc_athene_ai;
     ALTER TABLE relations OWNER TO svc_athene_ai;
     ALTER TABLE namespaces OWNER TO svc_athene_ai;
     ALTER TABLE resource_hashes OWNER TO svc_athene_ai;
     ALTER TABLE _migrations OWNER TO svc_athene_ai;
     ```
  2. Альтернатива (без downtime): `pg_dump` → создать новую БД → `pg_restore`
  3. Выполнить миграцию на сервере перед деплоем нового docker-compose
- **Проверка:**
  ```bash
  # Проверить что БД переименована
  psql -U postgres -c "\l" | grep memory
  
  # Проверить что svc_athene_ai имеет права
  psql -U postgres -c "SELECT has_table_privilege('svc_athene_ai', 'memories', 'SELECT');"
  psql -U postgres -c "SELECT has_table_privilege('svc_athene_ai', 'memories', 'INSERT');"
  
  # Проверить подключение через svc_athene_ai
  psql -U svc_athene_ai -d memory -c "SELECT count(*) FROM memories;"
  ```
- **Rollback:** `ALTER DATABASE memory RENAME TO athene_memory;`
- **Время:** 15 мин

### Шаг 7: Деплой и верификация

- **Файлы:** —
- **Действия:**
  1. Остановить старые контейнеры: `docker compose down`
  2. Удалить старые имена контейнеров (если остались): `docker rm athena-memory athena-celery-worker athena-flower athena-redis athena-postgres athena-qdrant`
  3. Выполнить миграцию БД (шаг 6)
  4. Запустить новый стек: `docker compose up -d`
  5. Проверить healthcheck: `curl -sf http://localhost:8000/health`
  6. Проверить MCP тулы через opencode: `selti_memory_search`
  7. Проверить логи: `docker compose logs selti --tail=50`
- **Проверка:** все сервисы в статусе `healthy`, тулы отвечают
- **Rollback:** откатить docker-compose.yml, вернуть имена контейнеров, выполнить обратную миграцию БД
- **Время:** 15 мин

## Итого

- **Общее время:** ~60 мин (1 час с запасом)
- **Критические пути:**
  1. Миграция БД — требует остановки подключений. Если есть активные соединения — `RENAME DATABASE` заблокируется
  2. Права svc_athene_ai — нужно выдать права ПЕРЕД переключением приложения
  3. opencode.json — если не обновить URL, все агенты потеряют доступ к памяти
- **Риски:**
  1. **Данные в БД:** при `RENAME` ничего не теряется, но `pg_dump` как fallback safer
  2. **Docker сеть:** `app-net` может конфликтовать с gera, если gera уже использует `app-net` — проверить `docker network ls`
  3. **CI/CD:** GitHub Actions workflow указывает на `memory-server` — нужно обновить до `selti`
  4. **Старые контейнеры:** после `docker compose down` имена контейнеров освобождаются, но если были `docker run` — нужно удалять вручную
  5. **Права svc_athene_ai:** если не выдать права — приложение не сможет работать с БД

## Чек-лист перед деплоем

- [ ] Проверить `docker network ls` — нет ли конфликта `app-net`
- [ ] Создать бэкап БД: `pg_dump -U postgres athene_memory > backup_before_rename.sql`
- [ ] Обновить `.env` на сервере (добавить `SERVICE_NAME`, `SELTI_DB_PASSWORD`)
- [ ] Выдать права svc_athene_ai (SQL из шага 6)
- [ ] Проверить что svc_athene_ai может подключиться: `psql -U svc_athene_ai -d athene_memory -c "SELECT 1;"`
- [ ] Обновить `opencode.json` на сервере
- [ ] Проверить `docker compose config` после всех правок
- [ ] Проверить что `${SERVICE_NAME}` подставляется: `docker compose config | grep container_name`
