# План перехода selti на Celery — v3 (production-ready)

**Дата:** 31.07.2026
**Проект:** selti — Python MCP-сервер семантической памяти
**Статус:** Готов к реализации
**Авторы:** Момо (plan), Сона (code), Эна (architect), Нора (DB), Мая (observability), Катерина (testing)

---

## Что изменилось с v2

| | v2 | v3 |
|---|---|---|
| Grafana | Вcluded | **Убран** (отдельным сервисом потом) |
| Алерты | 10 | **7** (фокус на критичных) |
| Worker config | Базовый | **Production**: graceful shutdown, memory limits, prefetch |
| Timeouts | 300s везде | **Per task type**: memory=300, batch=900, hash=180 |
| Retry | Базовый | **Exponential backoff + jitter**, max 5 retries |
| Docker | Стандартный | **Optimized**: `--without-gossip --without-mingle`, memory limits |
| Логирование | Упомянуто | **Полная реализация** по стандарту Argenta |
| Метрики | 6 | **7** (добавлен task_latency для queue wait) |

---

## Критическая проблема: sync/async mismatch

**Весь код selti — async.** Celery prefork workers — sync.

```
tool (async) → service (async) → asyncpg (async) → PG
                            → httpx (async) → Embedding API
                            → QdrantClient (sync) → Qdrant
```

### Решение: AsyncBridge

```python
# memory_server/tasks/async_bridge.py
import asyncio

def run_async(coro_func, *args, **kwargs):
    """Запустить async функцию в sync контексте Celery worker."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_func(*args, **kwargs))
    finally:
        loop.close()
```

**Почему НЕ eventlet/gevent:** asyncpg + green threads = конфликт (uvloop vs monkey-patch). `soft_time_limit` не работает в gevent.

---

## Архитектура после миграции

```
┌─────────────────────────────────────┐
│  MCP Server (FastMCP + uvicorn)     │
│  - auth middleware                   │
│  - metrics middleware                │
│  - tools: send_task → Celery        │
│  - НЕТ MemoryService                │
│  - НЕТ asyncpg pool                 │
└──────────────┬──────────────────────┘
               │ send_task()
        ┌──────▼──────┐
        │ Redis broker │
        │ (db=0)       │
        └──────┬──────┘
               │
┌──────────────▼──────────────────────┐
│  Celery Worker (prefork, 1 тип)     │
│  - AsyncBridge (run_async)           │
│  - MemoryService (per worker)        │
│  - asyncpg pool (per worker process) │
│  - EmbeddingClient (lazy init)       │
│  - QdrantClient (sync, per process)  │
└──────┬──────┬──────┬────────────────┘
       │      │      │
  ┌────▼─┐ ┌─▼──┐ ┌─▼───────┐
  │ PG   │ │Qdr │ │Embed API│
  └──────┘ └────┘ └─────────┘
```

---

## Фаза 0: Инфраструктура Celery (1.5 дня)

### Кто что делает

| Агент | Файлы | Задачи | Срок |
|---|---|---|---|
| **Рэй** | `requirements.txt`, `Dockerfile`, `.env.example` | Добавить зависимости celery/flower/structlog; обновить COPY-инструкции; добавить Celery env vars | 0.5 дня |
| **Эна** | `memory_server/config.py`, `memory_server/celery_app.py` | Добавить Celery settings в config; создать Celery instance с production настройками (routing, retry, serialization) | 1 день |
| **Сона** | `memory_server/tasks/__init__.py`, `memory_server/tasks/async_bridge.py` | Создать пакет tasks; реализовать `run_async()` для sync→async моста | 0.5 дня |
| **Нора** | `memory_server/tasks/connections.py` | Worker-scoped singleton: asyncpg pool (min=2, max=4), QdrantClient, EmbeddingClient. Сигналы `worker_process_init`/`worker_process_shutdown` | 0.5 дня |
| **Афина** | — | Оркестрация, контроль сроков, согласование с Милордом | — |

