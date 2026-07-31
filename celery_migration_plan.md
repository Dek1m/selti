# План перехода selti на полный Celery

**Дата:** 31.07.2026
**Проект:** selti — Python MCP-сервер семантической памяти
**Статус:** В разработке
**Автор:** Момо (Planner)
**Обновлено:** Делегация ролей добавлена

---

## Команда и зоны ответственности

| Агент | Роль | Зоны в этом плане |
|---|---|---|
| **Афина** | Team Lead | Оркестрация, контроль, согласование с Милордом |
| **Момо** | Planner | Планирование, декомпозиция, актуализация плана |
| **Эна** | Architect | Архитектурные решения, конфигурация Celery, ревью |
| **Сона** | Programmer | Реализация tasks, миграция tools, cleanup |
| **Катерина** | Tester | Unit/integration/load тесты, regression |
| **Нора** | DB-Architect | Connection pool, оптимизация запросов |
| **Рэй** | DevOps | Docker, CI/CD, деплой, инфраструктура |
| **Лита** | Security | Аудит безопасности, secrets, auth |
| **Тиамат** | Tech-Writer | Документация, README, runbooks |
| **Мая** | Observability | Метрики, мониторинг, Grafana, алерты |

---

## Архитектурные решения (уже приняты)

| Параметр | Значение |
|----------|----------|
| Broker | Redis (уже есть в инфраструктуре) |
| Backend | Redis (result store) |
| Pool | prefork |
| Очереди | `memory_ops` (high), `embed_ops` (medium), `batch_ops` (low) |
| Workers | `memory-worker` (c=4, replicas=2), `embed-worker` (c=2, replicas=1), `batch-worker` (c=1, replicas=1) |
| Мониторинг | Flower + Prometheus метрики |
| Lazy init | Embedding models загружаются при старте worker |

---

## Классификация операций по очередям

### Очередь `memory_ops` (high priority)
Операции с памятью, требующие минимального отклика:
- `memory_store` — одиночное сохранение
- `memory_get` — получение по ID (read-only, может остаться sync)
- `memory_update` — обновление (включая ре-эмбеддинг)
- `memory_delete` — удаление
- `memory_archive` — архивация
- `memory_link` / `memory_unlink` — управление связями
- `hash_upsert` / `hash_get` / `hash_list` / `hash_delete` — хеши

### Очередь `embed_ops` (medium priority)
Операции, связанные с вычислением эмбеддингов:
- `memory_search` — семантический поиск (embed + search)
- `memory_find_similar` — поиск похожих
- `memory_traverse` — обход графа (embed + CTE walk)
- `memory_graph_stats` — статистика графа (read-only)
- `memory_recent` / `memory_list` / `memory_stats` / `memory_namespaces` — read-only, можно в embed_ops или оставить sync

### Очередь `batch_ops` (low priority)
Пакетные операции:
- `memory_ingest_batch` — батчевая вставка (embed + insert + sync)
- `memory_forget` — удаление всех записей пользователя

> **Важно:** MCP tools должны оставаться синхронными с точки зрения клиента (caller). Задачи в очередях — это фоновые операции. MCP tool вызывает Celery task, ждёт результат (или возвращает task ID для async).

---

## Фаза 0: Подготовка инфраструктуры (2 дня)

### Цель
Настроить Celery app, Docker, очереди и минимальную конфигурацию.

### Ответственный: **Рэй (DevOps)**
### Участники: **Эна (architect)**, **Афина (Team Lead)**

### Задачи по агентам

| Агент | Задачи |
|---|---|
| **Рэй** | Docker Compose (workers, flower), Dockerfile, env vars, healthcheck |
| **Эна** | Конфигурация Celery (queues, routing, serialization), celery_config.py |
| **Афина** | Контроль, согласование с Милордом перед стартом следующей фазы |

### Точки согласования
- [ ] **Старт фазы:** Афина подтверждает, что Redis доступен в dev-окружении
- [ ] **Конец фазы:** Рэй + Эна демонстрируют `celery inspect ping` → Афина принимает

### Шаги

1. [ ] **Установка зависимостей**
   - Файл: `requirements.txt`
   - Добавить: `celery[redis]>=5.6.0`, `flower>=2.0.0`
   - Добавить: `kombu>=5.3.0` (для Exchange/Queue)

2. [ ] **Создать `memory_server/celery_app.py`**
   - Celery instance с конфигурацией из `settings`
   - `broker_url` и `result_backend` из env (Redis)
   - Сериализация: только JSON
   - `task_track_started=True`
   - `task_time_limit=300`, `task_soft_time_limit=240`
   - `task_acks_late=True`, `worker_prefetch_multiplier=1`
   - `worker_send_task_events=True` (для Flower)
   - Auto-discover tasks из `memory_server.tasks`

3. [ ] **Создать `memory_server/tasks/__init__.py`**
   - Пустой файл для пакета

4. [ ] **Создать `memory_server/tasks/celery_config.py`**
   - Конфигурация очередей через `kombu.Exchange` и `kombu.Queue`:
     ```python
     memory_ops = Queue('memory_ops', Exchange('memory_ops'), routing_key='memory')
     embed_ops = Queue('embed_ops', Exchange('embed_ops'), routing_key='embed')
     batch_ops = Queue('batch_ops', Exchange('batch_ops'), routing_key='batch')
     ```
   - `task_routes` для маршрутизации задач по очередям

