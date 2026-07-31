# План перехода selti на Celery — v2 (обновлённый)

**Дата:** 31.07.2026
**Проект:** selti — Python MCP-сервер семантической памяти
**Статус:** Готов к реализации
**Авторы:** Момо (plan), Сона (code analysis), Эна (architecture), Нора (DB), Мая (observability), Катерина (testing)
**Предыдущая версия:** celery_migration_plan.md (1073 строки, 8 фаз)

---

## Что изменилось с v1

| Параметр | v1 (старый) | v2 (новый) |
|---|---|---|
| Контекст | Миграция pgvector→Qdrant | Qdrant **уже готов** |
| Фазы | 8 | **6** |
| Сроки | 21 день | **~10–12 дней** |
| Tools | 25 | **22** (18 memory + 4 hash) |
| Главная проблема | Не определена | **sync/async mismatch** |
| Worker pool | 3 типа × N replicas | **1 тип, 1 очередь** (v1) |

---

## Критическая проблема: sync/async mismatch

**Весь код selti — async.** Celery prefork workers — sync. План v1 этого НЕ решает.

```
Сейчас:
  tool (async) → service (async) → asyncpg (async) → PG
                                  → httpx (async) → Embedding API
                                  → QdrantClient (sync) → Qdrant

После миграции:
  tool (async) → Celery task (sync!) → ???
```

### Решение: AsyncBridge

Каждый Celery task — sync, но внутри вызывает async код через `asyncio.new_event_loop()`:

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

**Почему НЕ eventlet/gevent:** asyncpg + green threads = конфликт (asyncpg использует uvloop, gevent monkey-patch ломает socket).

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

### Ответственный: Рэй (DevOps) + Эна (architect)

### Задачи

#### 0.1 Добавить зависимости
- **Файл:** `requirements.txt`
- **Добавить:**
  ```
  celery[redis]>=5.6.0
  flower>=2.0.0
  ```
- **Проверка:** `pip install -r requirements.txt`

#### 0.2 Обновить config.py
- **Файл:** `memory_server/config.py`
- **Добавить настройки:**
  ```python
  # ── Celery ──
  celery_broker_url: str = "redis://:@redis:6379/0"
  celery_result_backend: str = "redis://:@redis:6379/1"
  celery_task_time_limit: int = 300
  celery_task_soft_time_limit: int = 240
  ```

#### 0.3 Создать celery_app.py
- **Файл:** `memory_server/celery_app.py` (НОВЫЙ)
- **Содержимое:**
  ```python
  from celery import Celery
  from memory_server.config import settings

  app = Celery("selti")
  app.conf.update(
      broker_url=settings.celery_broker_url,
      result_backend=settings.celery_result_backend,
      task_serializer="json",
      result_serializer="json",
      accept_content=["json"],
      timezone="UTC",
      enable_utc=True,
      task_track_started=True,
      task_time_limit=settings.celery_task_time_limit,
      task_soft_time_limit=settings.celery_task_soft_time_limit,
      task_acks_late=True,
      worker_prefetch_multiplier=1,
      task_routes={
          "memory.store": {"queue": "memory"},
          "memory.search": {"queue": "memory"},
          "memory.get": {"queue": "memory"},
          "memory.update": {"queue": "memory"},
          "memory.delete": {"queue": "memory"},
          "memory.list": {"queue": "memory"},
          "memory.recent": {"queue": "memory"},
          "memory.forget": {"queue": "memory"},
          "memory.archive": {"queue": "memory"},
          "memory.link": {"queue": "memory"},
          "memory.unlink": {"queue": "memory"},
          "memory.get_relations": {"queue": "memory"},
          "memory.traverse": {"queue": "memory"},
          "memory.graph_stats": {"queue": "memory"},
          "memory.ingest_batch": {"queue": "batch"},
          "memory.namespaces": {"queue": "memory"},
          "hash.upsert": {"queue": "memory"},
          "hash.get": {"queue": "memory"},
          "hash.list": {"queue": "memory"},
          "hash.delete": {"queue": "memory"},
      },
  )
  app.autodiscover_tasks(["memory_server.tasks"])
  ```

#### 0.4 Создать структуру tasks/
- **Файлы (НОВЫЕ):**
  ```
  memory_server/tasks/__init__.py
  memory_server/tasks/async_bridge.py
  memory_server/tasks/connections.py
  memory_server/tasks/base.py
  memory_server/tasks/memory_tasks.py
  memory_server/tasks/hash_tasks.py
  memory_server/tasks/serializers.py
  memory_server/tasks/errors.py
  ```

