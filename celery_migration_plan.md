# План перехода selti на Celery (обновлённый)

**Дата:** 31.07.2026
**Проект:** selti — Python MCP-сервер семантической памяти
**Статус:** Актуализирован с учётом завершённой миграции pgvector → Qdrant
**Автор:** Момо (Planner)
**Версия:** 2.0

---

## Контекст: что УЖЕ сделано

Миграция pgvector → Qdrant **полностью завершена** (Фазы 0-6 plan.md). Текущее состояние:

| Компонент | Статус | Детали |
|---|---|---|
| Qdrant | ✅ Работает | 1791 точка, HNSW индекс, on-disk |
| repository_qdrant.py | ✅ Активен | dual-write: вектор → Qdrant, метаданные → PG |
| server.py lifespan | ✅ Создаёт | pool, embedding_client, qdrant_client, repository, service |
| requirements.txt | ✅ Есть | qdrant-client>=1.12.0 |
| Docker | ✅ Есть | qdrant сервис в docker-compose.yml |
| Tools | ✅ 18 штук | async с _track_tool, service из lifespan_context |

**Вывод:** План миграции на Celery нужно писать "с чистого листа", не дублируя уже выполненные шаги.

---

## Архитектурные решения

| Параметр | Значение | Обоснование |
|----------|----------|-------------|
| Broker | Redis (уже есть) | Минимальные изменения инфраструктуры |
| Backend | Redis | Result store на том же Redis |
| Pool | prefork | CPU-bound embedding операции |
| Очереди | `memory_ops` (high), `embed_ops` (medium), `batch_ops` (low) | Разделение по приоритету |
| Workers | memory-worker (c=4), embed-worker (c=2), batch-worker (c=1) | Балансировка нагрузки |
| MCP API | **Без изменений** | Клиент не должен заметить переход |
| sync/async | MCP tools → Celery task → sync execution | Workers выполняют sync код |

---

## Критический путь

```
Фаза 0 (Инфра) → Фаза 1 (Tasks) → Фаза 2 (Tools) → Фаза 3 (Тесты) → Фаза 5 (Оптимизация)
                                  ↘
                                  Фаза 4 (Мониторинг + FastAPI) → Фаза 5
```

---

## Фаза 0: Инфраструктура Celery (1.5 дня)

### Цель
Настроить Celery app, очереди, Docker workers и минимальную конфигурацию.

### Ответственный: **Рэй (DevOps)** + **Эна (Architect)**

### Шаги

#### 0.1 Установка зависимостей
- **Файл:** `requirements.txt`
- **Действия:**
  ```diff
  + celery[redis]>=5.6.0
  + flower>=2.0.0
  + kombu>=5.3.0
  ```
- **Проверка:** `pip install -r requirements.txt` проходит
- **Время:** 5 мин

#### 0.2 Создать `memory_server/celery_app.py`
- **Новый файл**
- **Содержимое:**
  ```python
  from celery import Celery
  
  app = Celery("selti")
  
  app.config_from_object({
      "broker_url": settings.celery_broker_url,
      "result_backend": settings.celery_result_backend,
      "task_serializer": "json",
      "result_serializer": "json",
      "accept_content": ["json"],
      "task_track_started": True,
      "task_time_limit": 300,
      "task_soft_time_limit": 240,
      "task_acks_late": True,
      "worker_prefetch_multiplier": 1,
      "worker_send_task_events": True,
      "task_routes": {
          "memory.tasks.memory_*": {"queue": "memory_ops"},
          "memory.tasks.embed_*": {"queue": "embed_ops"},
          "memory.tasks.batch_*": {"queue": "batch_ops"},
          "hash.tasks.*": {"queue": "memory_ops"},
      },
  })
  
  app.autodiscover_tasks(["memory_server.tasks"])
  ```
- **Время:** 30 мин

#### 0.3 Создать `memory_server/tasks/__init__.py`
- **Новый файл** (пустой)

#### 0.4 Обновить `memory_server/config.py`
- **Файл:** `memory_server/config.py`
- **Добавить поля:**
  ```python
  celery_broker_url: str = "redis://:@redis:6379/0"
  celery_result_backend: str = "redis://:@redis:6379/1"
  celery_worker_concurrency_memory: int = 4
  celery_worker_concurrency_embed: int = 2
  celery_worker_concurrency_batch: int = 1
  ```
- **Время:** 10 мин

#### 0.5 Обновить `.env.example`
- **Добавить переменные:**
  ```
  CELERY_BROKER_URL=redis://:@redis:6379/0
  CELERY_RESULT_BACKEND=redis://:@redis:6379/1
  ```

#### 0.6 Обновить `docker-compose.yml`
- **Добавить сервисы:**
  ```yaml
  celery-memory-worker:
    build: .
    command: celery -A memory_server.celery_app worker -Q memory_ops -l INFO -c 4 -n memory-worker@%h
    env_file: .env
    depends_on:
      redis: { condition: service_healthy }
      postgres: { condition: service_healthy }
      qdrant: { condition: service_healthy }
    restart: unless-stopped

  celery-embed-worker:
    build: .
    command: celery -A memory_server.celery_app worker -Q embed_ops -l INFO -c 2 -n embed-worker@%h
    env_file: .env
    depends_on:
      redis: { condition: service_healthy }
    restart: unless-stopped

  celery-batch-worker:
    build: .
    command: celery -A memory_server.celery_app worker -Q batch_ops -l INFO -c 1 -n batch-worker@%h
    env_file: .env
    depends_on:
      redis: { condition: service_healthy }
    restart: unless-stopped

  flower:
    image: mher/flower:2.0
    command: celery -A memory_server.celery_app flower --port=5555 --enable_prometheus
    ports: ["5555:5555"]
    environment:
      CELERY_BROKER_URL: redis://redis:6379/0
    depends_on: [redis]
    restart: unless-stopped
  ```

