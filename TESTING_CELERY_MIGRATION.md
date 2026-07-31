# План тестирования Celery миграции — selti

**Дата:** 31.07.2026
**Автор:** Катерина (Tester)
**Статус:** Draft

---

## 1. Анализ текущего состояния

### 1.1 Текущий тест-сьют
- **16 тестовых файлов**, ~130 тестов
- Все на `pytest-asyncio` с моками `asyncpg.Pool` и embedding provider
- `conftest.py`: фикстуры `mock_pool`, `mock_repository`, `mock_service`, `mock_embedding_provider`
- Паттерн: tools тестируются через `mock_ctx` с `lifespan_context["service"]`

### 1.2 Ключевые архитектурные изменения
**До миграции:**
```
MCP Tool → ctx.request_context.lifespan_context["service"] → Repository → PG
```

**После миграции:**
```
MCP Tool → task_bridge.run_task_async() → Celery Task → Worker → Repository → PG
```

### 1.3 Что нужно протестировать
1. **Unit-тесты Celery tasks** — каждая task отдельно
2. **Обновлённые tool тесты** — tools теперь вызывают tasks
3. **Serializers** — JSON serialization Pydantic моделей
4. **task_bridge** — мост sync↔async
5. **Regression** — существующие тесты не сломаны
6. **Integration** — с реальным Redis broker
7. **Load** — parallel workers

---

## 2. Ответы на вопросы

### 2.1 CELERY_ALWAYS_EAGER и async tasks

**Проблема:** План использует `CELERY_ALWAYS_EAGER=True` (устаревший синтаксис Celery 3.x).

**Правильное решение для Celery 5.x:**
```python
app.conf.update(task_always_eager=True)
```

**Async tasks — особенности:**
- `task_always_eager=True` выполняет задачи **синхронно в том же процессе**
- Для sync задач (prefork pool) — работает идеально
- Для async задач — **не рекомендуется** в eager mode, так как eager выполнение не запускает event loop
- **Решение:** Все Celery tasks в selti будут **sync** (prefork pool = sync workers). Async обёртка остаётся на уровне `task_bridge.py`, не в самих tasks

**Нужен ли pytest-celery?**
- `pytest-celery` — полезен для integration тестов (поднимает тестовый broker)
- Для unit-тестов достаточно `task_always_eager=True`
- **Рекомендация:** Добавить `pytest-celery>=1.0.0` в dev-зависимости для integration тестов

### 2.2 Mock strategy для Celery tasks в tool тестах

**Текущий паттерн:**
```python
@pytest.fixture
def mock_service():
    service = MagicMock()
    service.store = AsyncMock()
    # ...
```

**После миграции — два подхода:**

**Подход A: Mock task_bridge (рекомендуется для unit-тестов tools)**
```python
@pytest.fixture
def mock_task_bridge():
    with patch("memory_server.tools.task_bridge.run_task_async") as mock:
        mock.return_value = {"id": "mem-1", "content": "test"}
        yield mock

# В тесте:
async def test_memory_store(mock_ctx, mock_task_bridge):
    result = await memory_store(content="test", user_id="u1", ctx=mock_ctx)
    mock_task_bridge.assert_called_once_with(
        "memory.store",
        content="test", user_id="u1",
        metadata=None, namespace=None, importance=None,
    )
```

**Подход B: task_always_eager + mock repository (для интеграции tool→task)**
```python
@pytest.fixture
def celery_app():
    from memory_server.celery_app import app
    app.conf.update(task_always_eager=True)
    return app

# Tasks выполняются sync, но repository мокается
```

**Рекомендация:** Использовать **оба подхода**:
- Подход A — для быстрых unit-тестов (проверяем что tool вызывает task с правильными аргументами)
- Подход B — для integration тестов (проверяем что task вызывает repository правильно)

### 2.3 Integration tests с реальным Redis broker

