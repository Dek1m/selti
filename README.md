# athena-memory — Semantic Memory MCP Server

**athena-memory** — высокопроизводительный MCP-сервер семантической памяти для AI-агентов. Обеспечивает векторное хранение, поиск по семантической близости и интеллектуальную дедупликацию записей на основе протокола MCP (Model Context Protocol) через SSE-транспорт.

---

> **Примечание:** Данный проект разработан с применением технологий искусственного интеллекта в рамках рабочего процесса Argenta Team. Код прошел рецензирование, тестирование и подготовлен к эксплуатации в production-среде.

---

## Стек технологий

| Компонент             | Технология                        |
|-----------------------|-----------------------------------|
| Язык                  | Python 3.12                       |
| Фреймворк             | FastAPI + FastMCP                 |
| База данных           | PostgreSQL 17 + pgvector (HNSW)   |
| Кеш                   | Redis 7                           |
| Мониторинг            | Prometheus + Grafana              |
| Контейнеризация       | Docker + Docker Compose           |

---

## Архитектура

Система построена по многослойной архитектуре с чётким разделением ответственности:

```
Client (MCP over SSE)
       |
       v
   FastAPI / FastMCP ─── Auth Middleware (опционально)
       |
       v
   ┌─────────────────────────────────────────┐
   │            MCP Tools (10)               │
   │  store / search / get / update / delete │
   │  list / forget / ingest / stats / find  │
   └──────────────────────┬──────────────────┘
                          │
                          v
   ┌─────────────────────────────────────────┐
   │           MemoryService                 │
   │        (бизнес-логика)                  │
   └──────┬──────────────────────┬───────────┘
          │                      │
          v                      v
   ┌───────────┐        ┌───────────────┐
   │ DedupEngine│◄──────►│ Embedding API │
   │exact+sem. │        │  + Redis Cache│
   └─────┬─────┘        └───────┬───────┘
         │                      │
         v                      v
   ┌─────────────────────────────────────────┐
   │          MemoryRepository               │
   │       (SQL via asyncpg)                 │
   └──────────────────┬──────────────────────┘
                      │
                      v
   ┌─────────────────────────────────────────┐
   │    PostgreSQL 17 + pgvector (HNSW)      │
   │        8192-мерные эмбеддинги           │
   └─────────────────────────────────────────┘
```

**Слои архитектуры:**

- **MCP Tools** — 10 инструментов, декорированных FastMCP. Валидация namespace, трекинг метрик, обработка ошибок.
- **DedupEngine** — двухуровневая дедупликация: точная (SHA256) и семантическая (cosine distance).
- **MemoryService** — координатор бизнес-логики: вызов эмбеддингов, дедупликация, взаимодействие с репозиторием.
- **Repository** — уровень доступа к данным на asyncpg; сырые SQL-запросы с параметризацией.
- **PostgreSQL / pgvector** — HNSW-индекс для 8192-мерных векторов, B-tree индексы для фильтрации, JSONB для метаданных.
- **Redis Cache** — кеш эмбеддингов (SHA256-ключи, TTL 24 часа); снижает нагрузку на Embedding API.

---

## MCP Tools

Сервер предоставляет 10 инструментов для управления семантической памятью:

| Tool                  | Описание                                                   | Параметры                                               |
|-----------------------|------------------------------------------------------------|---------------------------------------------------------|
| `memory_store`        | Сохранить запись с дедупликацией                           | content, user_id, metadata?, namespace?                 |
| `memory_search`       | Векторный поиск по семантической близости                  | query, user_id, limit?, threshold?, namespace?          |
| `memory_get`          | Получить запись по идентификатору                          | id                                                      |
| `memory_update`       | Обновить содержимое и/или метаданные записи                | id, content?, metadata?                                 |
| `memory_delete`       | Удалить запись по идентификатору                           | id                                                      |
| `memory_list`         | Список записей с фильтрацией и пагинацией                  | user_id?, namespace?, limit?, offset?                   |
| `memory_forget`       | Массовое удаление всех записей пользователя                | user_id, namespace?                                     |
| `memory_ingest_batch` | Массовое сохранение набора записей с дедупликацией         | entries: list[{content, metadata?, namespace?}], user_id |
| `memory_stats`        | Статистика по неймспейсам: количество записей, дата обновления | user_id                                              |
| `memory_find_similar` | Поиск семантически похожих записей без сохранения          | content, user_id, limit?, threshold?, namespace?        |

Каждый инструмент инструментирован метриками: количество вызовов, длительность выполнения, статус (ok/error).

---

## Namespace-стратегия

Namespace обеспечивают логическую изоляцию данных в рамках одной базы. Передаются опционально (по умолчанию — `default`). Валидация на уровне tools; неверное значение вызывает `ValueError`.