5. [ ] **Обновить `memory_server/config.py`**
   - Добавить настройки Celery:
     ```python
     celery_broker_url: str = "redis://:@redis:6379/0"
     celery_result_backend: str = "redis://:@redis:6379/1"
     celery_worker_concurrency_memory: int = 4
     celery_worker_concurrency_embed: int = 2
     celery_worker_concurrency_batch: int = 1
     ```

6. [ ] **Обновить `.env.example`**
   - Добавить переменные Celery:
     ```
     CELERY_BROKER_URL=redis://:@redis:6379/0
     CELERY_RESULT_BACKEND=redis://:@redis:6379/1
     CELERY_WORKER_CONCURRENCY_MEMORY=4
     CELERY_WORKER_CONCURRENCY_EMBED=2
     CELERY_WORKER_CONCURRENCY_BATCH=1
     ```

7. [ ] **Обновить `docker-compose.yml`**
   - Добавить сервис `celery-memory-worker`:
     ```yaml
     celery-memory-worker:
       build: .
       command: celery -A memory_server.celery_app worker -Q memory_ops -l INFO -c 4 -n memory-worker@%h
       env_file: .env
       depends_on:
         redis:
           condition: service_healthy
         postgres:
           condition: service_healthy
       restart: unless-stopped
     ```
   - Добавить сервис `celery-embed-worker`:
     ```yaml
     celery-embed-worker:
       build: .
       command: celery -A memory_server.celery_app worker -Q embed_ops -l INFO -c 2 -n embed-worker@%h
       env_file: .env
       depends_on:
         redis:
           condition: service_healthy
       restart: unless-stopped
     ```
   - Добавить сервис `celery-batch-worker`:
     ```yaml
     celery-batch-worker:
       build: .
       command: celery -A memory_server.celery_app worker -Q batch_ops -l INFO -c 1 -n batch-worker@%h
       env_file: .env
       depends_on:
         redis:
           condition: service_healthy
       restart: unless-stopped
     ```
   - Добавить сервис `flower`:
     ```yaml
     flower:
       image: mher/flower:2.0
       command: celery -A memory_server.celery_app flower --port=5555 --enable_prometheus
       ports:
         - "5555:5555"
       environment:
         - CELERY_BROKER_URL=redis://redis:6379/0
         - CELERY_RESULT_BACKEND=redis://redis:6379/1
       depends_on:
         - redis
       restart: unless-stopped
     ```

8. [ ] **Обновить `Dockerfile`**
   - Добавить `COPY memory_server/celery_app.py` и `COPY memory_server/tasks/`
   - Добавить ENTRYPOINT для worker (опционально, через docker-compose override)

9. [ ] **Создать `memory_server/tasks/worker_init.py`**
   - Lazy init для embedding models при старте worker
   - Использовать `celery.signals.worker_process_init` для инициализации
   - Кэшировать EmbeddingClient, QdrantClient, asyncpg pool

### Проверка
- [ ] `celery -A memory_server.celery_app worker -Q memory_ops -l INFO` стартует без ошибок
- [ ] `celery -A memory_server.celery_app inspect ping` отвечает
- [ ] Flower доступен на `http://localhost:5555`
- [ ] Docker compose поднимает все сервисы

### Риски
- Redis может быть недоступен → healthcheck + `depends_on: condition: service_healthy`
- Конфликт портов (Flower 5555) → проверить, что порт свободен

### Откат
- Удалить сервисы из docker-compose.yml
- Откатить изменения в requirements.txt
- Удалить celery_app.py и tasks/

---

## Фаза 1: Инфраструктура задач (3 дня)

### Цель
Создать обёртку для Celery tasks с учётом специфики MCP tools: sync-интерфейс для клиента + async execution в workers.

### Ответственный: **Сона (Programmer)**
### Участники: **Эна (architect)**, **Нора (db-architect)**, **Афина (Team Lead)**

### Задачи по агентам

| Агент | Задачи |
|---|---|
| **Сона** | celery_app.py, tasks/__init__.py, memory_tasks.py, embed_tasks.py, batch_tasks.py, hash_tasks.py, serializers.py, errors.py |
| **Эна** | Архитектура task_bridge, паттерны retry, design base.py |
| **Нора** | Connection pool singleton (connections.py), оптимизация запросов, worker_init.py |
| **Афина** | Контроль, проверка что задачи регистрируются корректно |

### Точки согласования
- [ ] **День 1:** Эна ревьюит архитектуру base.py и connections.py перед реализацией
- [ ] **День 2:** Нора + Сона — pool создаётся при старте worker, метрики доступны
- [ ] **Конец фазы:** Афина — `celery inspect registered` показывает все задачи

### Шаги

1. [ ] **Создать `memory_server/tasks/base.py`**
   - Базовый класс задач с:
     - Lazy init: пул соединений создаётся при первом вызове, не при импорте
     - Автоматическое подключение к PG, Redis, Qdrant
     - Таймауты (hard: 300s, soft: 240s)
     - Retry с exponential backoff
     - Логирование через `celery.utils.log.get_task_logger`
     - Метрики через `prometheus_client`