**Стратегия:**
```python
# tests/conftest.py
@pytest.fixture(scope="session")
def celery_config():
    """Конфигурация для integration тестов с реальным Redis."""
    return {
        "broker_url": os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15"),  # отдельная БД
        "result_backend": os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15"),
        "task_always_eager": False,  # РЕАЛЬНЫЙ broker!
    }

@pytest.fixture(scope="session")
def celery_app(celery_config):
    from memory_server.celery_app import app
    app.conf.update(**celery_config)
    return app
```

**Требования:**
- Docker Compose с Redis для тестов (отдельный порт/БД)
- Тестовая БД PostgreSQL (отдельная от dev)
- Тесты в `tests/integration/` (отдельная директория)
- Маркер `@pytest.mark.integration` для запуска только в CI

**Что тестировать:**
1. Task реально отправляется в Redis и выполняется worker'ом
2. Result store работает (AsyncResult)
3. Retry при недоступности Redis
4. Таймауты (task_time_limit, task_soft_time_limit)
5. Очереди (memory_ops, embed_ops, batch_ops) — routing

### 2.4 Load testing стратегия

**Инструменты:**
- `locust` или `pytest-benchmark` для нагрузочных тестов
- `asyncio.Semaphore` для контроля параллелизма

**Сценарии:**
```python
# tests/load/test_parallel_ops.py
import asyncio
import time
import pytest

@pytest.mark.load
class TestParallelOperations:
    async def test_100_concurrent_memory_store(self, celery_app):
        """100 параллельных memory_store операций."""
        from memory_server.tools.task_bridge import run_task_async

        start = time.monotonic()
        tasks = [
            run_task_async("memory.store", content=f"test-{i}", user_id="load-test")
            for i in range(100)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        duration = time.monotonic() - start

        errors = [r for r in results if isinstance(r, Exception)]
        assert len(errors) == 0, f"Errors: {errors}"
        assert duration < 30, f"Too slow: {duration:.1f}s"

    async def test_mixed_workload(self, celery_app):
        """Смешанная нагрузка: store + search + delete."""
        operations = []
        for i in range(50):
            operations.append(run_task_async("memory.store", content=f"load-{i}", user_id="lt"))
            if i % 5 == 0:
                operations.append(run_task_async("memory.search", query=f"load-{i}", user_id="lt"))
            if i % 10 == 0:
                operations.append(run_task_async("memory.delete", memory_id=f"load-{i-1}"))

        results = await asyncio.gather(*operations, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(errors) / len(operations) < 0.01  # <1% error rate
```

**Метрики для замера:**
- Latency P50/P95/P99 для каждой операции
- Throughput (ops/sec)
- Error rate
- Worker utilization (CPU, memory)
- Queue depth under load

### 2.5 Regression testing

**Стратегия:**
1. **Snapshot testing** — зафиксировать результаты текущих тестов
2. **Построчный diff** — каждый обновлённый тест должен сохранять поведение
3. **Автоматический запуск** — CI запускает все тесты после каждого коммита

**Конкретные шаги:**
```bash
# 1. Записать baseline
pytest tests/ -v --tb=short > tests/baseline_results.txt

# 2. После миграции — сравнить
pytest tests/ -v --tb=short > tests/post_migration_results.txt

# 3. Проверить что все тесты проходят
diff tests/baseline_results.txt tests/post_migration_results.txt
```

**Какие тесты требуют обновления:**
| Файл | Что менять | Причина |
|------|-----------|---------|
| `test_tools.py` | Mock service → mock task_bridge | Tools теперь вызывают tasks |
| `test_service.py` | **БЕЗ ИЗМЕНЕНИЙ** | Service тестируется напрямую, без Celery |
| `test_config.py` | Добавить Celery settings | Новые env vars |
| `test_hash.py` | Mock task_bridge | Hash tools мигрируются на Celery |
| `conftest.py` | Добавить Celery fixtures | `celery_app`, `celery_worker` |

