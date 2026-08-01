# План оптимизации selti

> Декомпозиция по результатам аудита: Сона (код), Нора (БД), Эна (архитектура), Рэй (инфраструктура), Мая (observability)
> Текущая версия: v0.11.2 (коммит 37e4474)
> Python 3.14, PostgreSQL, Qdrant, Redis, Celery, FastMCP

---

## Фаза 1: Quick Wins (P0) — 2-3 дня

Максимальный эффект за минимальное время. Независимые задачи, можно параллелить.

### Шаг 1.1: Подключить хранимки из миграции 009

- **Файлы:** `memory_server/db/queries.py`, `memory_server/memory/repository.py`, `memory_server/memory/service.py`
- **Что делаем:** Хранимки `graph_traverse_full`, `graph_stats_unified`, `memory_upsert`, `memory_insert_batch` уже созданы в миграции 009, но НЕ подключены к коду. Подключаем:
  - `repository.py`: переключаем `traverse()` на вызов `graph_traverse_full` (1 round-trip вместо 41)
  - `repository.py`: переключаем `insert_batch()` на `memory_insert_batch` (1 round-trip вместо 50)
  - `repository.py`: переключаем `upsert()` на `memory_upsert` (1 round-trip вместо 2-3)
  - `service.py`: убираем `get_by_id` + `get_relations` в цикле traverse — заменяем на один вызов
- **Зависимости:** —
- **Время:** 2-3 часа
- **Риск:** Низкий. Хранимки уже протестированы в миграции. Проверить совместимость сигнатур
- **Ожидаемый эффект:** Traverse 27x-200x быстрее, batch insert 50x быстрее, upsert 2-3x быстрее

### Шаг 1.2: Дедупликация PG insert в insert_batch

- **Файлы:** `memory_server/db/queries.py`, `memory_server/memory/repository.py`
- **Что делаем:** Убираем идентичные ветки if/else в `insert_batch`. Если подключены хранимки (шаг 1.1), дублирование исчезает автоматически. Если нет — рефакторим в единую ветку
- **Зависимости:** шаг 1.1 (желателен)
- **Время:** 30 мин
- **Риск:** Низкий
- **Ожидаемый эффект:** Чище код, меньше техдолг

### Шаг 1.3: Health check через пул соединений + метрики

- **Файлы:** `memory_server/server.py` (или модуль health check), `memory_server/metrics.py`
- **Что делаем:** Health check создаёт новые соединения вместо использования pool. Переключаем на `pool.acquire()` с таймаутом. Дополнительно: экспортируем health status в Prometheus (gauge `selti_health_status`, counter `selti_health_checks_total`)
- **Зависимости:** —
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Не засоряем пул лишними соединениями, корректная проверка доступности + метрики для алертов

### Шаг 1.4: Prometheus metrics — PROMETHEUS_MULTIPROC_DIR

- **Файлы:** `Dockerfile`, `docker-compose.yml`, `memory_server/metrics.py` (или где настраивается prometheus)
- **Что делаем:** В Celery prefork.metrics не агрегируются без `PROMETHEUS_MULTIPROC_DIR`. Добавляем env var в docker-compose, создаём tmpdir для multiproc, чистим при старте воркера
- **Важно (от Мая):** БЕЗ ЭТОГО 19 алертов НЕ РАБОТАЮТ. Это не просто nice-to-have — это блокер для всей системы мониторинга. Все метрики в multi-process режиме (uvicorn + celery workers) будут невалидными
- **Зависимости:** —
- **Время:** 1 час
- **Риск:** Средний. Если неправильно настроить — метрики дублируются или теряются. Тестировать с `curl /metrics`
- **Ожидаемый эффект:** Корректная агрегация метрик в multi-process режиме, 19 алертов начинают работать

### Шаг 1.5: Дедупликация метрик — оставить ТОЛЬКО в dedup.py

