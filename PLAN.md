# План улучшений MCP Memory Server

## Фаза 0 — Критические фиксы

### 0.1. Починить метрики пула
- **Проблема:** `athena_db_pool_size` и `athena_db_pool_available` объявлены, но никогда не обновляются.
- **Решение:** Все метрики вынесены в `memory_server/metrics.py`. Метрики пула обновляются в lifespan + фоновая задача (каждые 15с).
- **Файлы:** `memory_server/metrics.py` (новый), `memory_server/__main__.py`, `memory_server/server.py`
- **Статус:** ✅

### 0.2. Threshold в SQL
- **Проблема:** Порог отсечения применялся в Python, а не в SQL.
- **Решение:** Добавлено `WHERE 1 - (embedding <=> $1::vector) >= $4` в `SEARCH_MEMORIES`.
- **Файлы:** `memory_server/db/queries.py`
- **Статус:** ✅

### 0.3. Структурированное логирование
- **Проблема:** `logging.basicConfig` с простым текстовым форматом. Нет correlation ID.
- **Решение:** JSONFormatter, correlation ID через ContextVar, JSON-логи с timestamp/level/logger/message/request_id/duration_ms.
- **Файлы:** `memory_server/server.py`, `memory_server/__main__.py`
- **Статус:** ✅

### 0.4. Тесты Фазы 0
- **Что:** 30 новых тестов: metrics (15), logging (15). Исправлены существующие тесты под изменения.
- **Файлы:** `tests/test_metrics.py` (новый), `tests/test_logging.py` (новый), `tests/conftest.py`
- **Статус:** ✅

---

## Фаза 1 — Дедупликация (ключевая фича)

### 1.1. Миграция 002 — content_hash, source_type, source_location
- **Что:** Добавить поля `content_hash`, `source_type`, `source_location`. Уникальный индекс `(namespace, content_hash)` для `user_facts`.
- **Файлы:** `migrations/002_dedup.sql`, `migrations/run.py`
- **Статус:** ❌

### 1.2. DedupEngine
- **Что:** Ядро дедупликации: exact match (SHA256) + semantic match (cosine > порог). Настраиваемые пороги per-namespace.
- **Файлы:** `memory_server/memory/dedup.py`
- **Статус:** ❌

### 1.3. Интеграция DedupEngine в MemoryService
- **Что:** При `store()` сначала dedup, потом insert. Возвращает `{action, id, existing_score?}`.
- **Файлы:** `memory_server/memory/service.py`, `memory_server/memory/repository.py`
- **Статус:** ❌

---

## Фаза 2 — Инфраструктура

### 2.1. Redis кеш эмбеддингов
- **Что:** Клиент для Redis, кеш `{sha256(text) → embedding}`, TTL 24h, hit/miss метрики.
- **Файлы:** `memory_server/cache/redis_client.py`, `memory_server/server.py`, `memory_server/embedding/client.py`, `requirements.txt`
- **Статус:** ❌

### 2.2. Аутентификация
- **Что:** Middleware на `Authorization: Bearer <api_key>`. Если ключ не задан — открытый доступ.
- **Файлы:** `memory_server/__main__.py`, `memory_server/config.py`, `.env.example`
- **Статус:** ❌

---

## Фаза 3 — Новый функционал

### 3.1. Новые MCP tools
- `memory_ingest_batch` — массовое сохранение с batch embedding
- `memory_stats` — статистика по namespace
- `memory_find_similar` — поиск похожих без сохранения
- **Файлы:** `memory_server/tools/memory_tools.py`
- **Статус:** ❌

### 3.2. Namespace-стратегия
- **Что:** Enum `Namespace` с `DEFAULT`, `USER_FACTS`, `CODE_KNOWLEDGE`, `DIALOGUE_INSIGHTS`, `PROJECT_META`. Валидация в tools.
- **Файлы:** `memory_server/config.py`, `memory_server/tools/memory_tools.py`
- **Статус:** ❌

### 3.3. Расширенный healthcheck
- **Что:** Проверка подключения к БД, Redis, опционально embedding API.
- **Файлы:** `memory_server/__main__.py`
- **Статус:** ❌

---

## Фаза 4 — Качество

### 4.1. Тесты
- DedupEngine, batch ingest, аутентификация, Redis кеш, метрики пула.
- **Файлы:** `tests/test_dedup.py`, `tests/test_auth.py` и обновление существующих
- **Статус:** ❌

### 4.2. Метрики для MCP tools
- `athena_mcp_tool_calls_total`, `athena_mcp_tool_duration_seconds`, `athena_cache_hit_ratio`, `athena_dedup_skipped_total`
- **Файлы:** `memory_server/__main__.py`, `memory_server/tools/memory_tools.py`
- **Статус:** ❌

### 4.3. Документация
- Namespace-стратегия, аутентификация, batch ingest, дедупликация, метрики.
- **Файлы:** `README.md`
- **Статус:** ❌