#### 0.7 Обновить `Dockerfile`
- **Действия:** Добавить копирование `celery_app.py` и `tasks/`
-CMD entrypoint остаётся uvicorn (workers запускаются через docker-compose override)

### Проверка Фазы 0
- [ ] `celery -A memory_server.celery_app worker -Q memory_ops -l INFO` стартует
- [ ] `celery -A memory_server.celery_app inspect ping` отвечает
- [ ] Flower доступен на `http://localhost:5555`
- [ ] `docker compose up -d` поднимает все сервисы

### Риски
- Redis недоступен → healthcheck + `depends_on: condition: service_healthy`
- Конфликт портов Flower (5555) → проверить доступность

---

## Фаза 1: Инфраструктура задач (2 дня)

### Цель
Создать Celery tasks, обёртку для sync выполнения в async контексте MCP tools, и singleton connections для workers.

### Ответственный: **Сона (Programmer)** + **Нора (DB-Architect)**

### Ключевой паттерн

```
MCP Tool (async) → Celery task.delay() → Worker (sync) → Service → Repository → PG/Qdrant
                       ↓
              result.get(timeout) ← AsyncResult
```

**Важно:** MCP tools остаются async для клиента. Celery tasks выполняются sync в prefork workers.

### Шаги

#### 1.1 Создать `memory_server/tasks/base.py`
- **Базовый класс задач:**
  ```python
  from celery import shared_task
  from memory_server.tasks.connections import get_pool, get_qdrant, get_embedding
  
  class MemoryTask:
      """Базовый класс для задач памяти."""
      
      def __init__(self):
          self._pool = None
          self._qdrant = None
          self._embedding = None
      
      @property
      def pool(self):
          if self._pool is None:
              self._pool = get_pool()
          return self._pool
      
      @property
      def qdrant(self):
          if self._qdrant is None:
              self._qdrant = get_qdrant()
          return self._qdrant
      
      @property
      def embedding(self):
          if self._embedding is None:
              self._embedding = get_embedding()
          return self._embedding
  ```
- **Время:** 40 мин

#### 1.2 Создать `memory_server/tasks/connections.py`
- **Singleton connections для workers:**
  ```python
  import asyncpg
  from qdrant_client import QdrantClient
  from memory_server.config import settings
  from memory_server.db.pool import create_pool
  from memory_server.embedding.client import EmbeddingClient
  from memory_server.cache.redis_client import EmbeddingCache
  
  _pool: asyncpg.Pool | None = None
  _qdrant: QdrantClient | None = None
  _embedding: EmbeddingClient | None = None
  
  def get_pool() -> asyncpg.Pool:
      global _pool
      if _pool is None:
          import asyncio
          dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
          _pool = asyncio.get_event_loop().run_until_complete(
              create_pool(dsn=dsn, min_size=settings.db_min_connections, max_size=settings.db_max_connections)
          )
      return _pool
  
  def get_qdrant() -> QdrantClient | None:
      global _qdrant
      if _qdrant is None and settings.qdrant_enabled:
          _qdrant = QdrantClient(url=settings.qdrant_url, timeout=30)
      return _qdrant
  
  def get_embedding() -> EmbeddingClient:
      global _embedding
      if _embedding is None:
          cache = EmbeddingCache(redis_url=settings.redis_url)
          _embedding = EmbeddingClient(
              api_url=settings.embedding_api_url,
              api_key=settings.embedding_api_key,
              model=settings.embedding_model,
              dimension=settings.embedding_dimension,
              cache=cache,
          )
      return _embedding
  
  def close_all():
      global _pool, _qdrant, _embedding
      if _pool:
          import asyncio
          asyncio.get_event_loop().run_until_complete(_pool.close())
          _pool = None
      if _qdrant:
          _qdrant.close()
          _qdrant = None
      if _embedding:
          import asyncio
          asyncio.get_event_loop().run_until_complete(_embedding.aclose())
          _embedding = None
  ```
- **Время:** 40 мин

#### 1.3 Создать `memory_server/tasks/memory_tasks.py`
- **Задачи для `memory_ops`:**
  ```python
  from celery import shared_task
  
  @shared_task(name="memory.store", queue="memory_ops", bind=True, max_retries=3)
  def store_memory(self, content, user_id, metadata, namespace, importance):
      """Сохранить память через Celery."""
      # Lazy init service
      from memory_server.memory.service import MemoryService
      from memory_server.memory.namespace_repository import NamespaceRepository
      
      pool = get_pool()
      qdrant = get_qdrant()
      embedding = get_embedding()
      ns_repo = NamespaceRepository(pool=pool)
      repository = MemoryRepository(pool=pool, qdrant=qdrant, qdrant_collection=settings.qdrant_collection)
      service = MemoryService(repository=repository, embedding_provider=embedding, namespace_repository=ns_repo, config=settings)
      
      import asyncio
      record, action = asyncio.get_event_loop().run_until_complete(
          service.store(content=content, user_id=user_id, metadata=metadata, namespace=namespace, importance=importance)
      )
      return {"id": record.id, "action": action.value}
  
  @shared_task(name="memory.search", queue="embed_ops", bind=True, max_retries=3)
  def search_memory(self, query, user_id, limit, threshold, namespace):
      """Поиск памяти через Celery."""
      # ... аналогично
      pass
  
  # ... аналогично для update, delete, archive, link, unlink, get_relations, traverse, graph_stats
  ```
