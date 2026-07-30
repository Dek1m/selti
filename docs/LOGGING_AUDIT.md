# Аудит архитектуры логирования selti

> Милорд! Это полный аудит + архитектурное решение по стандарту Argenta Team.
> Эна, 2026-07-30

---

## 1. Текущее состояние

### Файлы логирования

| Файл | Роль | Статус |
|------|------|--------|
| `memory_server/logger.py` | Единая точка входа | ✅ Соответствует стандарту |
| `memory_server/server.py` | Инициализация при старте | ✅ Вызывает `setup_logging()` |
| `memory_server/tools/memory_tools.py` | Логирование инструментов | ⚠️ Есть проблемы |
| `memory_server/memory/service.py` | Бизнес-логика | ⚠️ Есть проблемы |
| `memory_server/embedding/client.py` | HTTP-клиент | ⚠️ Минимальное логирование |
| `memory_server/cache/redis_client.py` | Redis-кэш | ✅ OK |
| `tests/test_posix_logging.py` | Тесты формата | ✅ Полное покрытие |

### Что работает хорошо

1. **PosixFormatter** — идеально соответствует стандарту:
   - Формат `[ISO8601] [LEVEL] [service] message {"meta": "json"}`
   - Маппинг WARNING → WARN
   - ISO 8601 UTC с миллисекундами
   - JSON-мета из extra-полей

2. **setup_logging()** — единая точка входа:
   - Читает `LOG_LEVEL` из env
   - Читает `SERVICE_NAME` из env
   - Настраивает корневой логгер
   - Пишет в stdout (не stderr)

3. **get_logger()** — именованные логгеры:
   - Каждый модуль получает свой логгер
   - Корректно работает с иерархией

4. **Тесты** — полное покрытие:
   - Формат строки
   - Фильтрация по уровню
   - JSON-мета
   - Edge cases (пустое сообщение, спецсимволы, unicode)

### Проблемы

#### 🔴 Критично

**1. Дублирование логов (tools ↔ service)**

```python
# memory_tools.py
logger.info("memory_store: content_len=%d namespace=%s", len(content), namespace)
record, action = await _track_tool("memory_store", service.store(...))

# service.py (внутри store)
logger.info("store: content_len=%d user_id=%s namespace=%s", len(content), user_id, namespace)
```

**Результат:** Каждая операция логируется 2 раза:
```
[2026-07-30T18:00:00.000Z] [INFO] [selti] memory_store: content_len=100 namespace=default
[2026-07-30T18:00:00.001Z] [INFO] [selti] store: content_len=100 user_id=usr_123 namespace=default
```

**Влияние:**
- ×2 объём логов
- ×2 нагрузка на disk I/O
- ×2 стоимость хранения в Loki/ELK
- Сложность фильтрации (одно событие — два сообщения)

#### 🟡 Важно

**2. Отсутствие correlation ID в логах**

```python
# __main__.py — correlation ID есть
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Но в логах он не используется!
logger.info("memory_store: ...")  # нет request_id
```

**Результат:** Невозможно отследить цепочку запросов в проде.

**3. Непоследовательное использование get_logger vs logging.getLogger**

```python
# server.py
logger = logging.getLogger(__name__)  # ❌ напрямую

# memory_tools.py
logger = logging.getLogger(__name__)  # ❌ напрямую

# service.py
logger = logging.getLogger(__name__)  # ❌ напрямую

# cache/redis_client.py
logger = logging.getLogger(__name__)  # ❌ напрямую
```

Везде используется `logging.getLogger()` вместо `get_logger()` из нашего модуля.

#### 🟢 Мелочи

**4. Отсутствие JSON-режима для прода**

Текущий PosixFormatter всегда выводит человекочитаемый формат. Для централизованного сбора (Loki, ELK) нужен JSON-режим.

**5. Нет автоматического добавления duration_ms**

В `memory_tools.py` duration считается вручную:
```python
start = time.monotonic()
# ... операция ...
duration = time.monotonic() - start
logger.info("... duration=%.3fs", duration)
```