2. [ ] **Создать `memory_server/tasks/memory_tasks.py`**
   - Задачи для `memory_ops` очереди:
     ```python
     @shared_task(name='memory.store', queue='memory_ops', bind=True)
     def store_memory(self, content, user_id, metadata, namespace, importance):
         ...

     @shared_task(name='memory.update', queue='memory_ops', bind=True)
     def update_memory(self, memory_id, content, metadata, importance):
         ...

     @shared_task(name='memory.delete', queue='memory_ops', bind=True)
     def delete_memory(self, memory_id):
         ...

     @shared_task(name='memory.archive', queue='memory_ops', bind=True)
     def archive_memory(self, memory_id):
         ...

     @shared_task(name='memory.link', queue='memory_ops', bind=True)
     def create_link(self, source_id, target_id, link_type, description, weight):
         ...

     @shared_task(name='memory.unlink', queue='memory_ops', bind=True)
     def delete_link(self, source_id, target_id, link_type):
         ...
     ```

3. [ ] **Создать `memory_server/tasks/embed_tasks.py`**
   - Задачи для `embed_ops` очереди:
     ```python
     @shared_task(name='memory.search', queue='embed_ops', bind=True)
     def search_memory(self, query, user_id, limit, threshold, namespace):
         ...

     @shared_task(name='memory.find_similar', queue='embed_ops', bind=True)
     def find_similar(self, content, user_id, limit, threshold, namespace):
         ...

     @shared_task(name='memory.traverse', queue='embed_ops', bind=True)
     def traverse_graph(self, start_id, depth, link_types):
         ...
     ```

4. [ ] **Создать `memory_server/tasks/batch_tasks.py`**
   - Задачи для `batch_ops` очереди:
     ```python
     @shared_task(name='memory.ingest_batch', queue='batch_ops', bind=True)
     def ingest_batch(self, entries, user_id):
         ...

     @shared_task(name='memory.forget', queue='batch_ops', bind=True)
     def forget_user(self, user_id, namespace=None):
         ...
     ```

5. [ ] **Создать `memory_server/tasks/hash_tasks.py`**
   - Задачи для хешей (в `memory_ops`):
     ```python
     @shared_task(name='hash.upsert', queue='memory_ops', bind=True)
     def upsert_hash(self, source_type, source_id, content_hash, size_bytes, metadata):
         ...

     @shared_task(name='hash.get', queue='memory_ops', bind=True)
     def get_hash(self, source_type, source_id):
         ...

     @shared_task(name='hash.list', queue='memory_ops', bind=True)
     def list_hashes(self, source_type, updated_since, project, limit, offset):
         ...

     @shared_task(name='hash.delete', queue='memory_ops', bind=True)
     def delete_hash(self, source_type, source_id):
         ...
     ```

6. [ ] **Создать `memory_server/tasks/connections.py`**
   - Singleton-подключения для workers:
     ```python
     _pool: asyncpg.Pool | None = None
     _qdrant: QdrantClient | None = None
     _embedding: EmbeddingClient | None = None

     def get_pool() -> asyncpg.Pool: ...
     def get_qdrant() -> QdrantClient | None: ...
     def get_embedding_client() -> EmbeddingClient: ...
     ```
   - Инициализация при старте worker через `celery.signals.worker_process_init`
   - Закрытие при остановке через `celery.signals.worker_process_shutdown`

7. [ ] **Создать `memory_server/tasks/serializers.py`**
   - JSON-сериализация Pydantic моделей (MemoryRecord → dict → JSON)
   - Десериализация: dict → Pydantic models
   - Обработка datetime, UUID

8. [ ] **Создать `memory_server/tasks/errors.py`**
   - Кастомные исключения для tasks:
     ```python
     class TaskValidationError(ValueError): ...
     class TaskTimeoutError(Exception): ...
     class TaskDependencyError(Exception): ...
     ```

### Проверка
- [ ] Задачи регистрируются: `celery -A memory_server.celery_app inspect registered`
- [ ] Задачи маршрутизируются в правильные очереди
- [ ] Connection pool создаётся при старте worker
- [ ] Логи worker содержат информацию о подключении к PG/Redis/Qdrant

### Риски
- asyncpg pool может истощиться → настроить `db_min_connections` / `db_max_connections`
- Qdrant sync client может блокировать prefork worker → использовать timeout
- Embedding model может не загрузиться → retry + graceful degradation

### Откат
- Удалить `memory_server/tasks/`
- Откатить `celery_app.py`

---

## Фаза 2: Миграция MCP Tools (5 дней)

### Цель
Переключить MCP tools с прямых вызовов service на Celery tasks. MCP tool должен вызвать task и получить результат.

### Ответственный: **Сона (Programmer)**
### Участники: **Эна (architect)**, **Катерина (tester)**, **Афина (Team Lead)**

### Задачи по агентам

| Агент | Задачи |
|---|---|
| **Сона** | task_bridge.py, миграция memory_tools.py, hash_tools.py, обновление server.py, task_results.py, __main__.py |
| **Эна** | Ревью архитектурных решений, контроль что tool остаётся async для клиента |
| **Катерина** | Smoke тесты после каждого tool (не полные — полные в Фазе 3) |
| **Афина** | Контроль, приоритезация, эскалация проблем |