- **Файлы:** `memory_server/memory/dedup.py`, `memory_server/tools/memory_tools.py`, `memory_server/metrics.py`
- **Что делаем:** Метрики Prometheus дублируются в `dedup.py` и `memory_tools.py`. По рекомендации Мая: оставляем метрики ТОЛЬКО в `dedup.py` (там где бизнес-логика дедупликации). Убираем дублирующие метрики из `memory_tools.py`. Если метрики нужны в `memory_tools.py` — импортируем из `dedup.py`, а не дублируем
- **Зависимости:** —
- **Время:** 45 мин
- **Риск:** Низкий
- **Ожидаемый эффект:** Единый источник метрик, нет конфликтов имён, нет двойного счёта

### Шаг 1.6: Добавить .dockerignore

- **Файлы:** `.dockerignore` (новый)
- **Что делаем:** Исключаем из Docker context: `.git`, `__pycache__`, `.venv`, `tests/`, `*.pyc`, `.env`, `node_modules`, `plan.md`
- **Зависимости:** —
- **Время:** 15 мин
- **Риск:** Низкий
- **Ожидаемый эффект:** Меньше образ, быстрее сборка, нет секретов в контейнере

### Шаг 1.7: CPU limits в docker-compose

- **Файлы:** `docker-compose.yml`
- **Что делаем:** Добавляем `cpus: "2.0"` (или по необходимости) для каждого сервиса. Уже есть `mem_limit`, нет `cpus`
- **Зависимости:** —
- **Время:** 15 мин
- **Риск:** Низкий
- **Ожидаемый эффект:** Предотвращение starve других сервисов на хосте

---

## Фаза 2: Производительность БД + Observability (P1) — 4-5 дней

Критические узкие места в БД + ключевые observability-улучшения. Требуют координации Нора + Сона + Мая.

### Шаг 2.1: Подключить get_relations_unified (UNION ALL)

- **Файлы:** `memory_server/db/queries.py` (новый SQL), `memory_server/memory/repository.py`
- **Что делаем:** Создаём хранимку `get_relations_unified` с UNION ALL для получения входящих и исходящих связей за 1 запрос. Подключаем к `repository.get_relations()`. Сейчас — 2 отдельных запроса
- **Зависимости:** —
- **Время:** 1.5 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** get_relations 2x быстрее

### Шаг 2.2: list_with_count через window function

- **Файлы:** `memory_server/db/queries.py`, `memory_server/memory/repository.py`
- **Что делаем:** Сейчас `list()` делает 2 запроса: SELECT + COUNT. Заменяем на один запрос с `COUNT(*) OVER()` window function. Убираем отдельный COUNT
- **Зависимости:** —
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** list 2x быстрее, на 1 round-trip меньше

### Шаг 2.3: memory_forget_soft (soft delete)

- **Файлы:** `migrations/013_forget_soft.sql` (новый), `memory_server/db/queries.py`, `memory_server/memory/repository.py`
- **Что делаем:** Сейчас FORGET — hard delete. Создаём хранимку `memory_forget_soft` которая ставит `is_archived = true` + `archived_at = now()`. Обновляем `repository.forget()` на вызов хранимки. Добавляем partial unique index с учётом `is_archived`
- **Зависимости:** —
- **Время:** 1.5 часа
- **Риск:** Средний. Нужно мигрировать существующие данные, если хотим "восстановление". Пока просто soft delete без UI восстановления
- **Ожидаемый эффект:** Данные не теряются навсегда, можно откатить ошибочный forget

### Шаг 2.4: copy_records_to_table для ingest

- **Файлы:** `memory_server/memory/repository.py`, `memory_server/db/queries.py`
- **Что делаем:** Для `ingest_batch` заменяем поштучные INSERT на `asyncpg.Connection.copy_records_to_table()` — 450x быстрее bulk insert. Альтернатива: `executemany` с batch. Оцениваем что лучше для текущего use case
- **Зависимости:** шаг 1.1 (хранимки) — если подключены, оценить нужен ли ещё copy
- **Время:** 2 часа
- **Риск:** Средний. copy_records_to_table требует точного соответствия column order. Тестировать с реальными данными
- **Ожидаемый эффект:** Ingest 450x быстрее для больших батчей

### Шаг 2.5: Correlation ID → Celery tasks