**Какие тесты НЕ трогаем:**
- `test_repository.py` — repository layer без изменений
- `test_models.py` — модели без изменений
- `test_dedup.py` — dedup logic без изменений
- `test_embedding.py` — embedding без изменений
- `test_cache.py` — cache без изменений
- `test_health.py` — health проверки (обновить если добавится celery health)
- `test_metrics.py` — добавить celery метрики
- `test_posix_logging.py` — logging без изменений
- `test_exceptions.py` — exceptions без изменений
- `test_relations_archive.py` — relations без изменений

### 2.6 Coverage для tasks

**Целевое покрытие:** ≥80% для tasks модулей

**Что покрывать:**
| Модуль | Мин. покрытие | Критические сценарии |
|--------|--------------|---------------------|
| `tasks/memory_tasks.py` | 90% | Happy path, validation, retry, timeout |
| `tasks/embed_tasks.py` | 90% | Happy path, embed failure, timeout |
| `tasks/batch_tasks.py` | 85% | Batch processing, partial failure |
| `tasks/hash_tasks.py` | 85% | CRUD, validation |
| `tasks/base.py` | 80% | Error handling, retry logic |
| `tasks/connections.py` | 80% | Singleton init, connection failure |
| `tasks/serializers.py` | 95% | All Pydantic models, edge cases |
| `tasks/errors.py` | 100% | Кастомные исключения |
| `tools/task_bridge.py` | 90% | Sync/async, timeout, error propagation |
| `tools/task_results.py` | 80% | Status check, result retrieval |

---

## 3. Конкретные тесты

### 3.1 Обновление conftest.py

```python
# tests/conftest.py — добавки

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ... существующие фикстуры ...

# ── Celery fixtures ──

@pytest.fixture(scope="session")
def celery_env():
    """Переменные окружения для Celery."""
    return {
        "CELERY_BROKER_URL": os.getenv("TEST_CELERY_BROKER", "redis://localhost:6379/15"),
        "CELERY_RESULT_BACKEND": os.getenv("TEST_CELERY_BACKEND", "redis://localhost:6379/15"),
    }


@pytest.fixture
def celery_app(celery_env):
    """Celery app в eager mode для unit-тестов."""
    with patch.dict(os.environ, celery_env):
        from memory_server.celery_app import app
        app.conf.update(
            task_always_eager=True,
            task_eager_propagates=True,  # Пробрасывать исключения
        )
        yield app
        # Сброс после теста
        app.conf.update(task_always_eager=False)


@pytest.fixture
def mock_celery_task():
    """Мок для Celery task — возвращает результат без реального выполнения."""
    def _make_mock(task_name: str, return_value=None, side_effect=None):
        mock = MagicMock()
        mock.name = task_name
        mock.delay = MagicMock(return_value=MagicMock(
            id="test-task-id",
            state="SUCCESS",
            result=return_value,
            get=MagicMock(return_value=return_value),
        ))
        mock.apply_async = mock.delay
        if side_effect:
            mock.delay.side_effect = side_effect
        return mock
    return _make_mock


@pytest.fixture
def mock_task_bridge():
    """Мок для task_bridge — имитирует вызов Celery task."""
    with patch("memory_server.tools.task_bridge.run_task_async") as mock:
        yield mock


@pytest.fixture
def mock_task_bridge_sync():
    """Мок для sync версии task_bridge."""
    with patch("memory_server.tools.task_bridge.run_task_sync") as mock:
        yield mock


@pytest.fixture
def mock_redis():
    """Мок для Redis broker."""
    redis_mock = AsyncMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.set = AsyncMock()
    redis_mock.get = AsyncMock()
    redis_mock.delete = AsyncMock()
    return redis_mock
```

### 3.2 Новые тесты: tests/test_celery_tasks.py