### Точки согласования
- [ ] **День 1:** Эна ревьюит task_bridge.py — паттерн sync↔async одобрен
- [ ] **День 3:** Катерина — memory_store, memory_search, memory_get работают через Celery
- [ ] **День 4:** Сона — все 25 MCP tools переключены, Эна ревьюит
- [ ] **Конец фазы:** Афина — интеграционный smoke test, нет regressions

### Шаги

1. [ ] **Создать `memory_server/tools/task_bridge.py`**
   - Мост между sync MCP tools и async Celery tasks:
     ```python
     def run_task_sync(task_name: str, timeout: int = 60, **kwargs) -> Any:
         """Вызвать Celery task и дождаться результата (sync)."""
         result = app.send_task(task_name, kwargs=kwargs)
         return result.get(timeout=timeout)

     async def run_task_async(task_name: str, timeout: int = 60, **kwargs) -> Any:
         """Вызвать Celery task и дождаться результата (async)."""
         result = app.send_task(task_name, kwargs=kwargs)
         return await asyncio.to_thread(result.get, timeout=timeout)
     ```

2. [ ] **Модифицировать `memory_server/tools/memory_tools.py`**
   - Каждый tool вызывает Celery task вместо `service.*`:
     ```python
     @mcp.tool()
     async def memory_store(content, user_id, metadata, namespace, importance, ctx):
         result = await run_task_async(
             'memory.store',
             content=content, user_id=user_id,
             metadata=metadata, namespace=namespace,
             importance=importance,
         )
         return result
     ```
   - **Важно:** tool остаётся async для клиента, но выполняется через Celery
   - Таймаут tool = таймаут Celery task + запас

3. [ ] **Модифицировать `memory_server/tools/hash_tools.py`**
   - Аналогичная миграция для hash tools
   - ACL проверка остаётся на уровне tool (до отправки в Celery)

4. [ ] **Обновить `memory_server/server.py`**
   - Lifespan: убрать прямое создание service (сервис теперь в workers)
   - Lifespan: оставить только health-check pool для HTTP health endpoint
   - Или: создать lightweight service только для health-check

5. [ ] **Создать `memory_server/tools/task_results.py`**
   - Проверка статуса задач (для async режима):
     ```python
     @mcp.tool()
     async def task_status(task_id: str):
         result = AsyncResult(task_id, app=celery_app)
         return {"task_id": task_id, "status": result.state, "result": result.result}
     ```

6. [ ] **Обновить `memory_server/__main__.py`**
   - Добавить endpoint `/tasks/{task_id}` для проверки статуса задач
   - Добавить endpoint `/tasks` для списка активных задач

### Проверка
- [ ] `memory_store` сохраняет через Celery → запись в БД
- [ ] `memory_search` ищет через Celery → результат возвращается клиенту
- [ ] `memory_ingest_batch` обрабатывает батч через batch_ops очередь
- [ ] Все 25 MCP tools работают через Celery
- [ ] Таймауты работают корректно
- [ ] Retry при ошибках Redis/PG работает

### Риски
- **Критический:** MCP tool блокируется пока task не выполнится → таймаут 60s
  - Митигация: `task_soft_time_limit=240` < `tool_timeout=300`
- Сервис больше не создаёт pool в lifespan → health endpoint должен работать без pool
- Кэш эмбеддингов может не работать в worker context → проверить EmbeddingCache

### Откат
- Откатить `memory_tools.py` и `hash_tools.py` к прямым вызовам service
- Восстановить lifespan в `server.py`

---

## Фаза 3: Тесты и совместимость (3 дня)

### Цель
Убедиться, что все существующие тесты продолжают работать, и написать тесты для Celery tasks.

### Ответственный: **Катерина (Tester)**
### Участники: **Сона (Programmer)**, **Афина (Team Lead)**

### Задачи по агентам

| Агент | Задачи |
|---|---|
| **Катерина** | Unit-тесты tasks (CELERY_ALWAYS_EAGER), ~30-40 тестов, обновление conftest.py |
| **Сона** | Интеграционные тесты с Redis broker, тесты serializers |
| **Катерина** | Load тесты (100 параллельных операций), regression testing |
| **Афина** | Контроль, проверка покрытия ≥80% |

### Точки согласования
- [ ] **День 1:** Катерина — conftest.py с CELERY_ALWAYS_EAGER готов, unit-тесты запускаются
- [ ] **День 2:** Сона — интеграционные тесты с реальным Redis проходят
- [ ] **Конец фазы:** Афина — `pytest -v` все тесты зелёные, покрытие ≥80%

### Шаги

1. [ ] **Обновить `tests/conftest.py`**
   - Добавить fixture для mock Celery app:
     ```python
     @pytest.fixture
     def celery_app():
         from memory_server.celery_app import app
         app.conf.update(CELERY_ALWAYS_EAGER=True)
         return app
     ```
   - `CELERY_ALWAYS_EAGER=True` для unit-тестов (выполнение sync)