- **Время:** 2 часа

#### 1.4 Создать `memory_server/tasks/embed_tasks.py`
- **Задачи для `embed_ops`:**
  - `memory.search`
  - `memory.find_similar`
  - `memory.traverse`
- **Время:** 1 час

#### 1.5 Создать `memory_server/tasks/batch_tasks.py`
- **Задачи для `batch_ops`:**
  - `memory.ingest_batch`
  - `memory.forget`
- **Время:** 1 час

#### 1.6 Создать `memory_server/tasks/hash_tasks.py`
- **Задачи для `memory_ops`:**
  - `hash.upsert`, `hash.get`, `hash.list`, `hash.delete`
- **Время:** 30 мин

#### 1.7 Создать `memory_server/tasks/serializers.py`
- **JSON-сериализация Pydantic моделей:**
  ```python
  def serialize_record(record: MemoryRecord) -> dict: ...
  def deserialize_record(data: dict) -> MemoryRecord: ...
  def serialize_search_result(result: SearchResult) -> dict: ...
  ```
- **Время:** 30 мин

#### 1.8 Создать `memory_server/tasks/signals.py`
- **Celery signals для lifecycle:**
  ```python
  from celery.signals import worker_process_init, worker_process_shutdown
  
  @worker_process_init.connect
  def init_worker(**kwargs):
      get_pool()
      get_qdrant()
      get_embedding()
  
  @worker_process_shutdown.connect
  def shutdown_worker(**kwargs):
      close_all()
  ```
- **Время:** 20 мин

### Проверка Фазы 1
- [ ] `celery -A memory_server.celery_app inspect registered` показывает все задачи
- [ ] Задачи маршрутизируются в правильные очереди
- [ ] Pool создаётся при старте worker
- [ ] `celery -A memory_server.celery_app call memory.store --args='["test","user1"]'` работает

### Риски
- asyncpg pool может истощиться → настроить min/max connections
- Qdrant sync client может блокировать prefork worker → timeout=30s
- EmbeddingClient требует async → `asyncio.get_event_loop().run_until_complete()`

---

## Фаза 2: Миграция MCP Tools (3 дня)

### Цель
Переключить MCP tools с прямых вызовов service на Celery tasks.

### Ответственный: **Сона (Programmer)** + **Эна (Architect)**

### Ключевые решения

1. **Tool остаётся async** → вызывает `app.send_task()` → `result.get(timeout)`
2. **Service создаётся в lifespan** → нужен для health-check и potentially sync operations
3. **ACL/auth проверка** → остаётся на уровне tool (до отправки в Celery)
4. **Timeout** → tool timeout > task timeout > soft_time_limit

### Шаги

#### 2.1 Создать `memory_server/tools/task_bridge.py`
- **Мост sync↔async:**
  ```python
  import asyncio
  from memory_server.celery_app import app
  
  async def run_task_async(task_name: str, timeout: int = 120, **kwargs) -> Any:
      """Вызвать Celery task и дождаться результата (async)."""
      result = app.send_task(task_name, kwargs=kwargs)
      return await asyncio.to_thread(result.get, timeout=timeout)
  
  def run_task_sync(task_name: str, timeout: int = 120, **kwargs) -> Any:
      """Вызвать Celery task и дождаться результата (sync, для workers)."""
      result = app.send_task(task_name, kwargs=kwargs)
      return result.get(timeout=timeout)
  ```
- **Время:** 30 мин

#### 2.2 Модифицировать `memory_server/tools/memory_tools.py`
- **Для каждого tool:** заменить прямой вызов `service.*` на Celery task
- **Пример:**
  ```python
  @mcp.tool()
  async def memory_store(content, user_id, metadata, namespace, importance, ctx):
      metadata = _coerce_metadata(metadata)
      result = await run_task_async(
          "memory.store",
          content=content, user_id=user_id,
          metadata=metadata, namespace=namespace,
          importance=importance,
      )
      return result
  ```
- **Важно:** 
  - Tool остаётся async для клиента
  - Auth/ACL проверка — до вызова run_task_async
  - Timeout tool = timeout task + запас (120s > 60s soft)
- **Файлы:** `memory_tools.py`, `hash_tools.py`
- **Время:** 2 часа

#### 2.3 Обновить `memory_server/server.py`
- **Lifespan:** Оставить service для health-check
- **Добавить:** Проверку Celery health в health endpoint
- **Время:** 30 мин

#### 2.4 Создать `memory_server/tools/task_results.py`
- **Проверка статуса задач:**
  ```python
  @mcp.tool()
  async def task_status(task_id: str):
      result = AsyncResult(task_id, app=celery_app)
      return {"task_id": task_id, "status": result.state, "result": result.result}
  ```
- **Время:** 20 мин

#### 2.5 Создать `memory_server/tasks/errors.py`
- **Кастомные исключения:**
  ```python
  class TaskValidationError(ValueError): ...
  class TaskTimeoutError(Exception): ...
  class TaskDependencyError(Exception): ...
  ```
- **Время:** 10 мин

### Проверка Фазы 2
- [ ] `memory_store` сохраняет через Celery → запись в БД
- [ ] `memory_search` ищет через Celery → результат возвращается клиенту
- [ ] `memory_ingest_batch` обрабатывает батч через batch_ops очередь
- [ ] Все 18 MCP tools работают через Celery
- [ ] Таймауты работают корректно
- [ ] Retry при ошибках Redis/PG работает