```python
# tests/test_celery_tasks.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from memory_server.celery_app import app


class TestCeleryAppConfig:
    """Тесты конфигурации Celery app."""

    def test_celery_app_exists(self):
        """Celery app создаётся корректно."""
        assert app is not None
        assert app.main == "memory_server"

    def test_eager_mode_configurable(self, celery_app):
        """task_always_eager конфигурируется."""
        assert celery_app.conf.task_always_eager is True

    def test_task_time_limits(self, celery_app):
        """Таймауты установлены."""
        assert celery_app.conf.task_time_limit == 300
        assert celery_app.conf.task_soft_time_limit == 240

    def test_task_acks_late(self, celery_app):
        """acks_late включён для надёжности."""
        assert celery_app.conf.task_acks_late is True

    def test_serialization_is_json(self, celery_app):
        """Сериализация — только JSON."""
        assert "json" in celery_app.conf.accept_content
        assert celery_app.conf.result_serializer == "json"


# ── Memory Tasks ──

class TestMemoryStoreTask:
    """Тесты memory.store task."""

    @pytest.mark.asyncio
    async def test_store_success(self, celery_app):
        """Успешное сохранение."""
        with patch("memory_server.tasks.memory_tasks.get_pool") as mock_pool, \
             patch("memory_server.tasks.memory_tasks.get_embedding_client") as mock_emb:
            # Настройка моков
            conn = AsyncMock()
            pool = AsyncMock()
            pool.acquire = MagicMock()
            pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
            pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_pool.return_value = pool
            mock_emb.return_value.embed = AsyncMock(return_value=[0.1, 0.2])

            conn.fetchval = AsyncMock(return_value="new-id")
            conn.fetchrow = AsyncMock(return_value={
                "id": "new-id",
                "user_id": "u1",
                "content": "test",
                "metadata": {},
                "namespace": "default",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            })

            from memory_server.tasks.memory_tasks import store_memory
            result = store_memory(
                content="test",
                user_id="u1",
                metadata=None,
                namespace="default",
                importance=3,
            )

            assert result["id"] == "new-id"

    @pytest.mark.asyncio
    async def test_store_validation_empty_content(self, celery_app):
        """Пустой content → ошибка валидации."""
        from memory_server.tasks.memory_tasks import store_memory
        with pytest.raises(ValueError, match="content"):
            store_memory(content="", user_id="u1")


class TestMemorySearchTask:
    """Тесты memory.search task."""

    @pytest.mark.asyncio
    async def test_search_success(self, celery_app):
        """Успешный поиск."""
        with patch("memory_server.tasks.embed_tasks.get_pool") as mock_pool, \
             patch("memory_server.tasks.embed_tasks.get_embedding_client") as mock_emb:
            conn = AsyncMock()
            pool = AsyncMock()
            pool.acquire = MagicMock()
            pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
            pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_pool.return_value = pool
            mock_emb.return_value.embed = AsyncMock(return_value=[0.1, 0.2])
            conn.fetch = AsyncMock(return_value=[
                {"id": "1", "content": "match", "metadata": {}, "score": 0.95,
                 "namespace": "default", "created_at": datetime.now(timezone.utc),
                 "updated_at": datetime.now(timezone.utc)}
            ])

            from memory_server.tasks.embed_tasks import search_memory
            result = search_memory(
                query="test query",
                user_id="u1",
                limit=10,
                threshold=0.7,
                namespace=None,
            )
            assert len(result) >= 1


class TestMemoryDeleteTask:
    """Тесты memory.delete task."""

    @pytest.mark.asyncio
    async def test_delete_success(self, celery_app):
        """Успешное удаление."""
        with patch("memory_server.tasks.memory_tasks.get_pool") as mock_pool:
            conn = AsyncMock()
            pool = AsyncMock()
            pool.acquire = MagicMock()
            pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
            pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_pool.return_value = pool
            conn.execute = AsyncMock(return_value="DELETE 1")

            from memory_server.tasks.memory_tasks import delete_memory
            result = delete_memory(memory_id="mem-1")
            assert result is True


# ── Hash Tasks ──

class TestHashUpsertTask:
    """Тесты hash.upsert task."""

    @pytest.mark.asyncio
    async def test_upsert_success(self, celery_app):
        """Успешный upsert хеша."""
        with patch("memory_server.tasks.hash_tasks.get_pool") as mock_pool:
            conn = AsyncMock()
            pool = AsyncMock()
            pool.acquire = MagicMock()
            pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
            pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_pool.return_value = pool
            conn.fetchrow = AsyncMock(return_value={
                "id": 1,
                "source_type": "file",
                "source_id": "test.py",
                "content_hash": "a" * 64,
                "size_bytes": 1024,
                "metadata": None,
                "project": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            })

            from memory_server.tasks.hash_tasks import upsert_hash
            result = upsert_hash(
                source_type="file",
                source_id="test.py",
                content_hash="a" * 64,
                size_bytes=1024,
                metadata=None,
            )
            assert result["content_hash"] == "a" * 64

    @pytest.mark.asyncio
    async def test_upsert_invalid_hash_format(self, celery_app):
        """Невалидный формат хеша → ошибка."""
        from memory_server.tasks.hash_tasks import upsert_hash
        with pytest.raises(ValueError, match="Invalid content_hash"):
            upsert_hash(
                source_type="file",
                source_id="test.py",
                content_hash="not-a-hash",
            )


# ── Serializers ──

class TestSerializers:
    """Тесты JSON-сериализации Pydantic моделей."""

    def test_memory_record_roundtrip(self):
        """MemoryRecord → dict → JSON → dict → MemoryRecord."""
        from memory_server.tasks.serializers import serialize_record, deserialize_record
        from memory_server.models import MemoryRecord

        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="test-id",
            user_id="u1",
            content="Hello",
            metadata={"key": "value"},
            namespace="default",
            created_at=now,
            updated_at=now,
        )

        data = serialize_record(record)
        restored = deserialize_record(data)

        assert restored.id == record.id
        assert restored.content == record.content
        assert restored.metadata == record.metadata

    def test_serialize_none_values(self):
        """Обработка None значений."""
        from memory_server.tasks.serializers import serialize_record
        from memory_server.models import MemoryRecord

        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="test-id",
            user_id="u1",
            content="test",
            metadata=None,
            namespace="default",
            created_at=now,
            updated_at=now,
        )

        data = serialize_record(record)
        assert data["metadata"] is None


# ── Connections ──

class TestConnections:
    """Тесты singleton подключений."""

    def test_singleton_pattern(self):
        """Pool создаётся один раз."""
        from memory_server.tasks.connections import _pool_reset, get_pool

        _pool_reset()  # Сброс
        with patch("memory_server.tasks.connections._create_pool") as mock_create:
            mock_pool = MagicMock()
            mock_create.return_value = mock_pool

            pool1 = get_pool()
            pool2 = get_pool()

            assert pool1 is pool2
            mock_create.assert_called_once()

    def test_pool_reset(self):
        """Сброс pool создаёт новый."""
        from memory_server.tasks.connections import _pool_reset, get_pool

        _pool_reset()
        with patch("memory_server.tasks.connections._create_pool") as mock_create:
            mock_create.return_value = MagicMock()
            pool1 = get_pool()

            _pool_reset()
            mock_create.return_value = MagicMock()
            pool2 = get_pool()

            assert pool1 is not pool2
```