### Зависимости
- Сона ждёт пока Эна сделает `celery_app.py` (нужен импорт `app`)
- Нора ждёт пока Сона сделает `async_bridge.py` (нужен `run_async` для pool init)
- Рэй может работать параллельно с Эной (requirements не зависит от кода)

### Точки согласования
- [ ] День 0 (вечер): Рэй → Афина: `requirements.txt` обновлён, `pip install` проходит
- [ ] День 1 (утро): Эна → Афина: `celery_app.py` готов, `celery inspect ping` отвечает
- [ ] День 1 (обед): Сона + Нора → Эна: `async_bridge.py` + `connections.py` готовы, worker стартует с pool

### Проверка
- [ ] `celery -A memory_server.celery_app worker -l INFO` стартует
- [ ] `celery -A memory_server.celery_app inspect ping` отвечает
- [ ] Connection pool создаётся при старте worker

---

## Фаза 1: Tasks Infrastructure (2 дня)

### Кто что делает

| Агент | Файлы | Задачи | Срок |
|---|---|---|---|
| **Сона** | `memory_server/tasks/base.py`, `memory_server/tasks/serializers.py`, `memory_server/tasks/errors.py`, `memory_server/tasks/memory_tasks.py`, `memory_server/tasks/hash_tasks.py`, `memory_server/tasks/logging_config.py` | Базовый класс `SeltiTask` (retry, timeout, logging, metrics); JSON-сериализатор Pydantic/datetime/UUID; кастомные исключения; 15 memory tasks + 4 hash tasks; Argenta formatter для workers | 1.5 дня |
| **Нора** | `memory_server/tasks/connections.py` (доработка) | Pool sizing: min=2, max=4 на процесс (7×4=28 соединений). Добавить lazy init для EmbeddingClient | 0.5 дня |
| **Эна** | — (code review) | Ревью base.py, memory_tasks.py, connections.py. Проверка архитектурной корректности | 0.5 дня |
| **Афина** | — | Контроль сроков, контроль качества | — |

### Зависимости
- Сона ждёт Фазу 0 (нужны `celery_app.py`, `async_bridge.py`, `connections.py`)
- Эна ждёт Сону для code review
- Нора дорабатывает connections.py после получения feedback от Соны по usage

### Точки согласования
- [ ] День 2 (вечер): Сона → Эна: `base.py` + `memory_tasks.py` готовы, отправлены на review
- [ ] День 3 (утро): Эна → Сона: review пройден, замечания (если есть) исправлены
- [ ] День 3 (обед): Сона → Афина: все tasks зарегистрированы, `inspect registered` показывает 19 задач

### Проверка
- [ ] `celery -A memory_server.celery_app inspect registered` показывает все задачи
- [ ] Задачи маршрутизируются в правильные очереди
- [ ] Connection pool создаётся при старте worker

---

## Фаза 2: Миграция MCP Tools (3 дня)

### Кто что делает

| Агент | Файлы | Задачи | Срок |
|---|---|---|---|
| **Сона** | `memory_server/tools/task_bridge.py`, `memory_server/tools/memory_tools.py`, `memory_server/tools/hash_tools.py` | Создать `run_task()` мост (send_task + asyncio.to_thread); переделать 15 memory tools на `run_task('memory.*')`; переделать 4 hash tools на `run_task('hash.*')`. ACL проверка остаётся на уровне tool | 2 дня |
| **Эна** | `memory_server/server.py` | Убрать MemoryService, EmbeddingClient, QdrantClient из lifespan. Оставить run_migrations() + health-check pool. Code review task_bridge | 1 день |
| **Катерина** | — (smoke test) | Ручная проверка: memory_store → Celery → PG, memory_search → Celery → результат | 0.5 дня |
| **Афина** | — | Контроль, финальное согласование перед тестами | — |

### Зависимости
- Сона ждёт Фазу 1 (нужны tasks в `inspect registered`)
- Эна ждёт Сону (task_bridge должен работать, чтобы можно было тестировать server.py)
- Катерина ждёт оба: Сону (tools работают) + Эну (server.py обновлён)