- **Файлы:** `memory_server/tools/task_bridge.py`, `memory_server/memory/celery_app.py` (или signals.py), `memory_server/memory/service.py`
- **Что делаем:** Пробрасываем correlation ID из MCP-запроса в Celery tasks:
  - В `task_bridge.py`: при вызове `.delay()` / `.apply_async()` передаём `correlation_id` в `task_kwargs` или `headers`
  - В `celery_app.py` / `signals.py`: при старте task извлекаем `correlation_id` из headers, записываем в `contextvars`
  - В `service.py`: все логи внутри task содержат `correlation_id`
  - Паттерн: `current_task.request.headers.get('correlation_id')` → `contextvars`
- **Зависимости:** —
- **Время:** 2 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** Корреляция логов между MCP-запросом и Celery task, трейсинг ошибок от клиента до воркера

### Шаг 2.6: Structured JSON logging — замена ArgentaFormatter

- **Файлы:** `memory_server/logging_config.py` (или config), `requirements.txt` (добавить `structlog`), все модули с `logger`
- **Что делаем:** Заменяем `ArgentaFormatter` на `structlog` с JSON processor:
  - Настраиваем `structlog.processors.JSONRenderer()` как дефолтный formatter
  - Каждый log entry содержит: `timestamp`, `level`, `module`, `correlation_id`, `message`
  - Убираем ручное форматирование строк вида `f"..."` — заменяем на structured fields
  - Пример: `logger.info("memory_stored", namespace="user_facts", entity_id=123, duration_ms=45)`
- **Зависимости:** —
- **Время:** 3-4 часа
- **Риск:** Средний. Много файлов. Делаем постепенно, модуль за модулем. Сначала core (service, repository), потом tools
- **Ожидаемый эффект:** Машиночитаемые логи, фильтрация по полям в Grafana/Loki, корреляция между сервисами

---

## Фаза 3: Архитектура, надёжность и Observability (P2) — 7-9 дней

Структурные улучшения + observability-панели и трейсинг. Требуют координации Эна + Сона + Мая.

### Шаг 3.1: Circuit Breaker для EmbeddingClient

- **Файлы:** `memory_server/embedding/client.py`, `requirements.txt` (добавить `circuitbreaker`)
- **Что делаем:** Оборачиваем EmbeddingClient в Circuit Breaker. При 5+ ошибках подряд — opening state, 30s timeout → half-open. Используем библиотеку `circuitbreaker`. Обрабатываем `CircuitBreakerError` в service layer — возвращаем graceful degradation (без эмбеддинга)
- **Зависимости:** —
- **Время:** 2 часа
- **Риск:** Средний. Неправильный fallback может сломать store. Тестировать с mock
- **Ожидаемый эффект:** Embedding API падение не валит весь сервис

### Шаг 3.2: Circuit Breaker для QdrantClient

- **Файлы:** `memory_server/memory/repository_qdrant.py`, `requirements.txt`
- **Что делаем:** Аналогично шагу 3.1 для Qdrant. Qdrant используется для векторного поиска, его падение не должно валить основной PG-функционал
- **Зависимости:** шаг 3.1 (паттерн уже будет готов)
- **Время:** 1.5 часа
- **Риск:** Средний
- **Ожидаемый эффект:** Qdrant падение не влияет на PG-операции

### Шаг 3.3: Исправить Race Condition в EmbeddingClient._get_client()

- **Файлы:** `memory_server/embedding/client.py`
- **Что делаем:** `_get_client()` пересоздаёт клиент в каждом вызове из-за отсутствия lock. Добавляем `asyncio.Lock()` для инициализации. Паттерн: double-checked locking с lock
- **Зависимости:** —
- **Время:** 45 мин
- **Риск:** Низкий
- **Ожидаемый эффект:** Один клиент на все вызовы, нет гонок

### Шаг 3.4: _get_service() — кеширование в Celery tasks

- **Файлы:** `memory_server/memory/service.py` (или где определён `_get_service`)
- **Что делаем:** `_get_service()` пересоздаёт объекты в каждой Celery task. Добавляем кеш (например, через `functools.lru_cache` или module-level переменную с TTL). Объекты stateless — безопасно переиспользовать
- **Зависимости:** —
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Меньше аллокаций, быстрее cold start воркера

