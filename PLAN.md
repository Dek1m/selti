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
- **Что:** Добавлены поля `content_hash`, `source_type`, `source_location`, `version`, `is_archived`. Уникальный индекс `(namespace, content_hash)`. Индексы на source_type, source_location.
- **Файлы:** `migrations/002_dedup.sql`
- **Статус:** ✅
- **Кто:** Нора

### 1.2. DedupEngine
- **Что:** Ядро дедупликации: exact match (SHA256) + semantic match (cosine > порог). Настраиваемые пороги per-namespace.
- **Файлы:** `memory_server/memory/dedup.py`
- **Статус:** ✅
- **Кто:** Сона

### 1.3. Интеграция DedupEngine в MemoryService
- **Что:** При `store()` сначала dedup, потом insert. Возвращает `tuple[MemoryRecord, DedupAction]`.
- **Файлы:** `memory_server/memory/service.py`, `memory_server/memory/repository.py`, `memory_server/tools/memory_tools.py`
- **Статус:** ✅
- **Кто:** Сона

### 1.4. Тесты дедупликации
- **Что:** 13 тестов DedupEngine (exact, semantic, edge cases). Всего 102 теста.
- **Файлы:** `tests/test_dedup.py`
- **Статус:** ✅
- **Кто:** Катерина

---

## Фаза 2 — Инфраструктура

### 2.1. Redis кеш эмбеддингов
- **Что:** EmbeddingCache (get/set/mget/mset, sha256-ключи, TTL 24ч). Интеграция в EmbeddingClient и lifespan.
- **Файлы:** `memory_server/cache/redis_client.py`, `memory_server/cache/__init__.py`, `memory_server/server.py`, `memory_server/embedding/client.py`, `requirements.txt`
- **Статус:** ✅
- **Кто:** Сона

### 2.2. Аутентификация
- **Что:** HTTP middleware + ASGI middleware для `/mcp`. API key из .env. Белый список: `/health`, `/metrics`.
- **Файлы:** `memory_server/__main__.py`, `memory_server/config.py`, `.env.example`
- **Статус:** ✅
- **Кто:** Сона + Лита (security review)

### 2.3. Тесты инфраструктуры
- **Что:** 18 тестов кеша, 8 тестов auth. Всего 127 тестов.
- **Файлы:** `tests/test_cache.py`, `tests/test_auth.py`
- **Статус:** ✅
- **Кто:** Катерина

### 2.4. Security fixes
- **Что:** Исправлены: утечка пароля БД в логах, неинтерполируемый Redis URL, незащищённый `/mcp`.
- **Файлы:** `memory_server/server.py`, `memory_server/config.py`, `memory_server/__main__.py`
- **Статус:** ✅
- **Кто:** Лита

---

## Фаза 3 — Новый функционал

### 3.1. Новые MCP tools
- `memory_ingest_batch` — массовое сохранение с dedup
- `memory_stats` — статистика по namespace
- `memory_find_similar` — поиск похожих без сохранения
- **Файлы:** `memory_server/tools/memory_tools.py`, `memory_server/memory/service.py`, `memory_server/memory/repository.py`, `memory_server/db/queries.py`, `memory_server/models.py`
- **Статус:** ✅
- **Кто:** Сона

### 3.2. Namespace-стратегия
- **Что:** Enum `Namespace` с `DEFAULT`, `USER_FACTS`, `CODE_KNOWLEDGE`, `DIALOGUE_INSIGHTS`, `PROJECT_META`. Валидация во всех tools.
- **Файлы:** `memory_server/config.py`, `memory_server/tools/memory_tools.py`
- **Статус:** ✅
- **Кто:** Сона

### 3.3. Расширенный healthcheck
- **Что:** `/health` возвращает version, checks.config (dedup_enabled, api_key_configured, redis_configured).
- **Файлы:** `memory_server/__main__.py`
- **Статус:** ✅
- **Кто:** Сона

### 3.4. Тесты нового функционала
- **Что:** 12 тестов tools, 3 теста health. Всего 142 теста.
- **Файлы:** `tests/test_tools.py`, `tests/test_health.py`
- **Статус:** ✅
- **Кто:** Катерина

---

## Фаза 4 — Качество

### 4.1. Метрики для MCP tools
- **Что:** 6 новых метрик: `mcp_tool_calls_total`, `mcp_tool_duration_seconds`, `cache_hits/misses`, `dedup_skipped/inserted`. Инструментированы все 10 tools, EmbeddingClient, DedupEngine.
- **Файлы:** `memory_server/metrics.py`, `memory_server/tools/memory_tools.py`, `memory_server/embedding/client.py`, `memory_server/memory/dedup.py`
- **Статус:** ✅
- **Кто:** Сона + Мая

### 4.2. Документация
- **Что:** README.md с описанием: быстрый старт, 10 MCP tools, namespace-стратегия, дедупликация, аутентификация, мониторинг, миграции, переменные окружения.
- **Файлы:** `README.md`
- **Статус:** ✅
- **Кто:** Тиамат

### 4.3. Итоговые тесты
- **Что:** 142 теста, все зелёные. Покрытие: модули, интеграция, регрессия.
- **Статус:** ✅
- **Кто:** Катерина