### Риски
- **Критический:** MCP tool блокируется пока task не выполнится
  - Митигация: `task_soft_time_limit=240` < `tool_timeout=300`
- Service больше не создаёт pool в lifespan → health endpoint должен работать без pool
  - Решение: Health endpoint проверяет Celery ping, а не PG pool

---

## Фаза 3: Тесты (2 дня)

### Цель
Убедиться, что все существующие тесты продолжают работать, и написать тесты для Celery tasks.

### Ответственный: **Катерина (Tester)** + **Сона (Programmer)**

### Шаги

#### 3.1 Обновить `tests/conftest.py`
- **Добавить fixture для Celery:**
  ```python
  @pytest.fixture
  def celery_app():
      from memory_server.celery_app import app
      app.conf.update(CELERY_ALWAYS_EAGER=True)
      return app
  ```

#### 3.2 Создать `tests/test_celery_tasks.py`
- **Тесты для каждой задачи:**
  - Happy path (успешное выполнение)
  - Error cases (невалидные данные, таймауты)
  - Retry behavior
  - Connection failures
- **~30-40 тестов**

#### 3.3 Создать `tests/test_serializers.py`
- **Тесты JSON-сериализации:**
  - datetime, UUID, None, nested dicts
  - Edge cases

#### 3.4 Обновить `tests/test_tools.py`
- **Mock Celery tasks** в tool тестах
- Проверить что tools корректно вызывают tasks

#### 3.5 Проверить все существующие тесты
- `pytest tests/ -v` — все 14 файлов тестов должны пройти

### Проверка Фазы 3
- [ ] `pytest tests/ -v` — все тесты проходят
- [ ] `pytest tests/test_celery_tasks.py -v` — новые тесты проходят
- [ ] Покрытие кода ≥80% для tasks

---

## Фаза 4: Мониторинг + Логирование (2 дня)

### Цель
Настроить Prometheus метрики, structured logging, alerting rules, health checks для Celery workers.

### Ответственный: **Мая (Observability)** + **Сона (Programmer)**

### Архитектура observability

```
┌─────────────────────────────────────────────────┐
│                   selti-worker                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Prometheus│  │ structlog│  │ Celery       │  │
│  │ metrics  │  │ JSON logs│  │ signals      │  │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│       │              │               │           │
│  ┌────┴──────────────┴───────────────┴───────┐  │
│  │         metrics.py + logging.py           │  │
│  └─────────────────────┬─────────────────────┘  │
│                        │ :9090/metrics           │
└────────────────────────┼────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │     Prometheus      │
              │  (scrape :9090)     │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   Alertmanager      │
              │  (webhook/telegram) │
              └─────────────────────┘
```

### Шаги

#### 4.1 Обновить `memory_server/metrics.py` — полный набор Celery метрик

**Обязательные метрики (production):**

```python
from prometheus_client import Counter, Histogram, Gauge, Info

# --- Task lifecycle (ядро) ---
CELERY_TASK_SENT = Counter(
    "celery_task_sent_total",
    "Task sent to broker",
    ["task", "queue"],
)

CELERY_TASK_RECEIVED = Counter(
    "celery_task_received_total",
    "Task received by worker",
    ["task", "queue"],
)

CELERY_TASK_STARTED = Counter(
    "celery_task_started_total",
    "Task started execution",
    ["task", "queue"],
)

CELERY_TASK_SUCCEEDED = Counter(
    "celery_task_succeeded_total",
    "Task completed successfully",
    ["task", "queue"],
)

CELERY_TASK_FAILED = Counter(
    "celery_task_failed_total",
    "Task failed",
    ["task", "queue", "exception"],
)

CELERY_TASK_RETRIES = Counter(
    "celery_task_retries_total",
    "Task retry attempts",
    ["task", "queue"],
)

CELERY_TASK_REJECTED = Counter(
    "celery_task_rejected_total",
    "Task rejected by worker",
    ["task", "queue"],
)

CELERY_TASK_REVOKED = Counter(
    "celery_task_revoked_total",
    "Task revoked/cancelled",
    ["task"],
)

# --- Duration (histogram) ---
CELERY_TASK_DURATION = Histogram(
    "celery_task_duration_seconds",
    "Task execution duration (started → succeeded/failed)",
    ["task", "queue"],
    buckets=[0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300],
)

CELERY_TASK_LATENCY = Histogram(
    "celery_task_latency_seconds",
    "Time from sent to started (queue wait + pickup)",
    ["task", "queue"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
)

# --- Worker state ---
CELERY_WORKER_online = Gauge(
    "celery_worker_online",
    "Worker online status (1=online, 0=offline)",
    ["worker"],
)

CELERY_WORKER_ACTIVE_TASKS = Gauge(
    "celery_worker_active_tasks",
    "Number of active tasks per worker",
    ["worker"],
)

CELERY_WORKER_PROCESSES = Gauge(
    "celery_worker_processes",
    "Number of child processes per worker",
    ["worker"],
)

CELERY_WORKER_RSS_BYTES = Gauge(
    "celery_worker_rss_bytes",
    "Worker RSS memory usage",
    ["worker"],
)

# --- Queue depth ---
CELERY_QUEUE_LENGTH = Gauge(
    "celery_queue_length",
    "Number of unacknowledged messages in queue",
    ["queue"],
)

# --- Broker connectivity ---
CELERY_BROKER_CONNECTIVITY = Gauge(
    "celery_broker_connectivity",
    "Broker connection status (1=connected, 0=disconnected)",
)

# --- Info ---
CELERY_APP_INFO = Info(
    "celery_app",
    "Celery application metadata",
)
```

