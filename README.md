# athena-memory — Semantic Memory MCP Server

Memory MCP сервер для AI-агентов. Хранит и ищет записи по семантической близости. Работает по протоколу MCP (Model Context Protocol) через SSE-транспорт.

## Стек

- Python 3.12 / FastAPI + FastMCP
- PostgreSQL 17 + pgvector (HNSW, 8192d)
- Redis 7 (кеш эмбеддингов)
- Prometheus + Grafana
- Docker / Docker Compose

## Быстрый старт

```bash
# 1. Скопировать шаблон окружения
cp .env.example .env

# 2. Заполнить пароли в .env (PG_PASSWORD, APP_PASSWORD, REDIS_PASSWORD)
#    Сгенерировать: python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 3. Запустить с локальной БД и Redis
docker compose --profile local-db up -d

# 4. Проверить здоровье
curl http://localhost:8000/health

# 5. Применить миграции
python migrations/run.py
```

Для подключения к внешней PostgreSQL/Redis — убери флаг `--profile local-db`.

## Архитектура

```
Client (MCP over SSE) → FastMCP → Tools → MemoryService → Repository → PostgreSQL + pgvector
                                            ↕
                                      DedupEngine (exact + semantic)
                                            ↕
                                     Redis (кэш эмбеддингов)
```

Слои:
- **Tools** — FastMCP-обработчики, валидация namespace
- **Service** — бизнес-логика, вызов DedupEngine, координация embedding/repository
- **Repository** — сырые SQL-запросы через asyncpg
- **Database** — PostgreSQL 17 с pgvector (HNSW-индекс, 8192 измерений)

Полная дорожная карта — [PLAN.md](PLAN.md).

## MCP Tools

| Tool | Описание | Параметры |
|---|---|---|
| `memory_store` | Сохранить запись (с дедупликацией) | content, user_id, metadata?, namespace? |
| `memory_search` | Векторный поиск | query, user_id, limit?, threshold?, namespace? |
| `memory_get` | Получить по ID | id |
| `memory_update` | Обновить | id, content?, metadata? |
| `memory_delete` | Удалить | id |
| `memory_list` | Список с пагинацией | user_id?, namespace?, limit?, offset? |
| `memory_forget` | Массовое удаление | user_id, namespace? |
| `memory_ingest_batch` | Массовое сохранение | entries: list, user_id |
| `memory_stats` | Статистика по неймспейсам | user_id |
| `memory_find_similar` | Поиск без сохранения | content, user_id, limit?, threshold?, namespace? |

## Namespace-стратегия

| Namespace | Назначение |
|---|---|
| `default` | Обычные записи |
| `user_facts` | Факты о пользователе |
| `code_knowledge` | Знания из кодовой базы |
| `dialogue_insights` | Инсайты из диалогов |
| `project_meta` | Метаданные проектов |

Неймспейс передаётся опционально. Если не указан — используется `default`. Валидация выполняется на уровне tools; неверное значение вызывает `ValueError`.

## Дедупликация

Алгоритм DedupEngine:

1. **Exact match** — SHA256(content) → поиск по `content_hash` в БД. Если найден:
   - `user_facts` → `update` (перезапись)
   - остальные → `skip` (пропуск)

2. **Semantic match** — если exact не сработал: эмбеддинг → векторный поиск с порогом. Если `score >= threshold` → `skip`.

Пороги по умолчанию (настраиваются через `DEDUP_THRESHOLDS`):

| Namespace | Порог |
|---|---|
| default | 0.95 |
| user_facts | 0.90 |
| code_knowledge | 0.95 |
| dialogue_insights | 0.85 |
| project_meta | 0.90 |

Отключить дедупликацию: `DEDUP_ENABLED=false` в .env.

## Аутентификация

Опциональная. Включить через `API_KEY` в .env. Если ключ задан:

- Все эндпоинты (включая `/mcp`) требуют заголовок `Authorization: Bearer <API_KEY>`
- Белый список (без аутентификации): `/health`, `/metrics`

Если `API_KEY` пуст — доступ открыт.

## Мониторинг

- **`/metrics`** — Prometheus-эндпоинт (7+ метрик: количество вызовов, длительность, размер пула, hit/miss кеша, dedup-skip)
- **`/health`** — healthcheck с проверкой конфига
- **Grafana dashboard** — `monitoring/dashboards/`

## Миграции

```bash
# Применить миграции
python migrations/run.py

# Откатить последнюю
python migrations/run.py --down
```

Миграции в `migrations/` — SQL-файлы с версионированием.

## Переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `DATABASE_URL` | PostgreSQL (asyncpg) | `postgresql+asyncpg://athena:athena@localhost:5432/athena_memory` |
| `REDIS_URL` | Redis | `redis://:@redis:6379/0` |
| `EMBEDDING_API_URL` | API эмбеддингов | `http://10.0.0.21:8080/v1` |
| `EMBEDDING_API_KEY` | Ключ API эмбеддингов | (пусто) |
| `EMBEDDING_MODEL` | Модель эмбеддингов | `qwen3-embedding-8b` |
| `EMBEDDING_DIMENSION` | Размерность эмбеддинга | `8192` |
| `API_KEY` | Ключ аутентификации MCP | (пусто — без аутентификации) |
| `LOG_LEVEL` | Уровень логирования | `INFO` |
| `DEDUP_ENABLED` | Включить дедупликацию | `true` |
| `DEDUP_THRESHOLD` | Глобальный порог semantic dedup | `0.95` |
| `SEARCH_DEFAULT_LIMIT` | Лимит поиска по умолчанию | `10` |
| `SEARCH_DEFAULT_THRESHOLD` | Порог поиска по умолчанию | `0.7` |
| `MCP_HOST` | Хост сервера | `0.0.0.0` |
| `MCP_PORT` | Порт сервера | `8000` |
| `PG_USER` | Пользователь PostgreSQL | `athena` |
| `PG_PASSWORD` | Пароль PostgreSQL (локальный профиль) | — |
| `REDIS_PASSWORD` | Пароль Redis (локальный профиль) | — |