### Точки согласования
- [ ] День 4 (вечер): Сона → Эна: task_bridge + memory_tools готовы, `memory_store` через Celery работает
- [ ] День 5 (обед): Эна → Сона: server.py обновлён, lifespan упрощён
- [ ] День 5 (вечер): Катерина → Афина: smoke test пройден, все 22 MCP tools работают через Celery
- [ ] День 6: Афина → Милорд: Фаза 2 завершена, переход к тестам

### Проверка
- [ ] `memory_store` сохраняет через Celery → запись в БД
- [ ] `memory_search` ищет через Celery → результат возвращается
- [ ] Все 22 MCP tools работают через Celery
- [ ] Таймауты работают корректно
- [ ] Retry при ошибках работает

---

## Фаза 3: Тесты (2 дня)

### Кто что делает

| Агент | Файлы | Задачи | Срок |
|---|---|---|---|
| **Катерина** | `tests/conftest.py`, `tests/test_celery_tasks.py`, `tests/test_task_bridge.py`, `tests/test_serializers.py`, `tests/test_tools.py` | Добавить Celery fixtures (`task_always_eager=True`); тесты happy path/error/retry для каждой задачи (≥80%); тесты sync→async моста + timeout; тесты JSON serialization Pydantic/datetime/UUID; обновить mock в test_tools.py (`task_bridge.run_task` вместо service) | 1.5 дня |
| **Сона** | — (поддержка) | Консультации по implementation details, исправление багов в tasks/tools по результатам тестов | 0.5 дня |
| **Афина** | — | Контроль покрытия, контроль regression | — |

### Зависимости
- Катерина ждёт Фазу 2 (все tasks и tools должны работать через Celery)
- Сона ждёт Катерину (баг-репорты → фиксы)

### Точки согласования
- [ ] День 7 (вечер): Катерина → Афина: conftest + test_celery_tasks готовы
- [ ] День 8 (обед): Катерина → Афина: все тесты проходят, покрытие ≥80%
- [ ] День 8 (вечер): Сона → Катерина: баги исправлены, regression test пройден
- [ ] День 8 (вечер): Катерина → Афина: финальный `pytest tests/ -v` — всё зелёное

### Проверка
- [ ] `pytest tests/ -v` — все тесты проходят
- [ ] `pytest tests/test_celery_tasks.py -v` — новые тесты проходят
- [ ] Покрытие ≥80% для tasks
- [ ] Regression: все 16 тестовых файлов проходят (10 не требуют изменений)

---

## Фаза 4: Мониторинг + FastAPI (2 дня) — ✅ ЧАСТИЧНО ВЫПОЛНЕНА

### Кто что делает