**Label cardinality — строго контролировать:**
| Label | Допустимые значения | Риск cardinality |
|-------|---------------------|------------------|
| `task` | `memory.store`, `memory.search`, ... (~10 values) | ✅ Безопасно |
| `queue` | `memory_ops`, `embed_ops`, `batch_ops` (3 values) | ✅ Безопасно |
| `worker` | `memory-worker@%h`, ... (~3 values) | ✅ Безопасно |
| `status` | `succeeded`, `failed` (2 values) | ✅ Безопасно |
| `exception` | `TimeoutError`, `OperationalError`, ... (~10 values) | ✅ Безопасно |
| ❌ `task_id` | уникальные = cardinality explosion | 🚫 НИКОГДА |

**Histogram buckets — обоснование:**
- `duration`: [0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300] — memory ops 0.1-5s, embed ops 1-30s, batch 10-300s
- `latency`: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30] — queue wait, обычно <1s

#### 4.2 Обновить `memory_server/tasks/signals.py` — lifecycle + метрики + логи

```python
import time
import structlog
from celery.signals import (
    task_sent, task_received, task_started,
    task_success, task_failure, task_retry,
    task_rejected, task_revoked,
    worker_ready, worker_shutdown,
)

logger = structlog.get_logger("selti-worker")

# --- Task lifecycle metrics ---

@task_sent.connect
def handle_task_sent(sender, task_id, task, args, kwargs, **kw):
    from memory_server.metrics import CELERY_TASK_SENT
    CELERY_TASK_SENT.labels(task=task, queue=kw.get("queue", "default")).inc()
    logger.debug("task_sent", task_id=task_id, task=task)

@task_received.connect
def handle_task_received(sender, task_id, task, args, kwargs, **kw):
    from memory_server.metrics import CELERY_TASK_RECEIVED
    CELERY_TASK_RECEIVED.labels(task=task, queue=kw.get("queue", "default")).inc()
    # Store start time for duration calculation
    _task_start_times[task_id] = time.monotonic()
    logger.info("task_received", task_id=task_id, task=task)

@task_started.connect
def handle_task_started(sender, task_id, **kw):
    from memory_server.metrics import CELERY_TASK_STARTED
    CELERY_TASK_STARTED.labels(task=kw.get("task", "unknown"), queue=kw.get("queue", "default")).inc()
    logger.info("task_started", task_id=task_id)

@task_success.connect
def handle_task_result(sender, task_id, result, **kw):
    from memory_server.metrics import CELERY_TASK_SUCCEEDED, CELERY_TASK_DURATION
    task_name = sender.name if sender else "unknown"
    queue = kw.get("queue", "default")
    CELERY_TASK_SUCCEEDED.labels(task=task_name, queue=queue).inc()
    # Duration
    start = _task_start_times.pop(task_id, None)
    if start:
        duration = time.monotonic() - start
        CELERY_TASK_DURATION.labels(task=task_name, queue=queue).observe(duration)
        logger.info("task_succeeded", task_id=task_id, duration=round(duration, 3))

@task_failure.connect
def handle_task_failure(sender, task_id, exception, traceback, **kw):
    from memory_server.metrics import CELERY_TASK_FAILED, CELERY_TASK_DURATION
    task_name = sender.name if sender else "unknown"
    queue = kw.get("queue", "default")
    CELERY_TASK_FAILED.labels(task=task_name, queue=queue, exception=type(exception).__name__).inc()
    start = _task_start_times.pop(task_id, None)
    if start:
        duration = time.monotonic() - start
        CELERY_TASK_DURATION.labels(task=task_name, queue=queue).observe(duration)
    logger.error("task_failed", task_id=task_id, exception=str(exception), duration=round(duration, 3) if start else None)

@task_retry.connect
def handle_task_retry(sender, request, reason, **kw):
    from memory_server.metrics import CELERY_TASK_RETRIES
    CELERY_TASK_RETRIES.labels(task=sender.name, queue=kw.get("queue", "default")).inc()
    logger.warn("task_retry", task_id=request.id, reason=str(reason), attempt=request.retries)

@task_rejected.connect
def handle_task_rejected(sender, **kw):
    from memory_server.metrics import CELERY_TASK_REJECTED
    CELERY_TASK_REJECTED.labels(task=kw.get("task", "unknown"), queue=kw.get("queue", "default")).inc()
    logger.warn("task_rejected", task_id=kw.get("task_id"), reason=kw.get("reason"))

@task_revoked.connect
def handle_task_revoked(sender, request, terminated, signum, expired, **kw):
    from memory_server.metrics import CELERY_TASK_REVOKED
    CELERY_TASK_REVOKED.labels(task=sender.name if sender else "unknown").inc()
    logger.warn("task_revoked", task_id=request.id if request else None)

# --- Worker lifecycle ---

@worker_ready.connect
def handle_worker_ready(sender, **kw):
    logger.info("worker_ready", worker=sender.hostname)

@worker_shutdown.connect
def handle_worker_shutdown(sender, **kw):
    logger.info("worker_shutdown", worker=sender.hostname)

# --- Helpers ---
_task_start_times: dict[str, float] = {}
```

#### 4.3 Настроить structured logging для workers — стандарт Argenta

**Формат:** `[ISO8601] [LEVEL] [service] message {"key": "value"}`