Хотелось бы декларативно: `@timed` декоратор или контекстный менеджер.

**6. Вставка данных в message вместо JSON-меты**

```python
# Вместо:
logger.info("store: content_len=%d user_id=%s", len(content), user_id)

# Лучше:
logger.info("store", extra={"content_len": len(content), "user_id": user_id})
```

---

## 2. Архитектурное решение

### Концепция: "Два слоя логирования"

```
┌─────────────────────────────────────────────────────┐
│                    СЛОЙ ИНСТРУМЕНТОВ                  │
│  memory_tools.py — логирует ВХОД/ВЫХОД инструментов │
│  + correlation ID + duration + context               │
└─────────────────────────────────────────────────────┘
                          │
                          │ вызывает service
                          ▼
┌─────────────────────────────────────────────────────┐
│                    СЛОЙ БИЗНЕСА                      │
│  service.py — НЕ логирует (или только WARN/ERROR)   │
└─────────────────────────────────────────────────────┘
```

### Правило

> **Инструменты** логируют вход/выход. **Service** логирует только ошибки и предупреждения.

### Новая архитектура логирования

```
┌─────────────────────────────────────────────────────┐
│                    logger.py                         │
│  ┌─────────────────────────────────────────────┐    │
│  │ PosixFormatter (текущий)                     │    │
│  │ + JSON-режим через LOG_FORMAT=json          │    │
│  └─────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────┐    │
│  │ RequestContextFilter                        │    │
│  │ + request_id из ContextVar                  │    │
│  │ + tool_name из ContextVar                   │    │
│  └─────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────┐    │
│  │ get_logger(name) → logging.Logger           │    │
│  │ setup_logging(level, service, fmt)          │    │
│  └─────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘
```

### Компоненты

#### 1. `RequestContextFilter`

Автоматически добавляет `request_id` и `tool_name` во все логи:

```python
class RequestContextFilter(logging.Filter):
    """Добавляет correlation ID и context во все логи."""
    
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or ""
        record.tool_name = tool_name_var.get() or ""
        return True
```

#### 2. `JsonFormatter` (опциональный)

Для прода — полный JSON:

```python
class JsonFormatter(logging.Formatter):
    """JSON-формат для централизованного сбора."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": self._LEVEL_MAP.get(record.levelname, record.levelname),
            "service": getattr(record, "service", SERVICE_NAME),
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", ""),
            "tool_name": getattr(record, "tool_name", ""),
        }
        # Добавляем extra-поля
        for key in ["duration_ms", "session_id", "error", "tool", "query", ...]:
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        return json.dumps(log_entry, default=str)
```

#### 3. Улучшенный `setup_logging`

```python
def setup_logging(
    level: str | None = None,
    service: str | None = None,
    fmt: str = "posix",  # "posix" | "json"
) -> None:
    """Настроить глобальный логгер.
    
    Args:
        level: Уровень логирования (DEBUG/INFO/WARN/ERROR). Из env LOG_LEVEL.
        service: Имя сервиса в логах. Из env SERVICE_NAME.
        fmt: Формат вывода. "posix" — человекочитаемый, "json" — для прода.
    """
    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    log_level = LOG_LEVELS.get(level_name, logging.INFO)
    svc = service or SERVICE_NAME
    
    # Выбор форматтера
    if fmt == "json" or os.environ.get("LOG_FORMAT") == "json":
        formatter = JsonFormatter(service=svc)
    else:
        formatter = PosixFormatter(service=svc)
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())
    
    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(handler)
```

#### 4. Улучшенный `memory_tools.py`