### 3.3 Обновлённые tool тесты

```python
# tests/test_tools.py — обновления

# Старый fixture (УДАЛИТЬ или оставить для legacy):
# @pytest.fixture
# def mock_service():
#     ...

# Новый fixture для post-migration:
@pytest.fixture
def mock_task_bridge():
    """Мок task_bridge для тестирования tools после миграции на Celery."""
    with patch("memory_server.tools.task_bridge.run_task_async") as mock:
        yield mock


class TestMemoryStoreCelery:
    """memory_store через Celery."""

    @pytest.mark.asyncio
    async def test_store_calls_celery_task(self, mock_ctx, mock_task_bridge):
        """Tool вызывает Celery task с правильными аргументами."""
        mock_task_bridge.return_value = {
            "id": "new-id",
            "content": "test",
            "_dedup_action": "insert",
        }

        result = await memory_store(
            content="test",
            user_id="u1",
            namespace="default",
            ctx=mock_ctx,
        )

        mock_task_bridge.assert_called_once_with(
            "memory.store",
            content="test",
            user_id="u1",
            metadata=None,
            namespace="default",
            importance=None,
        )
        assert result["id"] == "new-id"

    @pytest.mark.asyncio
    async def test_store_propagates_task_error(self, mock_ctx, mock_task_bridge):
        """Ошибка Celery task пробрасывается в tool."""
        mock_task_bridge.side_effect = RuntimeError("Celery task failed")

        with pytest.raises(RuntimeError, match="Celery task failed"):
            await memory_store(content="test", user_id="u1", ctx=mock_ctx)


class TestMemorySearchCelery:
    """memory_search через Celery."""

    @pytest.mark.asyncio
    async def test_search_calls_celery_task(self, mock_ctx, mock_task_bridge):
        """Tool вызывает memory.search task."""
        mock_task_bridge.return_value = [
            {"id": "1", "content": "match", "score": 0.95}
        ]

        result = await memory_search(
            query="test",
            user_id="u1",
            ctx=mock_ctx,
        )

        mock_task_bridge.assert_called_once()
        call_args = mock_task_bridge.call_args
        assert call_args[0][0] == "memory.search"  # task name


class TestHashToolsCelery:
    """Hash tools через Celery."""

    @pytest.mark.asyncio
    async def test_hash_upsert_calls_celery(self, mock_ctx, mock_task_bridge):
        """hash_upsert вызывает Celery task."""
        mock_task_bridge.return_value = {
            "id": 1,
            "content_hash": "a" * 64,
        }

        result = await hash_upsert(
            source_type="file",
            source_id="test.py",
            content_hash="a" * 64,
            ctx=mock_ctx,
        )

        mock_task_bridge.assert_called_once()
```