**Пример:**
```
2026-07-31T14:30:00.123Z [INFO] [selti-worker] task_received {"task_id": "abc-123", "task": "memory.store", "queue": "memory_ops"}
2026-07-31T14:30:00.456Z [ERROR] [selti-worker] task_failed {"task_id": "abc-123", "exception": "TimeoutError", "duration": 240.0}
```

**Реализация — `memory_server/logging_config.py`:**

```python
import logging
import structlog
import sys
from datetime import datetime, timezone

SERVICE_NAME = "selti-worker"

def setup_worker_logging(log_level: str = "INFO"):
    """Настройка structured logging для Celery workers."""
    
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            add_service_name,
            structlog.dev.ConsoleRenderer() if sys.stderr.isatty() else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(log_level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

def add_service_name(logger, method_name, event_dict):
    event_dict["service"] = SERVICE_NAME
    return event_dict

# --- Celery log format overrides ---
# Убираем стандартный формат Celery "[YYYY-MM-DD HH:MM:SS,mmm: LEVEL/ProcessName]"
# и заменяем на structlog

CELERY_WORKER_LOG_FORMAT = "%(message)s"
CELERY_WORKER_TASK_LOG_FORMAT = "%(message)s"
CELERY_WORKER_HIJACK_ROOT_LOGGER = False  # Не перехватывать root logger
```

**В `celery_app.py` добавить:**
```python
app.conf.update(
    worker_hijack_root_logger=False,
    worker_log_format="%(message)s",
    worker_task_log_format="%(message)s",
)
```

**Correlation ID через task_id:**
```python
# В signals.py, task_received:
structlog.contextvars.clear_contextvars()
structlog.contextvars.bind_contextvars(task_id=task_id, task_name=task)

# Все последующие логи в рамках этой задачи будут содержать task_id
```

**Уровни логирования — строго:**
| Уровнь | Когда | Пример |
|--------|-------|--------|
| `DEBUG` | Трассировка, dev only | `task_sent`, payload |
| `INFO` | Нормальные события | `task_received`, `task_succeeded`, `worker_ready` |
| `WARN` | Требует внимания, но не ошибка | `task_retry`, `task_rejected`, `queue_backlog` |
| `ERROR` | Ошибка, требующая действия | `task_failed`, `connection_lost` |

⚠️ **WARN, не WARNING!** Стандарт Argenta.

#### 4.4 Создать `memory_server/api/tasks.py` — HTTP endpoints

```python
from fastapi import APIRouter, HTTPException
from celery import app as celery_app

router = APIRouter(prefix="/tasks", tags=["tasks"])

@router.get("/{task_id}")
async def get_task_status(task_id: str):
    """Статус конкретной задачи."""
    from celery.result import AsyncResult
    result = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": result.state,
        "result": result.result if result.ready() else None,
    }

@router.get("/")
async def list_active_tasks():
    """Список активных задач по worker."""
    inspector = celery_app.control.inspect(timeout=3.0)
    active = inspector.active() or {}
    return {"workers": active}

@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Отменить задачу."""
    celery_app.control.revoke(task_id, terminate=True)
    return {"task_id": task_id, "status": "revoked"}

@router.get("/queues")
async def get_queue_lengths():
    """Длины очередей (из Redis)."""
    # Реализация через redis LLEN
    ...
```

#### 4.5 Обновить `memory_server/__main__.py`

- Подключить `tasks router` к FastAPI app
- Добавить `/workers` endpoint (список workers через inspect)

#### 4.6 Создать `memory_server/tasks/celery_health.py`

```python
def check_celery_health() -> dict:
    """Проверка здоровья Celery cluster."""
    try:
        inspector = celery_app.control.inspect(timeout=3.0)
        ping = inspector.ping()
        active = inspector.active()
        stats = inspector.stats()
        
        workers_online = len(ping) if ping else 0
        total_active = sum(len(v) for v in (active or {}).values())
        
        return {
            "healthy": workers_online > 0,
            "workers_online": workers_online,
            "active_tasks": total_active,
            "workers": list(ping.keys()) if ping else [],
        }
    except Exception as e:
        return {"healthy": False, "error": str(e)}
```

**В health endpoint (`/health`):**
```python
@app.get("/health")
async def health():
    celery = check_celery_health()
    return {
        "status": "healthy" if celery["healthy"] else "degraded",
        "components": {
            "celery": celery,
            "postgres": ...,
            "qdrant": ...,
        },
    }
```

#### 4.7 Обновить `monitoring/alerts/prometheus-rules.yml` — production alerting

```yaml
groups:
  - name: celery_alerts
    rules:
      # === КРИТИЧЕСКИЕ ===
      
      - alert: CeleryWorkerDown
        expr: sum(celery_worker_online) == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "All Celery workers are down"
          description: "No Celery workers responding for {{ $value }} minutes"
      
      - alert: CeleryTaskFailureRateHigh
        expr: |
          sum(rate(celery_task_failed_total[5m])) by (task)
          / sum(rate(celery_task_succeeded_total[5m]) + rate(celery_task_failed_total[5m])) by (task)
          > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Celery task failure rate > 5% for {{ $labels.task }}"
          description: "{{ $value | humanizePercentage }} failure rate over 5 minutes"
      
      # === ВЫСОКИЕ ===
      
      - alert: CeleryTaskDurationHigh
        expr: |
          histogram_quantile(0.95, sum(rate(celery_task_duration_seconds_bucket[5m])) by (le, task))
          > 60
        for: 10m
        labels:
          severity: high
        annotations:
          summary: "Celery task P95 duration > 60s for {{ $labels.task }}"
          description: "P95 is {{ $value | humanizeDuration }}"
      
      - alert: CeleryQueueBacklog
        expr: celery_queue_length > 100
        for: 5m
        labels:
          severity: high
        annotations:
          summary: "Queue {{ $labels.queue }} backlog > 100 tasks"
          description: "{{ $value }} tasks waiting"
      
      # === СРЕДНИЕ ===
      
      - alert: CeleryTaskRetrySpike
        expr: sum(rate(celery_task_retries_total[5m])) by (task) > 0.2
        for: 10m
        labels:
          severity: medium
        annotations:
          summary: "High retry rate for {{ $labels.task }}"
          description: "{{ $value }} retries/sec over 5 minutes"
      
      - alert: CeleryWorkerMemoryHigh
        expr: |
          celery_worker_rss_bytes / (worker_max_memory_per_child * 1024) > 0.8
        for: 5m
        labels:
          severity: medium
        annotations:
          summary: "Worker {{ $labels.worker }} memory > 80% of limit"
```