| Агент | Файлы | Задачи | Срок | Статус |
|---|---|---|---|---|
| **Мая** | `memory_server/metrics.py`, `memory_server/tasks/signals.py`, `memory_server/tasks/__init__.py`, `monitoring/alerts/prometheus-rules.yml` | Добавить 8 Celery метрик + Redis ops + Qdrant ops + app метрики; создать signals.py (prerun/postrun/failure/retry → метрики); добавить 11 алертов (7 Celery + 3 Selti app + 1 Redis) | 1.5 дня | ✅ ВЫПОЛНЕНО |
| **Мая** | `memory_server/cache/redis_client.py`, `memory_server/vector/qdrant_store.py`, `memory_server/embedding/client.py`, `memory_server/tools/memory_tools.py`, `memory_server/memory/repository_qdrant.py` | Добавить Redis ops метрики, Qdrant ops метрики, EMBEDDING_DURATION, SEARCH_RESULTS, MEMORY_COUNT, DEDUP_SKIPPED/INSERTED | — | ✅ ВЫПОЛНЕНО |
| **Рэй** | `docker-compose.yml`, `memory_server/__main__.py` | Добавить services: celery-worker (production flags, healthcheck, memory limits) + flower (dev profile); добавить `check_celery_health()` в `/health` endpoint | 1 день | ⏳ ОЖИДАНИЕ |
| **Сона** | `memory_server/api/tasks.py` | Создать REST endpoints: `GET /tasks/{task_id}` (AsyncResult), `GET /tasks` (inspect active), `POST /tasks/{task_id}/cancel` (revoke). `celery.control.inspect()` через `asyncio.to_thread()` | 0.5 дня | ⏳ ОЖИДАНИЕ |
| **Сона** | `memory_server/tasks/worker_stats.py` | НОВЫЙ: Периодический сбор worker stats (workers_active, queue_length). Beat schedule: каждые 30с. | 0.5 дня | ⏳ НОВАЯ ЗАДАЧА |
| **Сона** | `memory_server/tools/memory_tools.py`, `memory_server/tools/hash_tools.py` | Рефакторинг: вынести `_track_tool` в общий декоратор `memory_server/utils/metrics_decorator.py` | 0.5 дня | ⏳ НОВАЯ ЗАДАЧА |
| **Афина** | — | Контроль, проверка метрик в dev | — | ⏳ ОЖИДАНИЕ |

### Зависимости
- Мая выполнила свою часть (метрики + алерты + signals.py)
- Рэй ждёт Фазу 0 (Dockerfile обновлён) + Фазу 2 (server.py готов)
- Сона ждёт Фазу 2 (task_bridge работает, можно делать endpoints)
- Сона может начать worker_stats.py и metrics_decorator параллельно с Фазой 2

### Точки согласования
- [x] ~~День 9 (вечер): Мая → Афина: метрики в `/metrics` видны~~ ✅
- [ ] День 9 (вечер): Рэй → Афина: docker-compose.yml с worker + flower, `docker compose up` работает
- [ ] День 10 (обед): Сона → Мая: api/tasks.py готов, можно тестировать через endpoints
- [ ] День 10 (вечер): Сона → Афина: worker_stats.py готов, метрики workers_active/queue_length обновляются
- [ ] День 10 (вечер): Афина → Милорд: Фаза 4 завершена

### Проверка
- [ ] `/metrics` содержит `athena_celery_*` метрики (✅ Мая)
- [ ] `/metrics` содержит Redis ops метрики (✅ Мая)
- [ ] `/metrics` содержит Qdrant ops метрики (✅ Мая)
- [ ] `/metrics` содержит обновлённые app метрики (✅ Мая)
- [ ] `/health` содержит celery check
- [ ] `GET /tasks` возвращает список активных задач
- [ ] Алерты срабатывают при тестовой ошибке (✅ Мая)
- [ ] `workers_active` и `queue_length` обновляются автоматически (⏳ worker_stats.py)

### Замечания Мая (требуют доработки)

1. **repository_qdrant.py — реальный потребитель Qdrant**
   - `QdrantVectorStore` не используется в production
   - Метрики добавлены в `repository_qdrant.py` — это правильно
   - **Действие:** Убедиться что `qdrant_store.py` метрики не конфликтуют с `repository_qdrant.py`

2. **Periodic worker stats**
   - `CELERY_WORKERS_ACTIVE` и `CELERY_QUEUE_LENGTH` требуют periodic обновления
   - **Действие:** Создать `worker_stats.py` с Beat schedule (каждые 30с)

3. **Дублирование `_track_tool`**
   - Одинаковый код в `memory_tools.py` и `hash_tools.py`
   - **Действие:** Вынести в общий декоратор `memory_server/utils/metrics_decorator.py`

4. **Полный путь метрик**
   - Все метрики покрывают: MCP tool → Redis cache → Embedding API → Qdrant → PostgreSQL
   - **Действие:** Проверить что нет разрывов в цепочке

---

## Фаза 5: Оптимизация и деплой (1.5 дня)

### Кто что делает