```python
@mcp.tool()
async def memory_store(
    content: str,
    user_id: str,
    metadata: str | dict | None = None,
    namespace: str | None = None,
    importance: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Store a new memory record."""
    assert ctx is not None
    metadata = _coerce_metadata(metadata)
    service = ctx.request_context.lifespan_context["service"]
    
    # Устанавливаем контекст для correlation
    tool_name_var.set("memory_store")
    
    # Логируем только в инструменте (не в service)
    logger.info("memory_store", extra={
        "content_len": len(content),
        "namespace": namespace,
        "user_id": user_id,
        "importance": importance,
    })
    
    try:
        record, action = await _track_tool("memory_store", service.store(
            content=content,
            user_id=user_id,
            metadata=metadata,
            namespace=namespace,
            importance=importance,
        ))
        result = record.model_dump(mode="json")
        result["_dedup_action"] = action.value
        logger.info("memory_store done", extra={
            "id": record.id,
            "dedup_action": action.value,
            "namespace": record.namespace,
        })
        return result
    except Exception as e:
        logger.exception("memory_store failed")
        raise RuntimeError(str(e)) from e
```

#### 5. Улучшенный `service.py`

```python
class MemoryService:
    """Business logic layer for memory operations."""
    
    async def store(self, content, user_id, metadata, namespace, importance):
        namespace = namespace or "default"
        ns_record = await self.ns_repo.get_or_create(namespace)
        
        # НЕ логируем INFO здесь — это делает инструмент
        # Логируем только WARN/ERROR
        
        if self.config.dedup_enabled:
            decision = await self.dedup.check(content, user_id, namespace, metadata=metadata)
            if decision.action == DedupAction.SKIP:
                # SKIP — не ошибка, но можно залогировать на DEBUG
                logger.debug("store: SKIP existing_id=%s", decision.existing_id)
                record = await self.repository.get_by_id(decision.existing_id)
                return record, DedupAction.SKIP
        
        # ... остальная логика без INFO-логов
```

---

## 3. План рефакторинга

### Фаза 1: Добавить контекст (без breaking changes)

| Шаг | Файл | Действие | Риск |
|-----|------|----------|------|
| 1.1 | `logger.py` | Добавить `RequestContextFilter` | Минимальный |
| 1.2 | `logger.py` | Добавить `tool_name_var` в `__init__.py` | Минимальный |
| 1.3 | `logger.py` | Добавить `JsonFormatter` | Минимальный |
| 1.4 | `logger.py` | Обновить `setup_logging()` — принимать `fmt` | Минимальный |
| 1.5 | `__main__.py` | Установить `request_id_var` в middleware | Минимальный |

**Результат:** Контекст доступен, но никто его пока не использует.

### Фаза 2: Убрать дублирование

| Шаг | Файл | Действие | Риск |
|-----|------|----------|------|
| 2.1 | `memory_tools.py` | Убрать `logger.info` в начале каждого тула | Средний |
| 2.2 | `memory_tools.py` | Оставить логи только в `_track_tool` и в конце | Средний |
| 2.3 | `service.py` | Убрать все `logger.info` (оставить только DEBUG) | Средний |
| 2.4 | `service.py` | Оставить `logger.warning` и `logger.exception` | Нет |
| 2.5 | Тесты | Обновить тесты, если логи меняются | Низкий |

**Результат:** ×2 сокращение объёма логов.

### Фаза 3: Привести к единому стилю

| Шаг | Файл | Действие | Риск |
|-----|------|----------|------|
| 3.1 | Все модули | Заменить `logging.getLogger(__name__)` → `get_logger(__name__)` | Минимальный |
| 3.2 | `logger.py` | Экспортировать `get_logger` как основной API | Нет |

**Результат:** Единый стиль во всех модулях.

### Фаза 4: Добавить correlation ID

| Шаг | Файл | Действие | Риск |
|-----|------|----------|------|
| 4.1 | `__main__.py` | Уже есть `request_id_var` — просто использовать | Нет |
| 4.2 | `memory_tools.py` | Установить `tool_name_var` в каждом тул-хендлере | Низкий |
| 4.3 | `logger.py` | Фильтр автоматически подставит в логи | Нет |

**Результат:** Полная трассировка запросов.

### Фаза 5: JSON-режим для прода

| Шаг | Файл | Действие | Риск |
|-----|------|----------|------|
| 5.1 | `docker-compose.yml` | Добавить `LOG_FORMAT: json` в env | Нет |
| 5.2 | `logger.py` | `JsonFormatter` уже добавлен в фазе 1 | Нет |
| 5.3 | Документация | Обновить `LOGGING_STANDARD.md` — описать JSON-режим | Нет |