#### 4.8 Обновить `docker-compose.yml` — метрики endpoints

```yaml
celery-memory-worker:
  ...
  # Добавить port для Prometheus scrape (если нужен отдельный)
  # Или скрапить через Flower /app metrics

flower:
  ...
  command: celery -A memory_server.celery_app flower --port=5555 --enable_prometheus
  ports:
    - "5555:5555"
```

#### 4.9 Добавить `prometheus.yml` scrape config

```yaml
scrape_configs:
  - job_name: "celery-flower"
    static_configs:
      - targets: ["flower:5555"]
    metrics_path: "/metrics"
    scrape_interval: 15s
  
  - job_name: "selti-memory-server"
    static_configs:
      - targets: ["memory-server:8000"]
    metrics_path: "/metrics"
    scrape_interval: 15s
```

#### 4.10 Docker healthcheck для workers

```yaml
celery-memory-worker:
  ...
  healthcheck:
    test: ["CMD-SHELL", "celery -A memory_server.celery_app inspect ping --destination celery@$$HOSTNAME || exit 1"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 15s
```

⚠️ `inspect ping` может нагружать CPU при большом кластере. Для 3 workers — допустимо. Для масштаба — перейти на HTTP health endpoint.

### Проверка Фазы 4
- [ ] `curl http://localhost:8000/metrics` содержит `celery_task_*` метрики
- [ ] Логи worker в формате `[ISO8601] [LEVEL] [selti-worker] message {"key": "value"}`
- [ ] `task_id` присутствует в логах задач (correlation ID)
- [ ] `GET /tasks/{task_id}` возвращает статус
- [ ] `GET /workers` возвращает список workers
- [ ] `GET /health` содержит celery check
- [ ] Alertmanager получает алерты при simulating failure
- [ ] `WARN` используется вместо `WARNING`
- [ ] Нет label cardinality explosion (проверить через `label_replace`)

### Production checklist

- [ ] Метрики: все обязательные counters/histograms/gauges на месте
- [ ] Labels: только `task`, `queue`, `worker`, `status`, `exception` — нет `task_id`
- [ ] Histogram buckets: обоснованы для selti workload
- [ ] Alert rules: все 6 алертов настроены с правильными порогами
- [ ] Logging: structlog JSON в production, console в dev
- [ ] Log levels: DEBUG → INFO → WARN → ERROR (не WARNING!)
- [ ] Correlation ID: task_id в каждом логе задачи
- [ ] Health check: `/health` отвечает 200/503
- [ ] Docker healthcheck: workers помечаются unhealthy при падении
- [ ] Prometheus scrape: interval 15s, targets корректны

---

## Фаза 5: Оптимизация и финализация (1.5 дня)

### Цель
Оптимизировать производительность, cleanup, документация.

### Ответственный: **Сона (Programmer)** + **Катерина (Tester)** + **Тиамат (Tech-Writer)**

### Шаги

#### 5.1 Оптимизация connection pool
- `worker_max_tasks_per_child=1000` (предотвращение memory leak)
- `worker_max_memory_per_child=200000` (200MB)
- Мониторинг через метрики

#### 5.2 Performance benchmark
- Замерить latency: MCP tool → Celery task → результат
- Сравнить с baseline (до Celery)
- Убедиться P95 < 2s для memory_ops

#### 5.3 Cleanup
- Удалить `celery_docs_summary.md`
- Обновить VERSION
- Обновить CHANGELOG

#### 5.4 Документация
- README.md: обновить секцию установки/конфигурации
- Добавить документацию по Celery tasks
- Описать очереди и маршрутизацию

#### 5.5 Финальный `pytest`
- Убедиться что все тесты проходят
- Проверить покрытие

### Проверка Фазы 5
- [ ] Все тесты проходят
- [ ] P95 latency < 2s для memory_ops
- [ ] P95 latency < 5s для embed_ops
- [ ] No memory leaks в workers (мониторинг 24h)
- [ ] Документация актуальна

---

## Итого: Оценка времени

| Фаза | Дни | Зависит от | Ответственный |
|------|-----|-----------|---------------|
| Фаза 0: Инфраструктура Celery | 1.5 | — | **Рэй** |
| Фаза 1: Инфраструктура задач | 2 | Фаза 0 | **Сона** |
| Фаза 2: Миграция MCP Tools | 3 | Фаза 1 | **Сона** |
| Фаза 3: Тесты | 2 | Фаза 2 | **Катерина** |
| Фаза 4: Мониторинг + FastAPI | 2 | Фаза 1 | **Мая** + **Сона** |
| Фаза 5: Оптимизация | 1.5 | Фаза 3, 4 | **Сона** |
| **ИТОГО** | **~12 дней** | | |