| Агент | Файлы | Задачи | Срок |
|---|---|---|---|
| **Катерина** | — | Benchmark: замерить latency MCP tool → Celery task → результат. Цель: P95 < 2s для memory_ops | 0.5 дня |
| **Сона** | `memory_server/tools/memory_tools.py`, `memory_server/tools/hash_tools.py`, `memory_server/server.py` | Cleanup legacy: убрать прямые вызовы service из tools (если остались); убрать MemoryService из server.py lifespan (если ещё не убран) | 0.5 дня |
| **Тиамат** | `README.md`, `.env.example` | Документация: Celery конфигурация, env vars, troubleshooting. Обновить README секцией "Celery Setup" | 0.5 дня |
| **Рэй** | `docker-compose.yml` | Деплой: `docker compose up -d` с новыми сервисами. Проверка healthcheck'ей | 0.5 дня |
| **Афина** | — | Финальное согласование с Милордом, контроль деплоя | — |

### Зависимости
- Катерина ждёт Фазу 3 (тесты пройдены, можно бенчмаркать)
- Сона ждёт Фазу 3 (знает legacy cleanup не сломает тесты)
- Тиамат ждёт Фазу 4 (нужно описать все endpoints и метрики)
- Рэй ждёт Фазу 4 (docker-compose финализирован)
- Деплой — последний, все ждут остальных

### Точки согласования
- [ ] День 11 (утро): Катерина → Афина: benchmark готов, P95 < 2s достигнут
- [ ] День 11 (обед): Сона + Тиамат → Афина: cleanup + документация готовы
- [ ] День 11 (вечер): Рэй → Афина: деплой выполнен, все сервисы зелёные
- [ ] День 11 (вечер): Афина → Милорд: миграция завершена, всё работает

### Проверка
- [ ] Все тесты проходят
- [ ] Latency P95 < 2s для memory_ops
- [ ] Документация актуальна
- [ ] Docker services стартуют, healthcheck'и зелёные
- [ ] Нет regressions в существующем функционале

---

## Критический путь

```
Фаза 0 → Фаза 1 → Фаза 2 → Фаза 3 → Фаза 5
                        ↓
                     Фаза 4 (параллельно с Фазой 2)
```

**Изменения после отчёта Мая:**
- Фаза 4 частично разблокирована: метрики и алерты готовы
- Остались: docker-compose (Рэй), api/tasks.py (Сона), worker_stats.py (Сона)
- Критический путь не изменился — Фаза 4 по-прежнему параллельна с Фазой 2

---

## Распределение нагрузки

| Агент | Фазы | Нагрузка |
|---|---|---|
| **Афина** | 0–5 | Контроль всех фаз |
| **Сона** | 0, 1, 2, 3, 4, 5 | Основной исполнитель + worker_stats + metrics_decorator |
| **Рэй** | 0, 4, 5 | Инфраструктура |
| **Эна** | 0, 1, 2 | Архитектура, ревью |
| **Катерина** | 2, 3, 5 | Тестирование |
| **Мая** | 4 | ✅ Метрики + алерты + signals (ВЫПОЛНЕНО) |
| **Нора** | 0, 1 | DB pool management |
| **Тиамат** | 5 | Документация |
| **Момо** | — | План (этот документ) |

---

## Матрица ответственности за файлы