#### 0.5 Обновить Dockerfile
- **Файл:** `Dockerfile`
- **Добавить:**
  ```dockerfile
  COPY memory_server/celery_app.py ./memory_server/
  COPY memory_server/tasks/ ./memory_server/tasks/
  ```
- ENTRYPOINT оставить как есть (uvicorn). Worker запускается через CMD override.

#### 0.6 Добавить .env переменные
- **Файл:** `.env.example`
- **Добавить:**
  ```
  CELERY_BROKER_URL=redis://:@redis:6379/0
  CELERY_RESULT_BACKEND=redis://:@redis:6379/1
  ```

### Проверка
- [ ] `celery -A memory_server.celery_app worker -l INFO` стартует
- [ ] `celery -A memory_server.celery_app inspect ping` отвечает

---

## Фаза 1: Tasks Infrastructure (2 дня)

### Ответственный: Сона (Programmer)
### Участники: Эна (architect), Нора (db-architect)

### Задачи

#### 1.1 Создать async_bridge.py
- **Файл:** `memory_server/tasks/async_bridge.py` (НОВЫЙ)
- **Паттерн:** `run_async()` для запуска async корутин в sync worker

#### 1.2 Создать connections.py
- **Файл:** `memory_server/tasks/connections.py` (НОВЫЙ)
- **Паттерн:** Worker-scoped singleton через `celery.signals.worker_process_init`
- **Ресурсы:** asyncpg pool, QdrantClient, EmbeddingClient
- **Pool sizing:** min=2, max=4 на процесс (7 процессов × max=4 = 28 соединений)

#### 1.3 Создать base.py
- **Файл:** `memory_server/tasks/base.py` (НОВЫЙ)
- **Базовый класс:** `SeltiTask` с retry, timeout, structured logging

#### 1.4 Создать serializers.py
- **Файл:** `memory_server/tasks/serializers.py` (НОВЫЙ)
- **Паттерн:** `SeltiEncoder` для JSON-сериализации Pydantic моделей, datetime, UUID

#### 1.5 Создать errors.py
- **Файл:** `memory_server/tasks/errors.py` (НОВЫЙ)
- **Исключения:** `TaskValidationError`, `TaskTimeoutError`, `TaskDependencyError`

#### 1.6 Создать memory_tasks.py
- **Файл:** `memory_server/tasks/memory_tasks.py` (НОВЫЙ)
- **Задачи:** memory.store, memory.search, memory.get, memory.update, memory.delete, memory.list, memory.recent, memory.forget, memory.archive, memory.link, memory.unlink, memory.get_relations, memory.traverse, memory.graph_stats, memory.namespaces
- **Паттерн:** `@shared_task` + `run_async(service.method, ...)` 

#### 1.7 Создать hash_tasks.py
- **Файл:** `memory_server/tasks/hash_tasks.py` (НОВЫЙ)
- **Задачи:** hash.upsert, hash.get, hash.list, hash.delete
- **Особенность:** HashRepository создаётся из worker pool

### Проверка
- [ ] `celery -A memory_server.celery_app inspect registered` показывает все задачи
- [ ] Задачи маршрутизируются в правильные очереди
- [ ] Connection pool создаётся при старте worker

---

## Фаза 2: Миграция MCP Tools (3 дня)

### Ответственный: Сона (Programmer)
### Участники: Эна (architect), Катерина (tester)

### Задачи

#### 2.1 Создать task_bridge.py
- **Файл:** `memory_server/tools/task_bridge.py` (НОВЫЙ)
- **Паттерн:**
  ```python
  async def run_task(task_name: str, timeout: float = 55.0, **kwargs) -> dict:
      result = celery_app.send_task(task_name, kwargs=kwargs)
      value = await asyncio.to_thread(result.get, timeout=timeout)
      return {"status": "ok", "result": value}
  ```

#### 2.2 Обновить memory_tools.py
- **Файл:** `memory_server/tools/memory_tools.py`
- **Изменение:** Каждый tool вместо `service.*` вызывает `run_task('memory.*', ...)`
- **Важно:** `memory_version` остаётся локально (читает VERSION файл)
- **ACL проверка** остаётся на уровне tool (до отправки в Celery)

#### 2.3 Обновить hash_tools.py
- **Файл:** `memory_server/tools/hash_tools.py`
- **Изменение:** Каждый tool вызывает `run_task('hash.*', ...)`
- **Особенность:** ACL проверка (`_check_write_auth`) остаётся на уровне tool

#### 2.4 Обновить server.py
- **Файл:** `memory_server/server.py`
- **Изменение:** Lifespan создаёт только health-check pool (lightweight)
- **Убрать:** Создание MemoryService, EmbeddingClient, QdrantClient из lifespan
- **Оставить:** run_migrations(), health pool