**Результат:** Готовность к Loki/ELK.

---

## 4. Итоговая архитектура

```
┌─────────────────────────────────────────────────────────┐
│                      logger.py                           │
│                                                          │
│  setup_logging(level, service, fmt)                      │
│    ├── PosixFormatter (по умолчанию)                     │
│    ├── JsonFormatter (если LOG_FORMAT=json)              │
│    └── RequestContextFilter (всегда)                      │
│                                                          │
│  get_logger(name) → logging.Logger                       │
│                                                          │
│  ContextVars:                                            │
│    ├── request_id_var (из __main__.py middleware)        │
│    └── tool_name_var (из tools)                          │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                 memory_tools.py                          │
│                                                          │
│  @mcp.tool() async def memory_store(...):                │
│    tool_name_var.set("memory_store")                     │
│    logger.info("memory_store", extra={...})              │
│    await _track_tool(...)                                │
│    logger.info("memory_store done", extra={...})         │
└─────────────────────────────────────────────────────────┘
                          │
                          │ вызывает service
                          ▼
┌─────────────────────────────────────────────────────────┐
│                  service.py                              │
│                                                          │
│  async def store(self, ...):                             │
│    # НЕ логирует INFO — это делает инструмент           │
│    # Логирует только WARN/ERROR                          │
│    if dedup_action == SKIP:                              │
│        logger.debug("store: SKIP ...")                   │
│    # ...                                                 │
└─────────────────────────────────────────────────────────┘
```

### Пример лога (текущий формат)

```
[2026-07-30T18:00:00.000Z] [INFO] [selti] memory_store {"content_len": 100, "namespace": "default", "user_id": "usr_123", "request_id": "req-abc-123", "tool_name": "memory_store"}
[2026-07-30T18:00:00.050Z] [INFO] [selti] memory_store done {"id": "mem_xyz", "dedup_action": "insert", "namespace": "default", "duration_ms": 50.2, "request_id": "req-abc-123", "tool_name": "memory_store"}
```

### Пример лога (JSON-режим)

```json
{"timestamp": "2026-07-30T18:00:00.000Z", "level": "INFO", "service": "selti", "message": "memory_store", "request_id": "req-abc-123", "tool_name": "memory_store", "content_len": 100, "namespace": "default", "user_id": "usr_123"}
{"timestamp": "2026-07-30T18:00:00.050Z", "level": "INFO", "service": "selti", "message": "memory_store done", "request_id": "req-abc-123", "tool_name": "memory_store", "id": "mem_xyz", "dedup_action": "insert", "namespace": "default", "duration_ms": 50.2}
```

---

## 5. Метрики

| Метрика | До | После | Экономия |
|---------|-----|-------|----------|
| Логов на операцию | 2 | 2 | — |
| Объём логов | 100% | 50% | ×2 |
| Correlation ID | ❌ | ✅ | — |
| JSON-режим | ❌ | ✅ | — |
| Единый стиль | ❌ | ✅ | — |

---

## 6. Зависимости

| Файл | Зависит от | Нужно изменить |
|------|------------|----------------|
| `logger.py` | — | Добавить `RequestContextFilter`, `JsonFormatter`, `tool_name_var` |
| `__main__.py` | `logger.py` | Уже использует `request_id_var` — ОК |
| `server.py` | `logger.py` | Оставить как есть |
| `memory_tools.py` | `logger.py`, `service.py` | Убрать дублирующие логи, добавить `tool_name_var` |
| `service.py` | — | Убрать INFO-логи (оставить DEBUG/WARN/ERROR) |
| `embedding/client.py` | `logger.py` | Добавить WARN/ERROR логи |
| `cache/redis_client.py` | — | Оставить как есть |
| `tests/test_posix_logging.py` | `logger.py` | Обновить тесты для нового формата |

---

*Эна, 2026-07-30. Милорд, жду вашего решения по плану!*