| Namespace            | Назначение                         |
|----------------------|------------------------------------|
| `default`            | Общие записи                       |
| `user_facts`         | Факты о пользователе               |
| `code_knowledge`     | Знания из кодовой базы             |
| `dialogue_insights`  | Инсайты из диалогов                |
| `project_meta`       | Метаданные проектов                |

---

## Дедупликация

Ядро системы — `DedupEngine`, реализующий двухуровневую стратегию предотвращения дубликатов.

### Уровень 1: Exact Match

Вычисляется SHA256(content). Выполняется поиск по `content_hash` в пределах namespace:

- **user_facts** → `UPDATE` (перезапись существующей записи)
- **остальные** → `SKIP` (пропуск, возврат существующей записи)

### Уровень 2: Semantic Match

Если точное совпадение не найдено, генерируется эмбеддинг и выполняется векторный поиск. При `score >= threshold` запись считается дубликатом → `SKIP`.

### Пороги семантической дедупликации (per-namespace)

| Namespace            | Порог  |
|----------------------|--------|
| `default`            | 0.95   |
| `user_facts`         | 0.90   |
| `code_knowledge`     | 0.95   |
| `dialogue_insights`  | 0.85   |
| `project_meta`       | 0.90   |

Пороги настраиваются через переменную `DEDUP_THRESHOLDS`. Полное отключение — `DEDUP_ENABLED=false`.

---

## Быстрый старт

### Предварительные требования

- Docker 24+ и Docker Compose v2
- Python 3.12 (для миграций вне контейнера)

### Запуск

```bash
# 1. Клонировать репозиторий
git clone <repository-url>
cd selti

# 2. Скопировать шаблон окружения
cp .env.example .env

# 3. Сгенерировать пароли
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 4. Заполнить .env:
#    PG_PASSWORD     — пароль суперпользователя PostgreSQL
#    APP_PASSWORD    — пароль пользователя приложения athena_app
#    REDIS_PASSWORD  — пароль Redis

# 5. Запустить с локальными PostgreSQL и Redis
docker compose --profile local-db up -d

# 6. Проверить здоровье сервера
curl http://localhost:8000/health

# 7. Применить миграции базы данных
python migrations/run.py
```

Для подключения к внешним PostgreSQL и Redis — запустите без профиля `--profile local-db` и укажите соответствующие URL в `.env`.

---

## Конфигурация

### Переменные окружения

| Переменная               | Описание                                   | По умолчанию                                              |
|--------------------------|--------------------------------------------|-----------------------------------------------------------|
| `DATABASE_URL`           | PostgreSQL connection string (asyncpg)     | `postgresql+asyncpg://athena:athena@localhost:5432/athena_memory` |
| `REDIS_URL`              | Redis connection string                    | `redis://:@redis:6379/0`                                 |
| `EMBEDDING_API_URL`      | URL API эмбеддингов (OpenAI-совместимый)   | `http://10.0.0.21:8080/v1`                               |
| `EMBEDDING_API_KEY`      | Ключ аутентификации API эмбеддингов        | (пусто)                                                   |
| `EMBEDDING_MODEL`        | Модель эмбеддингов                         | `qwen3-embedding-8b`                                      |
| `EMBEDDING_DIMENSION`    | Размерность эмбеддинга                     | `8192`                                                    |
| `API_KEY`                | Ключ аутентификации MCP-сервера            | (пусто — аутентификация отключена)                        |
| `LOG_LEVEL`              | Уровень логирования                        | `INFO`                                                    |
| `DEDUP_ENABLED`          | Включить дедупликацию                      | `true`                                                    |
| `DEDUP_THRESHOLD`        | Глобальный порог семантической дедупликации | `0.95`                                                   |
| `SEARCH_DEFAULT_LIMIT`   | Лимит результатов поиска по умолчанию      | `10`                                                      |
| `SEARCH_DEFAULT_THRESHOLD`| Порог релевантности поиска по умолчанию    | `0.7`                                                     |
| `MCP_HOST`               | Хост сервера                               | `0.0.0.0`                                                 |
| `MCP_PORT`               | Порт сервера                               | `8000`                                                    |
| `PG_USER`                | Пользователь PostgreSQL (локальный профиль)| `athena`                                                  |
| `PG_PASSWORD`            | Пароль PostgreSQL (локальный профиль)      | —                                                         |
| `REDIS_PASSWORD`         | Пароль Redis (локальный профиль)           | —                                                         |

### PostgreSQL (локальный профиль)

Для локального запуска используется образ `pgvector/pgvector:pg17` с предварительно настроенным HNSW-индексом:

- Расширение `vector`
- Таблица `memories` с колонкой `embedding vector(8192)`
- HNSW-индекс с параметрами `m = 16`, `ef_construction = 200`
- B-tree индексы: `user_id`, `namespace`, `created_at DESC`
- Триггер автообновления `updated_at`
- Уникальный индекс на `(namespace, content_hash)` для точной дедупликации