2. [ ] **Создать `tests/test_celery_tasks.py`**
   - Тесты для каждой задачи:
     - Happy path (успешное выполнение)
     - Error cases (невалидные данные, таймауты)
     - Retry behavior
     - Connection failures
   - ~30-40 тестов

3. [ ] **Обновить `tests/test_tools.py`**
   - Mock Celery tasks в tool тестах
   - Проверить что tools корректно вызывают tasks
   - Проверить таймауты и error handling

4. [ ] **Обновить `tests/test_service.py`**
   - Сервисные тесты остаются (тестируют business logic без Celery)
   - Добавить тесты для serializ/deserializ через JSON

5. [ ] **Создать `tests/test_serializers.py`**
   - Тесты JSON-сериализации Pydantic моделей
   - Edge cases: datetime, UUID, None, nested dicts

6. [ ] **Обновить `tests/test_config.py`**
   - Тесты для Celery-конфигурации
   - Проверка что настройки берутся из env

7. [ ] **Проверить все существующие тесты**
   - Запустить `pytest -v` — все 19 файлов тестов должны пройти
   - Убедиться что моки не сломаны

### Проверка
- [ ] `pytest tests/ -v` — все тесты проходят
- [ ] `pytest tests/test_celery_tasks.py -v` — новые тесты проходят
- [ ] Покрытие кода не ниже 80% для tasks

### Риски
- `CELERY_ALWAYS_EAGER` может вести себя иначе чем реальный worker → интеграционные тесты отдельно
- Моки asyncpg pool могут конфликтовать с Celery pool → изолировать

### Откат
- Удалить `tests/test_celery_tasks.py`, `tests/test_serializers.py`
- Откатить conftest.py

---

## Фаза 4: Мониторинг и метрики (2 дня)

### Цель
Настроить метрики Celery workers, интеграцию с Prometheus, дашборды.

### Ответственный: **Мая (Observability)**
### Участники: **Рэй (DevOps)**, **Афина (Team Lead)**

### Задачи по агентам

| Агент | Задачи |
|---|---|
| **Мая** | Prometheus метрики (athena_celery_*), signals.py, Grafana дашборд celery.json |
| **Рэй** | Настройка alerting в prometheus-rules.yml, healthcheck workers в docker-compose |
| **Мая** | HTTP endpoints (/tasks/{id}, /workers, /health) — если не в Фазе 5 |
| **Афина** | Контроль, проверка что метрики появляются в Prometheus |

### Точки согласования
- [ ] **День 1:** Мая — метрики celery_* экспортируются, Рэй — scraping настроен
- [ ] **Конец фазы:** Афина — Grafana дашборд отображает данные, алерты протестированы

### Шаги

1. [ ] **Обновить `memory_server/metrics.py`**
   - Добавить Celery-метрики:
     ```python
     CELERY_TASKS_TOTAL = Counter(
         "athena_celery_tasks_total",
         "Total Celery tasks executed",
         ["task", "queue", "status"],
     )

     CELERY_TASK_DURATION = Histogram(
         "athena_celery_task_duration_seconds",
         "Celery task execution duration",
         ["task", "queue"],
         buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
     )

     CELERY_TASK_RETRIES = Counter(
         "athena_celery_task_retries_total",
         "Total Celery task retries",
         ["task"],
     )

     CELERY_QUEUE_LENGTH = Gauge(
         "athena_celery_queue_length",
         "Approximate queue length",
         ["queue"],
     )
     ```

2. [ ] **Создать `memory_server/tasks/signals.py`**
   - Сигналы Celery для метрик:
     ```python
     from celery.signals import task_success, task_failure, task_retry

     @task_success.connect
     def handle_task_success(sender, **kwargs): ...

     @task_failure.connect
     def handle_task_failure(sender, **kwargs): ...

     @task_retry.connect
     def handle_task_retry(sender, **kwargs): ...
     ```

3. [ ] **Обновить `monitoring/alerts/prometheus-rules.yml`**
   - Добавить алерты Celery:
     ```yaml
     - name: celery
       interval: 30s
       rules:
         - alert: CeleryWorkerDown
           expr: athena_celery_workers_active == 0
           for: 2m
           labels:
             severity: critical
           annotations:
             summary: "Нет активных Celery workers"

         - alert: CeleryTaskFailureRateHigh
           expr: rate(athena_celery_tasks_total{status="FAILURE"}[5m]) > 0.1
           for: 5m
           labels:
             severity: warning
           annotations:
             summary: "High Celery task failure rate"

         - alert: CeleryTaskDurationHigh
           expr: histogram_quantile(0.95, rate(athena_celery_task_duration_seconds_bucket[5m])) > 60
           for: 5m
           labels:
             severity: warning
           annotations:
             summary: "P95 task duration > 60s"

         - alert: CeleryQueueBacklog
           expr: athena_celery_queue_length > 100
           for: 10m
           labels:
             severity: warning
           annotations:
             summary: "Celery queue backlog > 100 tasks"
     ```

4. [ ] **Создать `monitoring/dashboards/celery.json`**
   - Дашборд Grafana с панелями:
     - Task success/failure rate
     - Task duration (P50, P95, P99)
     - Queue length per queue
     - Active workers
     - Retry rate