### 3.4 Tests/test_task_bridge.py

```python
# tests/test_task_bridge.py

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio


class TestRunTaskAsync:
    """Тесты async обёртки task_bridge."""

    @pytest.mark.asyncio
    async def test_calls_send_task(self):
        """Отправляет task через app.send_task."""
        with patch("memory_server.tools.task_bridge.celery_app") as mock_app:
            mock_result = MagicMock()
            mock_result.get.return_value = {"id": "1"}
            mock_app.send_task.return_value = mock_result

            from memory_server.tools.task_bridge import run_task_async
            result = await run_task_async("memory.store", content="test")

            mock_app.send_task.assert_called_once_with(
                "memory.store",
                kwargs={"content": "test"},
            )
            assert result == {"id": "1"}

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        """Таймаут → TimeoutError."""
        with patch("memory_server.tools.task_bridge.celery_app") as mock_app:
            mock_result = MagicMock()
            mock_result.get.side_effect = asyncio.TimeoutError()
            mock_app.send_task.return_value = mock_result

            from memory_server.tools.task_bridge import run_task_async
            with pytest.raises(TimeoutError):
                await run_task_async("memory.store", timeout=1, content="test")

    @pytest.mark.asyncio
    async def test_task_error_propagates(self):
        """Ошибка задачи пробрасывается."""
        with patch("memory_server.tools.task_bridge.celery_app") as mock_app:
            mock_result = MagicMock()
            mock_result.get.side_effect = ValueError("Bad data")
            mock_app.send_task.return_value = mock_result

            from memory_server.tools.task_bridge import run_task_async
            with pytest.raises(ValueError, match="Bad data"):
                await run_task_async("memory.store", content="test")


class TestRunTaskSync:
    """Тесты sync обёртки task_bridge."""

    def test_calls_send_task(self):
        """Отправляет task и ждёт результат."""
        with patch("memory_server.tools.task_bridge.celery_app") as mock_app:
            mock_result = MagicMock()
            mock_result.get.return_value = {"id": "1"}
            mock_app.send_task.return_value = mock_result

            from memory_server.tools.task_bridge import run_task_sync
            result = run_task_sync("memory.store", content="test")

            assert result == {"id": "1"}

    def test_timeout_raises(self):
        """Таймаут → TimeoutError."""
        with patch("memory_server.tools.task_bridge.celery_app") as mock_app:
            mock_result = MagicMock()
            mock_result.get.side_effect = TimeoutError()
            mock_app.send_task.return_value = mock_result

            from memory_server.tools.task_bridge import run_task_sync
            with pytest.raises(TimeoutError):
                run_task_sync("memory.store", timeout=1, content="test")
```