### Параллельные задачи
- Фаза 4 (мониторинг) может идти параллельно с Фазой 2 (миграция tools)
- Фаза 3 (тесты) может частично идти параллельно с Фазой 2

---

## Распределение ролей: сводная таблица

| Агент | Фазы | Общая нагрузка |
|---|---|---|
| **Афина** | 0, 1, 2, 3, 4, 5 | Контроль всех фаз |
| **Сона** | 1, 2, 4, 5 | Основной исполнитель (4 фазы) |
| **Рэй** | 0 | Инфраструктура (1 фаза) |
| **Эна** | 0, 1, 2 | Архитектура (3 фазы) |
| **Катерина** | 3, 5 | Тестирование (2 фазы) |
| **Мая** | 4 | Мониторинг: метрики, structured logging, alerting, health checks |
| **Нора** | 1 | DB (1 фаза) |
| **Тиамат** | 5 | Документация (1 фаза) |
| **Момо** | — | План (этот документ) |

---

## Структура файлов после миграции

```
selti/
├── memory_server/
│   ├── celery_app.py         # НОВЫЙ: Celery instance
│   ├── config.py             # Обновлён: Celery settings
│   ├── metrics.py            # Обновлён: полный набор Celery метрик
│   ├── logging_config.py     # НОВЫЙ: structured logging (structlog)
│   ├── server.py             # Без изменений (lifespan остаётся)
│   ├── api/
│   │   └── tasks.py          # НОВЫЙ: APIRouter tasks (status, cancel, queues)
│   ├── tasks/                # НОВЫЙ: Celery tasks
│   │   ├── __init__.py
│   │   ├── base.py           # Базовый класс задач
│   │   ├── connections.py    # Singleton connections
│   │   ├── memory_tasks.py   # Задачи памяти
│   │   ├── embed_tasks.py    # Задачи эмбеддингов
│   │   ├── batch_tasks.py    # Батч-задачи
│   │   ├── hash_tasks.py     # Задачи хешей
│   │   ├── signals.py        # Celery signals + metrics + structured logs
│   │   ├── serializers.py    # JSON serialization
│   │   ├── errors.py         # Кастомные исключения
│   │   └── celery_health.py  # Health check
│   ├── tools/
│   │   ├── memory_tools.py   # Обновлён: вызовы через Celery
│   │   ├── hash_tools.py     # Обновлён: вызовы через Celery
│   │   ├── task_bridge.py    # НОВЫЙ: мост sync↔async
│   │   └── task_results.py   # НОВЫЙ: проверка статуса
│   └── ... (без изменений)
├── tests/
│   ├── conftest.py           # Обновлён: Celery fixtures
│   ├── test_celery_tasks.py  # НОВЫЙ
│   ├── test_serializers.py   # НОВЫЙ
│   └── ... (существующие тесты обновлены)
├── monitoring/
│   └── alerts/
│       └── prometheus-rules.yml  # Обновлён: 6 Celery алертов
├── docker-compose.yml        # Обновлён: workers, flower, healthchecks
├── Dockerfile                # Обновлён: workers
├── requirements.txt          # Обновлён: celery, flower, structlog, prometheus-client
└── celery_migration_plan.md  # ЭТОТ ФАЙЛ
```

**Убрано:**
- ❌ `monitoring/dashboards/celery.json` — Grafana будет отдельным сервисом потом
- ❌ Flower dashboard интеграция с Grafana

---

## Риски и митигации

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| MCP tool блокируется пока task не выполнится | Средняя | Высокое | `task_soft_time_limit=240` < `tool_timeout=300` |
| Redis недоступен | Низкая | Критическое | Healthcheck + `depends_on: condition: service_healthy` |
| asyncpg pool истощается | Средняя | Высокое | `db_min_connections=2`, `db_max_connections=10` |
| Qdrant sync client блокирует prefork worker | Низкая | Среднее | `timeout=30` для Qdrant operations |
| EmbeddingClient не работает в sync context | Средняя | Высокое | `asyncio.get_event_loop().run_until_complete()` |
| Workers потребляют много памяти | Средняя | Среднее | `worker_max_memory_per_child=200000` |
| Performance regression | Средняя | Среднее | Benchmark before/after, P95 < 2s |

---

## Rollback процедура

### Откат на любом этапе:
1. Остановить workers: `docker compose down celery-memory-worker celery-embed-worker celery-batch-worker`
2. Откатить `memory_tools.py` и `hash_tools.py` к прямым вызовам service
3. Откатить `server.py` (убрать Celery health check)
4. Удалить `tasks/` directory
5. Удалить `celery_app.py`
6. Откатить `requirements.txt`
7. Пересобрать: `docker compose build memory-server`
8. Запустить: `docker compose up -d memory-server`

---

## Ключевые принципы миграции

1. **Incremental** — каждая фаза тестируется отдельно
2. **Rollback-ready** — каждую фазу можно откатить без потери данных
3. **Non-breaking** — MCP API не меняется для клиентов
4. **Observable** — метрики и логи для каждого шага
5. **Testable** — `CELERY_ALWAYS_EAGER` для unit-тестов
6. **Minimal changes** — используем существующую инфраструктуру (Redis, pool, embedding client)

---

*План обновлён 31.07.2026 Момо (Planner)*
*Учтено: завершённая миграция pgvector → Qdrant, реальная структура кода*