### Шаг 3.5: NamespaceRepository._cache с TTL

- **Файлы:** `memory_server/memory/repository.py` (или где _cache)
- **Что делаем:** Кеш без TTL — stale данные навсегда. Заменяем на `cachetools.TTLCache(maxsize=128, ttl=300)`. TTL 5 минут — баланс между актуальностью и производительностью
- **Зависимости:** —
- **Время:** 45 мин
- **Риск:** Низкий
- **Ожидаемый эффект:** Кеш автоматически обновляется, нет stale данных

### Шаг 3.6: Protocol-based interfaces для Repository

- **Файлы:** `memory_server/memory/repository.py` (новый файл `interfaces.py`), `memory_server/memory/repository.py`
- **Что делаем:** Выносим интерфейсы в `interfaces.py`: `MemoryRepositoryProtocol`, `EmbeddingClientProtocol`. Repository и Client реализуют эти протоколы. DI через constructor в lifespan
- **Зависимости:** —
- **Время:** 2 часа
- **Риск:** Низкий. Механический рефакторинг
- **Ожидаемый эффект:** Легче тестировать (mock по протоколу), чёткие границы слоёв

### Шаг 3.7: Composition Root в lifespan

- **Файлы:** `memory_server/server.py`, `memory_server/connections.py`
- **Что делаем:** Глобальные singletons в `connections.py` заменяем на creation в `lifespan` context manager. Все зависимости создаются один раз и передаются через `request_context.lifespan_context`. Убираем глобальные переменные
- **Зависимости:** шаг 3.6 (интерфейсы нужны для DI)
- **Время:** 2-3 часа
- **Риск:** Высокий. Меняет паттерн инициализации всего приложения. Тестировать все 17 tools
- **Ожидаемый эффект:** Тестируемость, нет скрытых зависимостей, предсказуемый lifecycle

### Шаг 3.8: Grafana dashboards — selti.json с RED панелями

- **Файлы:** `grafana/dashboards/selti.json` (новый), `docker-compose.yml` (добавить volume для dashboards)
- **Что делаем:** Создаём Grafana dashboard с RED панелями (Rate, Errors, Duration):
  - **Rate:** `rate(http_requests_total[5m])` по endpoint'ам
  - **Errors:** `rate(http_requests_total{status=~"5.."}[5m])` + error ratio
  - **Duration:** `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))` по endpoint'ам
  - Дополнительно: Qdrant ops rate, Celery task success/fail rate, dedup ratio
  - Импорт через provisioning или JSON import
- **Зависимости:** шаг 1.4 (PROMETHEUS_MULTIPROC_DIR обязателен)
- **Время:** 2-3 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** Визуализация здоровья системы, быстрый поиск узких мест

### Шаг 3.9: Business metrics — dedup ratio, memory growth rate, search quality

- **Файлы:** `memory_server/metrics.py`, `memory_server/memory/dedup.py`, `memory_server/memory/service.py`
- **Что делаем:** Добавляем business-метрики в Prometheus:
  - `selti_dedup_ratio` (gauge) — отношение дедуплицированных к общим записям за период
  - `selti_memory_growth_rate` (gauge) — скорость роста записей в namespace (записи/час)
  - `selti_search_quality` (histogram) — количество результатов поиска по tool (label `tool`)
  - `selti_search_results_per_tool` (histogram) — распределение числа результатов по tool
  - Экспортируем из dedup.py и service.py при соответствующих операциях
- **Зависимости:** шаг 1.5 (метрики только в dedup.py)
- **Время:** 2 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** Бизнес-понимание использования системы, аномалии в consumption patterns

### Шаг 3.10: OpenTelemetry — distributed tracing