5. [ ] **Обновить healthcheck в `docker-compose.yml`**
   - Health-check для workers:
     ```yaml
     healthcheck:
       test: ["CMD", "celery", "-A", "memory_server.celery_app", "inspect", "ping", "--timeout=5"]
       interval: 30s
       timeout: 10s
       retries: 3
     ```

### Проверка
- [ ] `curl http://localhost:8000/metrics` содержит `athena_celery_*` метрики
- [ ] Flower показывает активных workers
- [ ] Алерты срабатывают при模拟ной ошибке
- [ ] Grafana дашборд отображает данные

### Риски
- Метрики могут замедлить workers → sample rate, если нужно
- Prometheus может не собирать метрики workers → проверить scraping config

### Откат
- Удалить Celery-метрики из `metrics.py`
- Удалить `celery.json` дашборд
- Откатить prometheus-rules.yml

---

## Фаза 5: Интеграция с FastAPI (2 дня)

### Цель
Добавить HTTP endpoints для управления Celery tasks и интеграцию с существующим FastAPI app.

### Ответственный: **Сона (Programmer)**
### Участники: **Лита (security)**, **Афина (Team Lead)**

### Задачи по агентам

| Агент | Задачи |
|---|---|
| **Сона** | APIRouter tasks.py, endpoints /tasks/{id}, /tasks, /workers, /health |
| **Лита** | Аудит безопасности endpoints (auth, rate limiting, information disclosure) |
| **Сона** | Интеграция с existing FastMCP, celery_health.py |
| **Афина** | Контроль, финальное согласование |

### Точки согласования
- [ ] **День 1:** Сона — эндпоинты работают, Лита — аудит пройден
- [ ] **Конец фазы:** Афина — все endpoints отвечают, auth работает

### Шаги

1. [ ] **Создать `memory_server/api/tasks.py`**
   - APIRouter для управления задачами:
     ```python
     router = APIRouter(prefix="/tasks", tags=["tasks"])

     @router.get("/{task_id}")
     async def get_task_status(task_id: str): ...

     @router.get("/")
     async def list_active_tasks(): ...

     @router.post("/{task_id}/cancel")
     async def cancel_task(task_id: str): ...

     @router.get("/queues")
     async def get_queue_lengths(): ...
     ```

2. [ ] **Обновить `memory_server/__main__.py`**
   - Подключить `tasks` router
   - Добавить endpoint `/workers` для статуса workers:
     ```python
     @app.get("/workers")
     async def workers_status():
         inspector = app.celery_app.control.inspect(timeout=5.0)
         return inspector.ping()
     ```

3. [ ] **Обновить healthcheck**
   - Расширить `/health` endpoint:
     ```python
     @app.get("/health")
     async def health():
         checks = {
             "config": ...,
             "celery": check_celery_health(),
             "redis": check_redis_health(),
         }
     ```
   - Проверка Celery health через `celery.control.ping()`

4. [ ] **Создать `memory_server/tasks/celery_health.py`**
   - Проверка здоровья Celery:
     ```python
     def check_celery_health() -> dict:
         inspector = app.control.inspect(timeout=3.0)
         active = inspector.active()
         ping = inspector.ping()
         return {
             "ping": ping is not None,
             "workers": len(active) if active else 0,
         }
     ```

### Проверка
- [ ] `GET /tasks/{task_id}` возвращает статус задачи
- [ ] `GET /tasks` возвращает список активных задач
- [ ] `POST /tasks/{task_id}/cancel` отменяет задачу
- [ ] `GET /health` содержит celery check
- [ ] `GET /workers` возвращает список workers

### Риски
- `celery.control.inspect()` может быть медленным → timeout=3s
- Отмена задачи может не сработать если task уже выполняется → acks_late=True

### Откат
- Удалить `api/tasks.py`
- Откатить `__main__.py`

---

## Фаза 6: Docker и деплой (2 дня)

### Цель
Финализировать Docker-конфигурацию, обновить deploy script, проверить production-ready.

### Ответственный: **Рэй (DevOps)**
### Участники: **Лита (security)**, **Тиамат (tech-writer)**, **Афина (Team Lead)**

### Задачи по агентам

| Агент | Задачи |
|---|---|
| **Рэй** | Production docker-compose.prod.yml, rolling deploy, scripts/celery_management.sh |
| **Лита** | Аудит secrets, credentials, network security, exposed ports |
| **Тиамат** | Документация deployment process, README обновление |
| **Афина** | Финальное согласование с Милордом перед production |

### Точки согласования
- [ ] **День 1:** Рэй — docker-compose.prod.yml готов, Лита — secrets audit пройден
- [ ] **Конец фазы:** Афина — `docker compose up -d` поднимает все сервисы, healthcheck'и зелёные

### Шаги

1. [ ] **Обновить `Dockerfile`**
   - Мульти-билд: builder + runtime
   - Копировать `celery_app.py`, `tasks/`
   - Добавить `celery` в PATH
   - Отдельный ENTRYPOINT для workers (через CMD override):
     ```dockerfile
     # Default: run MCP server
     ENTRYPOINT ["uvicorn", "memory_server.__main__:app", ...]

     # Override for worker: docker run ... CMD ["celery", "-A", "memory_server.celery_app", "worker", ...]
     ```