#### 2.5 Создать task_results.py (опционально)
- **Файл:** `memory_server/tools/task_results.py` (НОВЫЙ)
- **Tool:** `task_status(task_id)` — проверка статуса задачи

### Проверка
- [ ] `memory_store` сохраняет через Celery → запись в БД
- [ ] `memory_search` ищет через Celery → результат возвращается
- [ ] Все 22 MCP tools работают через Celery
- [ ] Таймауты работают корректно
- [ ] Retry при ошибках работает

---

## Фаза 3: Тесты (2 дня)

### Ответственный: Катерина (Tester)
### Участники: Сона (Programmer)

### Задачи

#### 3.1 Обновить conftest.py
- **Файл:** `tests/conftest.py`
- **Добавить:** Celery fixtures с `task_always_eager=True`

#### 3.2 Создать test_celery_tasks.py
- **Файл:** `tests/test_celery_tasks.py` (НОВЫЙ)
- **Тесты:** Happy path, error cases, retry behavior для каждой задачи
- **Покрытие:** ≥80%

#### 3.3 Создать test_task_bridge.py
- **Файл:** `tests/test_task_bridge.py` (НОВЫЙ)
- **Тесты:** sync→async мост, timeout handling

#### 3.4 Создать test_serializers.py
- **Файл:** `tests/test_serializers.py` (НОВЫЙ)
- **Тесты:** JSON serialization Pydantic, datetime, UUID, Enum

#### 3.5 Обновить test_tools.py
- **Файл:** `tests/test_tools.py`
- **Изменение:** Mock `task_bridge.run_task` вместо mock service

#### 3.6 Проверить regression
- **Действие:** Запустить все существующие тесты
- **Ожидание:** Все 16 тестовых файлов проходят (10 не требуют изменений)

### Проверка
- [ ] `pytest tests/ -v` — все тесты проходят
- [ ] `pytest tests/test_celery_tasks.py -v` — новые тесты проходят
- [ ] Покрытие ≥80% для tasks

---

## Фаза 4: Мониторинг + FastAPI (2 дня)

### Ответственный: Мая (Observability)
### Участники: Рэй (DevOps), Сона (Programmer)

### Задачи

#### 4.1 Добавить Celery метрики
- **Файл:** `memory_server/metrics.py`
- **Добавить:**
  - `CELERY_TASKS_TOTAL` — Counter (task, queue, status)
  - `CELERY_TASK_DURATION` — Histogram (task, queue)
  - `CELERY_TASK_RETRIES` — Counter (task, queue)
  - `CELERY_TASK_TIMEOUTS` — Counter (task, queue)
  - `CELERY_TASK_ERRORS` — Counter (task, queue, error_type)
  - `CELERY_WORKERS_ACTIVE` — Gauge (queue)

#### 4.2 Создать signals.py
- **Файл:** `memory_server/tasks/signals.py` (НОВЫЙ)
- **Сигналы:** task_prerun, task_postrun, task_failure, task_retry

#### 4.3 Расширить /health
- **Файл:** `memory_server/__main__.py`
- **Добавить:** `check_celery_health()` — ping workers через inspect

#### 4.4 Создать endpoints
- **Файл:** `memory_server/api/tasks.py` (НОВЫЙ)
- **Эндпоинты:**
  - `GET /tasks/{task_id}` — статус задачи
  - `GET /tasks` — список активных задач
  - `POST /tasks/{task_id}/cancel` — отмена

#### 4.5 Добавить алерты
- **Файл:** `monitoring/alerts/prometheus-rules.yml`
- **Группа:** celery (10 алертов)
- **Ключевые:** CeleryWorkerDown (critical), failure rate > 5% (warning), P95 duration > 60s (warning)

#### 4.6 Создать Grafana дашборд
- **Файл:** `monitoring/dashboards/celery.json` (НОВЫЙ)
- **Панели:** Active workers, task rate, queue length, duration P50/P95, error rate, worker memory

#### 4.7 Обновить docker-compose.yml
- **Добавить сервисы:** celery-worker (1 тип), flower
- **Healthcheck:** `celery inspect ping` с start_period=40s
- **Logging:** json-file с rotation (50m × 5)

### Проверка
- [ ] `/metrics` содержит `athena_celery_*` метрики
- [ ] `/health` содержит celery check
- [ ] Flower доступен на :5555
- [ ] Алерты срабатывают при тестовой ошибке

---

## Фаза 5: Оптимизация и деплой (1.5 дня)

### Ответственный: Сона + Рэй
### Участники: Катерина, Тиамат

### Задачи

#### 5.1 Benchmark
- **Действие:** Замерить latency MCP tool → Celery task → результат
- **Цель:** P95 < 2s для memory_ops