- **Файлы:** `requirements.txt` (добавить `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`), `memory_server/server.py`, `memory_server/memory/service.py`, `docker-compose.yml` (добавить OTLP collector)
- **Что делаем:** Интегрируем OpenTelemetry для distributed tracing:
  - Настраиваем `TracerProvider` с OTLP exporter (endpoint — локальный collector или Jaeger)
  - Инструментируем ключевые span'ы: MCP-запрос → service → repository → PG/Qdrant
  - Автоматическая инструментация для `asyncpg`, `httpx`, `celery` через `opentelemetry-instrument`
  - Каждый span содержит: `correlation_id`, `namespace`, `operation`, `duration_ms`
- **Зависимости:** шаг 2.5 (correlation ID нужен для span attributes)
- **Время:** 3-4 часа
- **Риск:** Средний. Overhead tracing ~1-2% на latency. Тестировать в staging
- **Ожидаемый эффект:** Визуализация цепочки вызовов, поиск bottleneck'ов в распределённой системе

### Шаг 3.11: DB pool real-time metrics

- **Файлы:** `memory_server/metrics.py`, `memory_server/connections.py` (или pool init)
- **Что делаем:** Экспортируем метрики DB pool в реальном времени:
  - `selti_db_pool_size` (gauge) — текущий размер пула
  - `selti_db_pool_available` (gauge) — доступные соединения
  - `selti_db_pool_used` (gauge) — используемые соединения
  - `selti_db_pool_waiting` (gauge) — запросы в очереди на соединение
  - Периодическое обновление через `asyncio.create_task` с интервалом 5s
- **Зависимости:** —
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Мониторинг нагрузки на PG, алерты на exhaustion пула

### Шаг 3.12: Search results per tool — label tool

- **Файлы:** `memory_server/tools/memory_tools.py`, `memory_server/metrics.py`
- **Что делаем:** Добавляем label `tool` к метрикам поиска:
  - `selti_search_duration_seconds{tool="memory_search"}` — время поиска по tool
  - `selti_search_results_count{tool="memory_search"}` — количество результатов по tool
  - Инструментируем каждый tool (memory_search, memory_find_similar, memory_list, memory_traverse)
- **Зависимости:** —
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Понимание какие tools нагружены больше, оптимизация частых путей

---

## Фаза 4: Качество кода и инфраструктура (P3) — 3-4 дня

Долгосрочные улучшения. Менее критичны, но важны для масштабирования.

### Шаг 4.1: Refactor repository_qdrant.py (SRP)

- **Файлы:** `memory_server/memory/repository_qdrant.py`
- **Что делаем:** 745 строк, PG + Qdrant в одном классе. Разделяем:
  - `QdrantVectorStore` — только векторные операции
  - `PGMemoryRepository` — только PG операции (уже есть)
  - `MemoryRepository` — фасад, координирует оба
- **Зависимости:** шаг 3.6 (интерфейсы)
- **Время:** 3-4 часа
- **Риск:** Средний. Рефакторинг большого файла. Покрыть тестами
- **Ожидаемый эффект:** SRP, проще тестировать каждый компонент отдельно

### Шаг 4.2: Factory для 17 MCP tools

- **Файлы:** `memory_server/tools/memory_tools.py`
- **Что делаем:** Все 17 tools повторяют паттерн try/catch + celery_call. Создаём decorator `@tool_handler` или factory function, которая оборачивает общую логику. Каждый tool — только бизнес-логика
- **Зависимости:** —
- **Время:** 2-3 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** -50% кода в memory_tools.py, единый паттерн обработки ошибок

### Шаг 4.3: Correlation IDs для MCP requests

- **Файлы:** `memory_server/server.py`, `memory_server/tools/task_bridge.py`
- **Что делаем:** Каждый MCP-запрос получает `correlation_id` через `contextvars`. Пробрасывается через все слои (server → tools → task_bridge → celery). Добавляем `correlation_id` в заголовок ответа клиенту
- **Зависимости:** шаг 2.5 (Celery correlation ID уже будет готов)
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** End-to-end трейсинг от клиента до воркера и обратно

### Шаг 4.4: Logging config + Docker log rotation