2. [ ] **Обновить `docker-compose.yml`**
   - Финальная конфигурация со всеми сервисами
   - Зависимости: memory-server → postgres, redis; workers → redis
   - Resource limits для каждого worker
   - Health checks для всех сервисов
   - Logging driver (json-file с rotation)

3. [ ] **Создать `docker-compose.prod.yml`**
   - Production override:
     - Без `local-db` profile (PostgreSQL внешний)
     - Без exposed ports (кроме memory-server)
     - Resource limits по памяти/CPU
     - Restart policies

4. [ ] **Обновить `deploy.sh`**
   - Добавить деплой workers:
     ```bash
     # Pull and restart workers
     docker compose -f docker-compose.prod.yml up -d celery-memory-worker celery-embed-worker celery-batch-worker flower
     ```
   - Rolling update для workers (по очереди)
   - Health-check после каждого worker

5. [ ] **Создать `scripts/celery_management.sh`**
   - Утилиты для управления:
     ```bash
     # Статус
     ./scripts/celery_management.sh status

     # Очистка очереди
     ./scripts/celery_management.sh purge memory_ops

     # Graceful shutdown
     ./scripts/celery_management.sh shutdown

     # Мониторинг
     ./scripts/celery_management.sh inspect
     ```

6. [ ] **Проверить `.env` на production**
   - Убедиться что secrets не в коде
   - Redis password настроен
   - PG password настроен

### Проверка
- [ ] `docker compose up -d` поднимает все сервисы
- [ ] Все healthchecks проходят
- [ ] `docker compose logs celery-memory-worker` — worker стартует
- [ ] `docker compose logs flower` — Flower работает
- [ ] Deploy script работает без ошибок

### Риски
- Workers могут потребовать больше памяти чем MCP server → настроить limits
- Prefork pool может fork() слишком много процессов → `worker_max_tasks_per_child=1000`

### Откат
- `docker compose down` → `docker compose up -d` с предыдущим docker-compose.yml
- Откатить deploy.sh

---

## Фаза 7: Оптимизация и финализация (2 дня)

### Цель
Оптимизировать производительность, убрать legacy code, написать документацию.

### Ответственный: **Сона (Programmer)**
### Участники: **Катерина (tester)**, **Тиамат (tech-writer)**, **Афина (Team Lead)**

### Задачи по агентам

| Агент | Задачи |
|---|---|
| **Сона** | Cleanup legacy code, performance benchmark, оптимизация connection pool |
| **Катерина** | Regression testing, stress testing (24h soak test) |
| **Тиамат** | README, architecture doc, runbooks, CHANGELOG |
| **Афина** | Финальный отчёт Милорду, закрытие миграции |

### Точки согласования
- [ ] **День 1:** Сona — benchmark P95 < 2s для memory_ops, Катерина — regression clean
- [ ] **Конец фазы:** Афина — финальный `pytest`, документация актуальна, отчёт Милорду

### Шаги

1. [ ] **Оптимизация connection pool**
   - Настроить `worker_max_tasks_per_child=1000` (предотвращение memory leak)
   - Настроить `worker_max_memory_per_child=200000` (200MB в KB)
   - Мониторить usage через метрики

2. [ ] **Убрать legacy sync код**
   - Если MCP tools полностью переключены на Celery:
     - Убрать прямые вызовы `service.*` из tools
     - Убрать `MemoryService` из `server.py` lifespan (если не нужен)
     - Оставить `MemoryService` в `tasks/` для worker context

3. [ ] **Обновить документацию**
   - README.md: обновить секцию установки/конфигурации
   - Добавить документацию по Celery tasks
   - Описать очереди и маршрутизацию

4. [ ] **Финальный `pytest` прогон**
   - Убедиться что все тесты проходят
   - Проверить покрытие

5. [ ] **Performance testing**
   - Замерить latency: MCP tool → Celery task → результат
   - Сравнить с baseline (до Celery)
   - Убедиться что P95 < 2s для memory_ops

6. [ ] **Cleanup**
   - Удалить `celery_docs_summary.md` (документация в README)
   - Обновить VERSION
   - Обновить CHANGELOG

### Проверка
- [ ] Все тесты проходят
- [ ] Latency P95 < 2s для memory_ops
- [ ] Latency P95 < 5s для embed_ops
- [ ] No memory leaks в workers (мониторинг 24h)
- [ ] Документация актуальна

### Риски
- Удаление legacy кода может сломать что-то → incremental удаление с тестами
- Performance regression → benchmark before/after

### Откат
- Git revert коммитов оптимизации

---

## Итого: Оценка времени

| Фаза | Дни | Зависит от | Ответственный |
|------|-----|-----------|---------------|
| Фаза 0: Подготовка инфраструктуры | 2 | — | **Рэй** |
| Фаза 1: Инфраструктура задач | 3 | Фаза 0 | **Сона** |
| Фаза 2: Миграция MCP Tools | 5 | Фаза 1 | **Сона** |
| Фаза 3: Тесты и совместимость | 3 | Фаза 2 | **Катерина** |
| Фаза 4: Мониторинг и метрики | 2 | Фаза 1 | **Мая** |
| Фаза 5: Интеграция с FastAPI | 2 | Фаза 2 | **Сона** |
| Фаза 6: Docker и деплой | 2 | Фаза 2, 4, 5 | **Рэй** |
| Фаза 7: Оптимизация | 2 | Фаза 3, 6 | **Сона** |
| **ИТОГО** | **21 день** | | |