| Файл | Ответственный | Фаза | Статус |
|---|---|---|---|
| `requirements.txt` | Рэй | 0 | ⏳ |
| `Dockerfile` | Рэй | 0 | ⏳ |
| `.env.example` | Рэй (0), Тиамат (5) | 0, 5 | ⏳ |
| `memory_server/config.py` | Эна | 0 | ⏳ |
| `memory_server/celery_app.py` | Эна | 0 | ⏳ |
| `memory_server/tasks/__init__.py` | Сона (0), Мая (4) | 0, 4 | ✅ (Мая) |
| `memory_server/tasks/async_bridge.py` | Сона | 0 | ⏳ |
| `memory_server/tasks/connections.py` | Нора | 0, 1 | ⏳ |
| `memory_server/tasks/base.py` | Сона | 1 | ⏳ |
| `memory_server/tasks/serializers.py` | Сона | 1 | ⏳ |
| `memory_server/tasks/errors.py` | Сона | 1 | ⏳ |
| `memory_server/tasks/memory_tasks.py` | Сона | 1 | ⏳ |
| `memory_server/tasks/hash_tasks.py` | Сона | 1 | ⏳ |
| `memory_server/tasks/logging_config.py` | Сона | 1 | ⏳ |
| `memory_server/tasks/signals.py` | Мая | 4 | ✅ |
| `memory_server/tasks/worker_stats.py` | Сона | 4 | ⏳ НОВЫЙ |
| `memory_server/tools/task_bridge.py` | Сона | 2 | ⏳ |
| `memory_server/tools/memory_tools.py` | Сона (2), Сона (4) | 2, 4 | ⏳ |
| `memory_server/tools/hash_tools.py` | Сона (2), Сона (4) | 2, 4 | ⏳ |
| `memory_server/utils/metrics_decorator.py` | Сона | 4 | ⏳ НОВЫЙ |
| `memory_server/server.py` | Эна | 2, 5 | ⏳ |
| `tests/conftest.py` | Катерина | 3 | ⏳ |
| `tests/test_celery_tasks.py` | Катерина | 3 | ⏳ |
| `tests/test_task_bridge.py` | Катерина | 3 | ⏳ |
| `tests/test_serializers.py` | Катерина | 3 | ⏳ |
| `tests/test_tools.py` | Катерина | 3 | ⏳ |
| `memory_server/metrics.py` | Мая | 4 | ✅ |
| `memory_server/cache/redis_client.py` | Мая | 4 | ✅ |
| `memory_server/vector/qdrant_store.py` | Мая | 4 | ✅ |
| `memory_server/embedding/client.py` | Мая | 4 | ✅ |
| `memory_server/memory/repository_qdrant.py` | Мая | 4 | ✅ |
| `monitoring/alerts/prometheus-rules.yml` | Мая | 4 | ✅ |
| `docker-compose.yml` | Рэй | 4 | ⏳ |
| `memory_server/__main__.py` | Рэй | 4 | ⏳ |
| `memory_server/api/tasks.py` | Сона | 4 | ⏳ |
| `README.md` | Тиамат | 5 | ⏳ |

**Принцип:** один файл = один основной исполнитель. Исключения:
- `.env.example`: Рэй (добавляет переменные в Фазе 0), Тиамат (документирует в Фазе 5) — не конфликтуют
- `memory_server/server.py`: Эна (архитектурные изменения в Фазе 2), Сона (legacy cleanup в Фазе 5) — последовательно
- `memory_server/tools/*.py`: Сона (миграция в Фазе 2, cleanup в Фазе 5) — один исполнитель
- `memory_server/tasks/__init__.py`: Сона (Фаза 0), Мая (Фаза 4) — не конфликтуют (разные части)
- `memory_server/tools/memory_tools.py`, `hash_tools.py`: Сона (Фаза 2), Сона (Фазе 4 — рефакторинг) — один исполнитель

---

## Production Best Practices (из research)

### Worker Optimization
| Параметр | Значение | Почему |
|---|---|---|
| `worker_prefetch_multiplier=1` | prefetch = concurrency | Fair scheduling, нет starved tasks |
| `task_acks_late=True` | ACK после выполнения | Не теряем задачи при crash |
| `task_reject_on_worker_lost=True` | Re-queue при crash | Автовосстановление |
| `worker_max_tasks_per_child=1000` | Recycling | Memory leak protection |
| `worker_max_memory_per_child=200000` | 200MB limit | OOM protection |
| `worker_soft_shutdown_timeout=60` | Graceful shutdown | Завершаем текущие задачи |
| `--without-gossip --without-mingle` | Отключить overhead | Ускоряет старт на 30-50% |