#### 5.2 Cleanup legacy
- **Действие:** Убрать прямые вызовы service из tools (если остались)
- **Действие:** Убрать MemoryService из server.py lifespan

#### 5.3 Документация
- **Обновить:** README.md (Celery конфигурация)
- **Обновить:** .env.example (Celery переменные)

#### 5.4 Деплой
- **Действие:** `docker compose up -d` с новыми сервисами
- **Проверка:** Все healthcheck'и зелёные

### Проверка
- [ ] Все тесты проходят
- [ ] Latency P95 < 2s для memory_ops
- [ ] Документация актуальна

---

## Критический путь

```
Фаза 0 → Фаза 1 → Фаза 2 → Фаза 3 → Фаза 5
                        ↓
                     Фаза 4 (параллельно с Фазой 2)
```

---

## Распределение нагрузки

| Агент | Фазы | Нагрузка |
|---|---|---|
| **Афина** | 0–5 | Контроль всех фаз |
| **Сона** | 0, 1, 2, 3, 5 | Основной исполнитель |
| **Рэй** | 0, 4, 5 | Инфраструктура |
| **Эна** | 0, 1, 2 | Архитектура, ревью |
| **Катерина** | 2, 3, 5 | Тестирование |
| **Мая** | 4 | Мониторинг |
| **Нора** | 1 | DB pool management |
| **Тиамат** | 5 | Документация |
| **Момо** | — | План (этот документ) |

---

## Риски и митигации

| Риск | Вероятность | Влияние | Митигация |
|---|---|---|---|
| **async/sync mismatch** | Критический | Критическое | AsyncBridge (run_async) + test |
| **Pool exhaustion** (70 соединений) | Средняя | Высокое | PgBouncer (transaction mode) или aggressive limits |
| **Memory leak в workers** | Средняя | Среднее | `worker_max_tasks_per_child=1000` |
| **Qdrant sync blocking** | Низкая | Среднее | В prefork sync вызов — нормально |
| **Tools ломаются при уборке lifespan** | Высокое | Критическое | Миграция tools + server.py в одной фазе |
| **Dual-write inconsistency** (PG + Qdrant) | Средняя | Высокое | PG-first + retry + reconciliation job |

---

## Структура файлов после миграции

```
selti/
├── memory_server/
│   ├── celery_app.py         # НОВЫЙ: Celery instance
│   ├── config.py             # Обновлён: Celery settings
│   ├── metrics.py            # Обновлён: Celery метрики
│   ├── server.py             # Обновлён: lifespan упрощён
│   ├── __main__.py           # Обновлён: /health, /tasks
│   ├── tasks/                # НОВЫЙ: Celery tasks
│   │   ├── __init__.py
│   │   ├── async_bridge.py   # sync→async мост
│   │   ├── connections.py    # Worker singletons
│   │   ├── base.py           # Базовый класс задач
│   │   ├── memory_tasks.py   # Задачи памяти
│   │   ├── hash_tasks.py     # Задачи хешей
│   │   ├── signals.py        # Celery signals
│   │   ├── serializers.py    # JSON serialization
│   │   └── errors.py         # Кастомные исключения
│   ├── tools/
│   │   ├── memory_tools.py   # Обновлён: через Celery
│   │   ├── hash_tools.py     # Обновлён: через Celery
│   │   └── task_bridge.py    # НОВЫЙ: мост sync↔async
│   ├── api/                  # НОВЫЙ
│   │   └── tasks.py          # Endpoints управления
│   └── ... (без изменений)
├── tests/
│   ├── conftest.py           # Обновлён: Celery fixtures
│   ├── test_celery_tasks.py  # НОВЫЙ
│   ├── test_task_bridge.py   # НОВЫЙ
│   ├── test_serializers.py   # НОВЫЙ
│   └── ... (существующие)
├── monitoring/
│   ├── dashboards/
│   │   └── celery.json       # НОВЫЙ
│   └── alerts/
│       └── prometheus-rules.yml  # Обновлён
├── docker-compose.yml        # Обновлён: workers, flower
├── Dockerfile                # Обновлён: tasks/
├── requirements.txt          # Обновлён: celery, flower
└── celery_migration_plan_v2.md  # Этот файл
```

---

## Ключевые принципы

1. **Incremental** — каждая фаза тестируется отдельно
2. **Rollback-ready** — каждую фазу можно откатить
3. **Non-breaking** — MCP API не меняется для клиентов
4. **Observable** — метрики и логи для каждого шага
5. **Testable** — `task_always_eager` для unit-тестов

---

*План v2: Момо + Эна + Сона + Нора + Мая + Катерина. 31.07.2026*