### Критический путь
```
Фаза 0 → Фаза 1 → Фаза 2 → Фаза 3 → Фаза 7
                     ↓
                  Фаза 4 → Фаза 6
                     ↓
                  Фаза 5 → Фаза 6
```

### Параллельные задачи
- Фаза 4 (мониторинг) может идти параллельно с Фазой 2 (миграция tools)
- Фаза 5 (FastAPI) может идти параллельно с Фазой 3 (тесты)

---

## Структура файлов после миграции

```
selti/
├── memory_server/
│   ├── __init__.py
│   ├── __main__.py          # FastAPI app (обновлён)
│   ├── celery_app.py         # НОВЫЙ: Celery instance
│   ├── config.py             # Обновлён: Celery settings
│   ├── exceptions.py         # Без изменений
│   ├── logger.py             # Без изменений
│   ├── metrics.py            # Обновлён: Celery метрики
│   ├── models.py             # Без изменений
│   ├── server.py             # Обновлён: убран service из lifespan
│   ├── cache/
│   │   └── redis_client.py   # Без изменений
│   ├── db/
│   │   ├── pool.py           # Без изменений
│   │   └── queries.py        # Без изменений
│   ├── embedding/
│   │   ├── client.py         # Без изменений
│   │   └── provider.py       # Без изменений
│   ├── memory/
│   │   ├── dedup.py          # Без изменений
│   │   ├── repository.py     # Без изменений
│   │   ├── repository_qdrant.py  # Без изменений
│   │   ├── service.py        # Без изменений (используется в workers)
│   │   └── ...
│   ├── tasks/                # НОВЫЙ: Celery tasks
│   │   ├── __init__.py
│   │   ├── celery_config.py  # Очереди, Exchange, routing
│   │   ├── base.py           # Базовый класс задач
│   │   ├── connections.py    # Singleton connections
│   │   ├── memory_tasks.py   # Задачи памяти
│   │   ├── embed_tasks.py    # Задачи эмбеддингов
│   │   ├── batch_tasks.py    # Батч-задачи
│   │   ├── hash_tasks.py     # Задачи хешей
│   │   ├── signals.py        # Celery signals для метрик
│   │   ├── serializers.py    # JSON serialization
│   │   ├── errors.py         # Кастомные исключения
│   │   └── worker_init.py    # Lazy init для workers
│   ├── tools/
│   │   ├── memory_tools.py   # Обновлён: вызовы через Celery
│   │   ├── hash_tools.py     # Обновлён: вызовы через Celery
│   │   ├── task_bridge.py    # НОВЫЙ: мост sync↔async
│   │   └── task_results.py   # НОВЫЙ: проверка статуса задач
│   └── vector/
│       └── qdrant_store.py   # Без изменений
├── tests/
│   ├── conftest.py           # Обновлён: Celery fixtures
│   ├── test_celery_tasks.py  # НОВЫЙ
│   ├── test_serializers.py   # НОВЫЙ
│   └── ... (существующие тесты обновлены)
├── monitoring/
│   ├── alerts/
│   │   └── prometheus-rules.yml  # Обновлён: Celery алерты
│   └── dashboards/
│       ├── postgres-pgvector.json
│       └── celery.json       # НОВЫЙ
├── docker-compose.yml        # Обновлён: workers, flower
├── docker-compose.prod.yml   # НОВЫЙ: production
├── Dockerfile                # Обновлён: workers
├── requirements.txt          # Обновлён: celery, flower
├── deploy.sh                 # Обновлён: workers deploy
├── scripts/
│   └── celery_management.sh  # НОВЫЙ: management utils
└── celery_migration_plan.md  # НОВЫЙ: этот файл
```

---

## Распределение ролей: сводная таблица

| Агент | Фазы | Общая нагрузка |
|---|---|---|
| **Афина** | 0, 1, 2, 3, 4, 5, 6, 7 | Контроль всех фаз (8/8) |
| **Сона** | 1, 2, 3, 5, 7 | Основной исполнитель (5 фаз) |
| **Рэй** | 0, 4, 6 | Инфраструктура (3 фазы) |
| **Эна** | 0, 1, 2 | Архитектура (3 фазы) |
| **Катерина** | 2, 3, 7 | Тестирование (3 фазы) |
| **Мая** | 4 | Мониторинг (1 фаза) |
| **Нора** | 1 | DB (1 фаза) |
| **Лита** | 5, 6 | Безопасность (2 фазы) |
| **Тиамат** | 6, 7 | Документация (2 фазы) |
| **Момо** | — | План (этот документ) |

---

## Ключевые принципы миграции

1. **Incremental** — каждая фаза тестируется отдельно, не всё сразу
2. **Rollback-ready** — каждую фазу можно откатить без потери данных
3. **Non-breaking** — MCP API не меняется для клиентов
4. **Observable** — метрики и логи для каждого шага
5. **Testable** — `CELERY_ALWAYS_EAGER` для unit-тестов

---

*План составлен 31.07.2026 Момо (Planner)*