### Timeouts per Task Type
| Тип | soft_time_limit | time_limit | Причина |
|---|---|---|---|
| memory_ops (store/get/update/delete) | 240s | 300s | Embedding + PG write |
| memory_ops (search/traverse) | 240s | 300s | Embedding + Qdrant search |
| batch_ops (ingest_batch) | 600s | 900s | Большие батчи |
| hash_ops | 120s | 180s | Простые CRUD |

### Retry Strategy
```python
@shared_task(
    bind=True,
    max_retries=5,
    retry_backoff=True,        # exponential backoff
    retry_backoff_max=60,      # cap 60s
    retry_jitter=True,         # +random jitter
    default_retry_delay=30,
)
def store_memory(self, ...):
    try:
        return run_async(service.store, ...)
    except ConnectionError as exc:
        raise self.retry(exc=exc)
    except ValueError as exc:
        # Validation error — не retry
        raise
```

### Docker Production Flags
```bash
# Без gossip/mingle/heartbeat — экономит ~30-50% overhead
celery -A memory_server.celery_app worker \
    -Q memory,batch \
    -c 4 \
    --without-gossip \
    --without-mingle \
    --without-heartbeat \
    --max-tasks-per-child=1000 \
    --max-memory-per-child=200000
```

---

## Логирование по стандарту Argenta

### Формат
```
[2026-07-31T12:00:00.000Z] [INFO] [selti-worker] memory.store: ok {"task_id": "abc-123", "duration_ms": 142.3}
```

### Правила
- **Формат:** `[ISO8601-UTC] [LEVEL] [SERVICE_NAME] message {"json": "meta"}`
- **Уровни:** DEBUG → INFO → **WARN** (не WARNING!) → ERROR
- **SERVICE_NAME:** `selti-worker` (для workers), `selti` (для сервера)
- **Correlation:** `task_id` через structlog contextvars
- **JSON-мета:** только нужные ключи (б секретов)

### Реализация
```python
# memory_server/tasks/logging_config.py
import logging
import os
import json
from datetime import datetime, timezone

SERVICE_NAME = os.environ.get("SERVICE_NAME", "selti-worker")

class ArgentaFormatter(logging.Formatter):
    """Формат: [ISO8601] [LEVEL] [SERVICE] message {"key": "value"}"""
    
    LEVEL_MAP = {"WARNING": "WARN", "CRITICAL": "ERROR"}
    
    def format(self, record):
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        level = self.LEVEL_MAP.get(record.levelname, record.levelname)
        service = SERVICE_NAME
        message = record.getMessage()
        
        meta = {}
        for key in ['task_id', 'task_name', 'queue', 'duration_ms', 'error']:
            val = getattr(record, key, None)
            if val is not None:
                meta[key] = val
        
        meta_str = (' ' + json.dumps(meta, default=str)) if meta else ''
        return f'[{timestamp}] [{level}] [{service}] {message}{meta_str}'

def setup_worker_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(ArgentaFormatter())
    
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
```

### Worker CLI Flags
```bash
# Не перехватывать root logger (чтоб наш formatter работал)
celery -A memory_server.celery_app worker \
    --without-gossip --without-mingle --without-heartbeat \
    -l INFO \
    --logfile=-  # stdout
```

---

## Риски и митигации

| Риск | Вероятность | Влияние | Митигация |
|---|---|---|---|
| **async/sync mismatch** | Критический | Критическое | AsyncBridge (run_async) + test |
| **Pool exhaustion** | Средняя | Высокое | max=4 per process, 28 total |
| **Memory leak в workers** | Средняя | Среднее | `max_tasks_per_child=1000`, `max_memory_per_child=200MB` |
| **Qdrant sync blocking** | Низкая | Среднее | В prefork sync вызов — нормально |
| **Tools ломаются при уборке lifespan** | Высокое | Критическое | Миграция tools + server.py в одной фазе |
| **Dual-write inconsistency** | Средняя | Высокое | PG-first + retry + reconciliation |
| **Periodic worker stats** | Низкая | Среднее | Beat schedule каждые 30с, fallback на 0 |
| **Дублирование метрик** | Низкая | Низкое | Вынос _track_tool в общий декоратор |