- **Файлы:** `memory_server/logging_config.py`, `docker-compose.yml`, `Dockerfile`
- **Что делаем:**
  - Python: `RotatingFileHandler` с maxBytes=10MB, backupCount=5 (если локально)
  - Docker: в `docker-compose.yml` добавляем `logging` driver с `max-size: "10m"` и `max-file: "3"` для каждого сервиса
  - Structured формат уже настроен в шаге 2.6
- **Зависимости:** шаг 2.6 (structlog)
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Логи не раздувают диск, автоматическая ротация в Docker

### Шаг 4.5: Тесты для изменений P0-P2

- **Файлы:** `tests/` (расширение)
- **Что делаем:** Покрытие тестами всех изменений из фаз 1-3: хранимки, circuit breaker, DI, window functions, soft delete, structured logging, correlation ID
- **Зависимости:** фазы 1-3
- **Время:** 4-6 часов
- **Риск:** Низкий
- **Ожидаемый эффект:** Уверенность в изменениях, нет регрессий

---

## Сводная таблица

| Фаза | Задач | Время | Сложность | Эффект |
|------|-------|-------|-----------|--------|
| **P0** | Quick Wins (1.1-1.7) | 2-3 дня | низкая-средняя | + traverse 27x-200x, метрики работают, 19 алертов активны |
| **P1** | БД + Observability (2.1-2.6) | 4-5 дней | средняя | + list 2x, forget не ломает, correlation ID, structured logs |
| **P2** | Архитектура + Observability (3.1-3.12) | 7-9 дней | средняя-высокая | + надёжность, Grafana dashboards, business metrics, OTel tracing |
| **P3** | Качество (4.1-4.5) | 3-4 дня | средняя | + SRP, -50% кода tools, log rotation, тесты |
| **Итого** | | **16-21 день** | | |

---

## Критический путь

```
1.1 (хранимки) → 2.4 (copy_records) → 4.1 (refactor qdrant)
     ↓
1.2 (дедуп insert) — сразу после 1.1
     ↓
1.4 (prometheus MULTIPROC) → 3.8 (grafana dashboards)
     ↓
1.5 (дедуп метрик) → 3.9 (business metrics)
     ↓
2.5 (correlation ID celery) → 3.10 (OpenTelemetry)
     ↓
2.6 (structured logging) → 4.4 (log rotation)
     ↓
3.6 (интерфейсы) → 3.7 (DI) → 4.1 (refactor)
```

Независимые задачи (можно параллелить с любыми):
- 1.3 (health check + метрики)
- 1.6 (.dockerignore)
- 1.7 (CPU limits)
- 2.1 (get_relations)
- 2.2 (list_with_count)
- 2.3 (soft delete)
- 3.1-3.5 (circuit breaker, lock, cache)
- 3.11 (DB pool metrics)
- 3.12 (search results per tool)
- 4.2 (factory tools)
- 4.3 (correlation ID MCP)

---

## Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Хранимки из 009 не совместимы с текущим кодом | низкая | высокое | Проверить сигнатуры перед подключением |
| Circuit Breaker ломает fallback | средняя | высокое | Тестировать с mock, graceful degradation |
| copy_records_to_table не работает с asyncpg | низкая | среднее | Альтернатива: executemany |
| Composition Root ломает все tools | средняя | высокое | Пошаговая миграция, тесты на каждом шаге |
| OpenTelemetry overhead на latency | низкая | среднее | Тестировать в staging, выключить если >2% |
| Structured logging ломает парсинг логов | низкая | среднее | Миграция по модулям, тесты на каждый модуль |

---

## Порядок PR

1. **PR #1 (P0):** Шаги 1.1-1.7 — quick wins, один коммит
2. **PR #2 (P1):** Шаги 2.1-2.4 — БД оптимизации
3. **PR #3 (P1):** Шаги 2.5-2.6 — Correlation ID + Structured logging
4. **PR #4 (P2):** Шаги 3.1-3.7 — архитектура (можно разбить на 2-3 PR)
5. **PR #5 (P2):** Шаги 3.8-3.10 — Grafana + Business metrics + OTel
6. **PR #6 (P2):** Шаги 3.11-3.12 — DB pool metrics + Search per tool
7. **PR #7 (P3):** Шаги 4.1-4.5 — качество кода