---

## Аутентификация

Опциональная защита на основе API-ключа. Включается установкой переменной `API_KEY` в `.env`.

**Механизм:**

- HTTP-middleware проверяет заголовок `Authorization: Bearer <API_KEY>` для всех эндпоинтов
- ASGI-middleware защищает `/mcp` (mount-приложение FastMCP)
- Белый список (доступ без аутентификации): `/health`, `/metrics`

При пустом значении `API_KEY` доступ открыт.

---

## Мониторинг

### Метрики Prometheus

Эндпоинт `/metrics` предоставляет 13+ метрик:

| Метрика                                   | Тип       | Описание                                    |
|-------------------------------------------|-----------|---------------------------------------------|
| `athena_http_requests_total`              | Counter   | Количество HTTP-запросов (method, endpoint, status) |
| `athena_http_request_duration_seconds`    | Histogram | Длительность HTTP-запросов                  |
| `athena_db_pool_size`                     | Gauge     | Текущий размер пула соединений              |
| `athena_db_pool_available`                | Gauge     | Доступные соединения в пуле                 |
| `athena_embedding_duration_seconds`       | Histogram | Длительность вызова API эмбеддингов         |
| `athena_search_results_count`             | Histogram | Количество результатов поиска               |
| `athena_memory_count`                     | Gauge     | Общее количество записей (по namespace)     |
| `athena_mcp_tool_calls_total`             | Counter   | Вызовы MCP-инструментов (tool, status)      |
| `athena_mcp_tool_duration_seconds`        | Histogram | Длительность выполнения MCP-инструментов    |
| `athena_embedding_cache_hits_total`       | Counter   | Попадания в кеш эмбеддингов                 |
| `athena_embedding_cache_misses_total`     | Counter   | Промахи кеша эмбеддингов                    |
| `athena_dedup_skipped_total`              | Counter   | Пропуски дедупликации (namespace, reason)   |
| `athena_dedup_inserted_total`             | Counter   | Вставки после проверки дедупликации         |

### Healthcheck

```bash
curl http://localhost:8000/health
```

Ответ содержит версию сервера и статусы проверок конфигурации.

### Grafana Dashboard

Готовый dashboard для PostgreSQL + pgvector — `monitoring/dashboards/postgres-pgvector.json`.

### Экспортёры

Для production-развёртывания предусмотрены экспортёры (включаются через `-f monitoring/exporters/docker-compose.exporters.yml`):

- **postgres-exporter** (порт 9187) — метрики PostgreSQL
- **redis-exporter** (порт 9121) — метрики Redis

### Алерты

Правила алертинга — `monitoring/alerts/prometheus-rules.yml`:

- Доступность PostgreSQL и Redis
- Высокая загрузка соединений
- Конфликты запросов на реплике
- Долгие запросы (> 5 минут)
- Отставание WAL-архивации
- Отсутствие бэкапов
- Отсутствие HNSW-индекса при > 10k записей
- Высокое потребление памяти Redis
- Высокий процент промахов кеша Redis

---

## Миграции

Управление схемой базы данных — через встроенный migration runner.

```bash
# Применить все неприменённые миграции
python migrations/run.py

# Откатить последнюю миграцию
python migrations/run.py --down
```

Миграции находятся в `migrations/` — версионированные SQL-файлы с up/down-секциями. Система отслеживает применённые миграции в таблице `_migrations`.

**Доступные миграции:**

| Файл              | Описание                                    |
|-------------------|---------------------------------------------|
| `001_initial.sql` | Начальная схема: расширение vector, таблица memories, HNSW-индекс, триггеры, функция поиска |
| `002_dedup.sql`   | Дедупликация: content_hash, source_type, source_location, version, is_archived, уникальный индекс |

---

## Zero-downtime Deploy

Скрипт `deploy.sh` реализует стратегию rolling-обновления:

1. Пулл нового образа из GHCR
2. Запуск нового контейнера на временном порту
3. Ожидание прохождения healthcheck
4. Переключение трафика (через reverse proxy или прямой restart)
5. Остановка и удаление старого контейнера

---

## Тестирование

Проект покрыт модульными и интеграционными тестами (142+ теста).

```bash
# Установка зависимостей для тестов
pip install -r requirements.txt

# Запуск тестов
pytest tests/ -v

# С отчётом о покрытии
pytest tests/ --cov=memory_server -v
```

---

## Разработка

Проект разработан в рамках **Argenta Team** — архитектура, разработка и сопровождение информационных систем.

Разработчик: [Dek1m](https://github.com/Dek1m)

---

**Argenta Team** — архитектура, разработка и сопровождение информационных систем.