---

## Структура файлов после миграции

```
selti/
├── memory_server/
│   ├── celery_app.py           # НОВЫЙ: Celery instance
│   ├── config.py               # Обновлён: Celery settings
│   ├── metrics.py              # ✅ Обновлён: 8 Celery + Redis + Qdrant + app метрики
│   ├── server.py               # Обновлён: lifespan упрощён
│   ├── __main__.py             # Обновлён: /health, /tasks
│   ├── tasks/                  # ✅ ЧАСТИЧНО ГОТОВ
│   │   ├── __init__.py         # ✅ Готов (Мая)
│   │   ├── async_bridge.py     # sync→async мост
│   │   ├── connections.py      # Worker singletons
│   │   ├── base.py             # Базовый класс задач
│   │   ├── memory_tasks.py     # Задачи памяти
│   │   ├── hash_tasks.py       # Задачи хешей
│   │   ├── signals.py          # ✅ Готов (Мая): Celery signals → метрики
│   │   ├── worker_stats.py     # НОВЫЙ: periodic сбор worker stats
│   │   ├── serializers.py      # JSON serialization
│   │   ├── errors.py           # Кастомные исключения
│   │   └── logging_config.py   # Argenta formatter для workers
│   ├── tools/
│   │   ├── memory_tools.py     # Обновлён: через Celery + метрики (✅)
│   │   ├── hash_tools.py       # Обновлён: через Celery
│   │   └── task_bridge.py      # НОВЫЙ: мост sync↔async
│   ├── utils/
│   │   └── metrics_decorator.py # НОВЫЙ: общий _track_tool декоратор
│   ├── cache/
│   │   └── redis_client.py     # ✅ Обновлён: Redis ops метрики
│   ├── vector/
│   │   └── qdrant_store.py     # ✅ Обновлён: Qdrant ops метрики
│   ├── embedding/
│   │   └── client.py           # ✅ Обновлён: EMBEDDING_DURATION
│   ├── memory/
│   │   └── repository_qdrant.py # ✅ Обновлён: Qdrant ops метрики
│   ├── api/                    # НОВЫЙ
│   │   └── tasks.py            # Endpoints управления
│   └── ... (без изменений)
├── tests/
│   ├── conftest.py             # Обновлён: Celery fixtures
│   ├── test_celery_tasks.py    # НОВЫЙ
│   ├── test_task_bridge.py     # НОВЫЙ
│   ├── test_serializers.py     # НОВЫЙ
│   └── ... (существующие)
├── monitoring/
│   └── alerts/
│       └── prometheus-rules.yml  # ✅ Обновлён: 7 celery + 3 app + 1 Redis алертов
├── docker-compose.yml          # Обновлён: worker, flower (dev)
├── Dockerfile                  # Обновлён: tasks/
├── requirements.txt            # Обновлён: celery, flower, structlog
└── CELERY_MIGRATION_PLAN_v3.md # Этот файл
```

---

## Ключевые принципы

1. **Incremental** — каждая фаза тестируется отдельно
2. **Rollback-ready** — каждую фазу можно откатить
3. **Non-breaking** — MCP API не меняется для клиентов
4. **Observable** — метрики и логи для каждого шага
5. **Testable** — `task_always_eager` для unit-тестов
6. **Production-ready** — graceful shutdown, memory limits, retry с backoff

---

*План v3: Момо + Эна + Сона + Нора + Мая + Катерина. 31.07.2026*
*Обновление: добавлено распределение "кто что делает" в каждой фазе, матрица ответственности за файлы, зависимости и точки согласования.*
*Обновление (31.07.2026): учтён отчёт Мая — метрики/алерты/signals.py выполнены, добавлены задачи worker_stats.py и metrics_decorator.py, обновлена матрица ответственности.*