### 3.5 Тесты ошибок

```python
# tests/test_task_errors.py

import pytest
from memory_server.tasks.errors import (
    TaskValidationError,
    TaskTimeoutError,
    TaskDependencyError,
)


class TestTaskErrors:
    """Тесты кастомных исключений tasks."""

    def test_validation_error(self):
        with pytest.raises(TaskValidationError):
            raise TaskValidationError("Invalid input")

    def test_timeout_error(self):
        with pytest.raises(TaskTimeoutError):
            raise TaskTimeoutError("Task exceeded time limit")

    def test_dependency_error(self):
        with pytest.raises(TaskDependencyError):
            raise TaskDependencyError("Redis unavailable")

    def test_errors_inherit_base(self):
        """Все ошибки наследуются от Exception."""
        assert issubclass(TaskValidationError, Exception)
        assert issubclass(TaskTimeoutError, Exception)
        assert issubclass(TaskDependencyError, Exception)
```

---

## 4. Стратегия запуска тестов

### 4.1 Команды

```bash
# Unit-тесты (все, включая Celery eager mode)
pytest tests/ -v --tb=short

# Только Celery tasks
pytest tests/test_celery_tasks.py tests/test_task_bridge.py tests/test_task_errors.py -v

# Integration тесты (требуют Redis)
pytest tests/integration/ -v -m integration

# Load тесты
pytest tests/load/ -v -m load

# Regression — все тесты + coverage
pytest tests/ -v --cov=memory_server --cov-report=term-missing --cov-fail-under=80
```

### 4.2 CI/CD Integration

```yaml
# .github/workflows/test.yml (добавить)
- name: Run Celery unit tests
  run: pytest tests/test_celery_tasks.py tests/test_task_bridge.py -v

- name: Run integration tests
  services:
    redis:
      image: redis:7
      ports: [6379:6379]
  run: pytest tests/integration/ -v -m integration
```

---

## 5. Чек-лист перед запуском

- [ ] `requirements.txt` обновлён: `celery[redis]>=5.6.0`, `pytest-celery>=1.0.0`
- [ ] `conftest.py` содержит Celery fixtures
- [ ] `tests/test_celery_tasks.py` создан
- [ ] `tests/test_task_bridge.py` создан
- [ ] `tests/test_task_errors.py` создан
- [ ] `tests/test_tools.py` обновлён (mock task_bridge)
- [ ] `tests/test_config.py` обновлён (Celery settings)
- [ ] Все существующие тесты проходят (`pytest tests/ -v`)
- [ ] Покрытие tasks ≥ 80%
- [ ] Integration тесты проходят с реальным Redis
- [ ] Load тест: 100 parallel ops < 30s, error rate < 1%

---

*Составлено 31.07.2026 Катериной (Tester)*
*Фыркаю на плохой код, мурчу на зелёные тесты* 🐱
