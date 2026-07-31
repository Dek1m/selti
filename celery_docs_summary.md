# Celery — Систематизированная документация для проекта selti

**Версия Celery:** 5.6.3 (актуальная на July 2026)  
**Цель:** Полный переход selti на Celery для фоновых задач

---

## 1. Getting Started — Быстрый старт

### Установка
```bash
pip install celery
pip install celery[redis]  # если нужен Redis как broker/backend
```

### Базовый пример
```python
from celery import Celery

app = Celery('tasks', broker='redis://localhost:6379/0')

@app.task
def add(x, y):
    return x + y
```

### Запуск worker
```bash
celery -A tasks worker --loglevel=INFO
```

### Вызов задачи
```python
result = add.delay(4, 4)
result.get(timeout=10)  # синхронное ожидание
```

---

## 2. Конфигурация — настройка Celery с Redis broker

### Минимальная конфигурация для selti
```python
# celery_app.py
from celery import Celery

app = Celery('selti')

# Конфигурация через словарь
app.conf.update(
    # Broker
    broker_url='redis://localhost:6379/0',
    
    # Result Backend (опционально)
    result_backend='redis://localhost:6379/1',
    
    # Сериализация
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    
    # Часовой пояс
    timezone='Europe/Moscow',
    enable_utc=True,
    
    # Безопасность
    task_track_started=True,
    task_time_limit=300,  # 5 минут hard limit
    task_soft_time_limit=240,  # 4 минуты soft limit
    
    # Acknowledgment
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    
    # Retry
    task_publish_retry=True,
    task_publish_retry_policy={
        'max_retries': 3,
        'interval_start': 0,
        'interval_step': 0.2,
        'interval_max': 0.5,
    },
)
```

### Конфигурация через файл (рекомендуется для prod)
```python
# celery_app.py
from celery import Celery

app = Celery('selti')
app.config_from_object('celeryconfig')
```

```python
# celeryconfig.py
broker_url = 'redis://redis:6379/0'
result_backend = 'redis://redis:6379/1'

task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']

timezone = 'Europe/Moscow'
enable_utc = True

task_track_started = True
task_time_limit = 300
task_soft_time_limit = 240

task_acks_late = True
worker_prefetch_multiplier = 1
```

### Ключевые настройки Redis

| Параметр | Значение по умолчанию | Описание |
|----------|----------------------|----------|
| `broker_url` | — | URL Redis для broker |
| `result_backend` | — | URL Redis для результатов |
| `redis_max_connections` | Без лимита | Макс. соединений в пуле |
| `redis_socket_timeout` | 120.0 | Timeout для операций (сек) |
| `redis_retry_on_timeout` | False | Retry при timeout |
| `redis_socket_keepalive` | False | TCP keepalive |
| `redis_backend_health_check_interval` | — | Интервал health check (сек) |

---

## 3. Структура Tasks — как правильно организовать

### Рекомендуемая структура для selti
```
selti/
├── celery_app.py          # Celery instance + config
├── tasks/
│   ├── __init__.py
│   ├── memory_tasks.py    # Задачи для memory операций
│   ├── embedding_tasks.py # Задачи для embedding
│   └── maintenance_tasks.py # Фоновые задачи
├── src/
│   └── ... (основной код)
└── worker.py              # Точка входа для worker
```

### Пример структуры задач
```python
# celery_app.py
from celery import Celery

app = Celery('selti')
app.config_from_object('celeryconfig')

# Автообнаружение задач
app.autodiscover_tasks(['tasks'])
```

```python
# tasks/__init__.py
from .celery_app import app

__all__ = ('app',)
```

```python
# tasks/memory_tasks.py
from celery import shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

@shared_task(bind=True, name='tasks.store_memory')
def store_memory(self, content: str, metadata: dict):
    """Асинхронное сохранение памяти"""
    logger.info(f'Storing memory: {content[:50]}...')
    try:
        # Логика сохранения
        return {'status': 'success', 'id': '...'}
    except Exception as exc:
        logger.error(f'Failed to store memory: {exc}')
        raise self.retry(exc=exc, countdown=60)

@shared_task(bind=True, name='tasks.batch_ingest')
def batch_ingest(self, items: list):
    """Пакетная вставка записей"""
    logger.info(f'Batch ingest: {len(items)} items')
    # Обработка пачками по 100
    chunk_size = 100
    for i in range(0, len(items), chunk_size):
        chunk = items[i:i + chunk_size]
        self.update_state(state='PROGRESS', 
                         meta={'current': i, 'total': len(items)})
        # Обработка чанка
    return {'status': 'success', 'processed': len(items)}
```

### Bound Tasks (с доступом к self)
```python
@app.task(bind=True)
def my_task(self, x, y):
    # self.request.id — ID задачи
    # self.request.retries — количество retry
    # self.update_state() — обновление состояния
    pass
```

### Shared Tasks (для переиспользования)
```python
from celery import shared_task

@shared_task
def add(x, y):
    return x + y
```

---

## 4. Retry и Error Handling

### Автоматический retry
```python
@app.task(
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,           # Exponential backoff
    retry_backoff_max=600,        # Макс. задержка 10 минут
    retry_jitter=True,            # Случайный jitter
    max_retries=5,
)
def fetch_data(url):
    return requests.get(url)
```

### Ручной retry с настройками
```python
@app.task(bind=True, default_retry_delay=60)
def process_item(self, item_id):
    try:
        result = do_processing(item_id)
    except TransientError as exc:
        raise self.retry(
            exc=exc,
            countdown=30,  # Задержка перед retry (сек)
            max_retries=3,
        )
    except PermanentError as exc:
        # Не retry, просто прокидываем ошибку
        raise
    return result
```

### Error Callbacks
```python
@app.task(bind=True)
def my_task(self):
    try:
        return do_work()
    except Exception as exc:
        # Логируем и пробрасываем
        logger.error(f'Task failed: {exc}')
        raise

# Привязка errback
my_task.apply_async(
    args=[],
    link_error=handle_error.s()
)

@app.task
def handle_error(request, exc, traceback):
    logger.error(f'Task {request.id} failed: {exc}')
    # Уведомление, запись в БД и т.д.
```

### Исключения
```python
from celery.exceptions import (
    SoftTimeLimitExceeded,
    MaxRetriesExceededError,
    Reject,
    Ignore,
)

@app.task(bind=True)
def my_task(self):
    try:
        do_work()
    except SoftTimeLimitExceeded:
        # Чистим ресурсы перед hard timeout
        cleanup()
        raise
```

### State Machine для задач
```
PENDING → STARTED → SUCCESS
                   → FAILURE
                   → RETRY
         → REVOKED
```

### Пример с прогрессом
```python
@app.task(bind=True)
def long_task(self, items):
    total = len(items)
    for i, item in enumerate(items):
        # Обновляем прогресс
        self.update_state(
            state='PROGRESS',
            meta={'current': i, 'total': total, 'percent': int(i/total*100)}
        )
        process(item)
    return {'status': 'complete', 'total': total}
```

---

## 5. Worker Pools — prefork, eventlet, solo

### Типы пулов

| Пул | Использование | Когда использовать |
|-----|---------------|-------------------|
| **prefork** (по умолчанию) | multiprocessing | CPU-bound задачи |
| **eventlet/gevent** | async I/O | I/O-bound задачи (HTTP, БД) |
| **solo** | однопоточный | Отладка, простые задачи |
| **thread** | threading | Lightweight I/O |

### Запуск с разными пулами
```bash
# Prefork (по умолчанию)
celery -A celery_app worker -l INFO -c 4

# Eventlet (для I/O-bound)
celery -A celery_app worker -l INFO -P eventlet -c 100

# Solo (отладка)
celery -A celery_app worker -l INFO -P solo

# Thread pool
celery -A celery_app worker -l INFO -P threads -c 10
```

### Настройка concurrency
```bash
# Количество worker процессов
celery -A celery_app worker -l INFO -c 8

# Или через конфигурацию
app.conf.worker_concurrency = 8
```

### Автомасштабирование
```bash
# Автомасштабирование от 3 до 10 процессов
celery -A celery_app worker -l INFO --autoscale=10,3
```

```python
# Конфигурация
app.conf.worker_autoscaler = 'celery.worker.autoscale:Autoscaler'
```

### Prefetch Limit
```python
# По умолчанию: worker_prefetch_multiplier * concurrency
# Для long-running задач: поставить 1
app.conf.worker_prefetch_multiplier = 1

# Или отключить prefetch entirely (только Redis)
app.conf.worker_disable_prefetch = True
```

---

## 6. Мониторинг через Flower

### Установка и запуск
```bash
pip install flower
celery -A celery_app flower --port=5555
```

### Docker Compose (для selti)
```yaml
services:
  flower:
    image: mher/flower:2.0
    command: celery -A celery_app flower --port=5555
    ports:
      - "5555:5555"
    environment:
      - CELERY_BROKER_URL=redis://redis:6379/0
      - CELERY_RESULT_BACKEND=redis://redis:6379/1
    depends_on:
      - redis
```

### Ключевые возможности Flower
- **Dashboard:** Мониторинг в реальном времени
- **Workers:** Список worker'ов, их статус, статистика
- **Tasks:** История задач, результаты, traceback
- **Queues:** Длина очередей
- **Rate Limits:** Управление rate limits
- **HTTP API:** 
  - `GET /api/workers` — список workers
  - `GET /api/tasks` — список задач
  - `POST /api/task/async-apply` — запуск задачи
  - `POST /api/worker/shutdown` — остановка worker

### Prometheus + Grafana
```python
# В конфигурации Celery
app.conf.worker_send_task_events = True
app.conf.event_queue_expires = 60

# Flower с Prometheus
# flower --enable_prometheus=True --prometheus_port=8080
```

### CLI мониторинг
```bash
# Список активных задач
celery -A celery_app inspect active

# Список зарегистрированных задач
celery -A celery_app inspect registered

# Статистика worker
celery -A celery_app inspect stats

# Статус кластера
celery -A celery_app status

# Очистка очереди
celery -A celery_app purge
```

---

## 7. Масштабирование Workers

### Множественные workers
```bash
# Один worker с именем
celery -A celery_app worker -l INFO -n worker1@%h

# Несколько workers на одной машине
celery -A celery_app worker -l INFO -c 4 -n w1@%h
celery -A celery_app worker -l INFO -c 4 -n w2@%h
```

### Docker Compose для масштабирования
```yaml
services:
  worker:
    build: .
    command: celery -A celery_app worker -l INFO -c 4
    deploy:
      replicas: 3  # 3 копии worker
    environment:
      - CELERY_BROKER_URL=redis://redis:6379/0
    depends_on:
      - redis
```

### Разделение по очередям
```python
# Конфигурация очередей
from kombu import Exchange, Queue

app.conf.task_queues = (
    Queue('default', Exchange('default'), routing_key='default'),
    Queue('high_priority', Exchange('high_priority'), routing_key='high_priority'),
    Queue('low_priority', Exchange('low_priority'), routing_key='low_priority'),
)

app.conf.task_routes = {
    'tasks.store_memory': {'queue': 'high_priority'},
    'tasks.batch_ingest': {'queue': 'low_priority'},
    'tasks.maintenance': {'queue': 'default'},
}
```

```bash
# Worker для конкретных очередей
celery -A celery_app worker -l INFO -Q high_priority
celery -A celery_app worker -l INFO -Q low_priority,default
```

### Remote Control
```python
# Остановка всех workers
app.control.broadcast('shutdown')

# Остановка конкретного worker
app.control.broadcast('shutdown', destination=['worker1@%h'])

# Ping workers
app.control.ping()

# Rate limit на лету
app.control.rate_limit('tasks.store_memory', '100/m')
```

---

## 8. Canvas — Продвинутые Workflow

### Chain (цепочка)
```python
from celery import chain

# Результат каждой задачи передаётся следующей
workflow = chain(
    process_data.s(data),
    validate_result.s(),
    save_to_db.s()
)
result = workflow.apply_async()
```

### Group (параллельное выполнение)
```python
from celery import group

# Все задачи выполняются параллельно
g = group(
    process_item.s(item) for item in items
)
result = g.apply_async()
results = result.get()  # Список результатов
```

### Chord (группа + callback)
```python
from celery import chord

# Выполнить группу, затем callback с результатами
header = [process_item.s(item) for item in items]
callback = aggregate_results.s()

chord(header)(callback).get()
```

### Пример для selti
```python
# Пакетная обработка с прогрессом
@app.task(bind=True)
def batch_process(self, items):
    chunk_size = 100
    chunks = [
        process_chunk.s(items[i:i+chunk_size])
        for i in range(0, len(items), chunk_size)
    ]
    
    # Выполнить все чанки параллельно
    job = group(chunks)
    result = job.apply_async()
    
    # Ждём завершения
    return result.get()
```

---

## 9. Best Practices для selti

### 1. Идиоматичность задач
```python
@app.task(bind=True, max_retries=3)
def fetch_data(self, url):
    try:
        response = requests.get(url, timeout=10)
        return response.json()
    except requests.RequestException as exc:
        raise self.retry(exc=exc, countdown=30)
```

### 2. Всегда ставь timeout
```python
@app.task(bind=True)
def long_running_task(self):
    try:
        # I/O операция
        result = requests.get(url, timeout=(5, 30))
    except requests.Timeout:
        raise self.retry(countdown=60)
```

### 3. Логирование
```python
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

@app.task
def my_task():
    logger.info('Task started')
    try:
        result = do_work()
        logger.info(f'Task completed: {result}')
        return result
    except Exception as exc:
        logger.error(f'Task failed: {exc}', exc_info=True)
        raise
```

### 4. Сериализация — только JSON
```python
app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
)
```

### 5. Игнорирование результатов (для fire-and-forget)
```python
@app.task(ignore_result=True)
def fire_and_forget(data):
    do_something(data)
```

### 6. Rate Limiting
```python
@app.task(rate_limit='100/m')  # 100 задач в минуту
def api_call():
    pass
```

### 7. Привязка к очередям
```python
@app.task(queue='high_priority')
def critical_task():
    pass
```

### 8. Использование signature для передачи
```python
from celery import signature

# Передача задачи как аргумент
@app.task
def process(data):
    return transform(data)

@app.task
def orchestrator():
    # Передаём signature, а не результат
    s = process.s(data)
    return s.apply_async()
```

### 9. Callbacks и Errbacks
```python
result = my_task.apply_async(
    args=[data],
    link=on_success.s(),
    link_error=on_failure.s()
)
```

### 10. Мониторинг и метрики
```python
@app.task(bind=True)
def monitored_task(self):
    start_time = time.time()
    try:
        result = do_work()
        metrics.task_success.inc()
        return result
    except Exception as exc:
        metrics.task_failure.inc()
        raise
    finally:
        duration = time.time() - start_time
        metrics.task_duration.observe(duration)
```

---

## 10. Интеграция с FastAPI (для selti)

### Пример интеграции
```python
# api/tasks.py
from fastapi import APIRouter, BackgroundTasks
from celery.result import AsyncResult
from celery_app import celery_app
from tasks import store_memory

router = APIRouter()

@router.post("/memory/async")
async def create_memory_async(content: str, metadata: dict):
    # Запускаем Celery задачу
    task = store_memory.apply_async(args=[content, metadata])
    return {"task_id": task.id, "status": "queued"}

@router.get("/memory/task/{task_id}")
async def get_task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": result.state,
        "result": result.result if result.ready() else None,
    }
```

### Lifespan для Celery
```python
# main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from celery_app import celery_app

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    celery_app.start()
    yield
    # Shutdown
    celery_app.control.shutdown()

app = FastAPI(lifespan=lifespan)
```

---

## 11. Docker Compose — готовая конфигурация для selti

```yaml
version: '3.8'

services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redisdata:/data
    command: redis-server --appendonly yes
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_DB: athena_memory
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  worker:
    build: .
    command: celery -A celery_app worker -l INFO -c 4 -Q default,high_priority
    environment:
      - CELERY_BROKER_URL=redis://redis:6379/0
      - CELERY_RESULT_BACKEND=redis://redis:6379/1
      - DATABASE_URL=postgresql://postgres:postgres@postgres:5432/athena_memory
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_started
    restart: unless-stopped

  flower:
    image: mher/flower:2.0
    command: celery -A celery_app flower --port=5555
    ports:
      - "5555:5555"
    environment:
      - CELERY_BROKER_URL=redis://redis:6379/0
      - CELERY_RESULT_BACKEND=redis://redis:6379/1
    depends_on:
      - redis
      - worker
    restart: unless-stopped

  selti:
    build: .
    command: uvicorn main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    environment:
      - CELERY_BROKER_URL=redis://redis:6379/0
      - CELERY_RESULT_BACKEND=redis://redis:6379/1
      - DATABASE_URL=postgresql://postgres:postgres@postgres:5432/athena_memory
    depends_on:
      - redis
      - postgres
      - worker

volumes:
  redisdata:
  pgdata:
```

---

## 12. Безопасность

### Не передавай секреты в задачах
```python
# ❌ ПЛОХО
@app.task
def bad_task(api_key):
    call_api(api_key)

# ✅ ХОРОШО
@app.task
def good_task(resource_id):
    api_key = get_secret_from_vault()
    call_api(resource_id, api_key)
```

### Сериализация — только JSON (не pickle!)
```python
app.conf.update(
    task_serializer='json',
    accept_content=['json'],  # Отклоняем pickle
)
```

### Ограничение доступа к result backend
```python
# Используй Redis с паролем
app.conf.result_backend = 'redis://:password@redis:6379/1'
```

---

## 13. Troubleshooting

### Задача в PENDING бесконечно
- Проверь, что result backend настроен правильно
- Убедись, что задача не имеет `ignore_result=True`
- Проверь, нет ли старых workers без result backend

### Worker не стартует
- Проверь права доступа к.pid файлам
- Убедись, что broker доступен
- Проверь логи: `celery -A celery_app worker -l DEBUG`

### Memory leak в worker
```python
# Ограничивай количество задач на процесс
app.conf.worker_max_tasks_per_child = 1000

# Или по памяти
app.conf.worker_max_memory_per_child = 200000  # 200MB в KB
```

### Задачи выполняются повторно
- Включи `task_acks_late=True` только для идиоматичных задач
- Настрой `worker_prefetch_multiplier=1`

---

## 14. Чек-лист для перехода на Celery в selti

- [ ] Установить `celery[redis]`
- [ ] Создать `celery_app.py` с конфигурацией
- [ ] Создать модуль `tasks/` с задачами
- [ ] Настроить Docker Compose с worker + flower
- [ ] Интегрировать с FastAPI (endpoint для запуска/проверки задач)
- [ ] Настроить мониторинг через Flower
- [ ] Настроить логирование
- [ ] Протестировать retry и error handling
- [ ] Настроить rate limits (если нужно)
- [ ] Добавить health check для worker
- [ ] Обновить документацию

---

## 15. Полезные ссылки

- **Официальная документация:** https://docs.celeryq.dev/
- **Redis broker:** https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html
- **Canvas:** https://docs.celeryq.dev/en/stable/userguide/canvas.html
- **Flower:** https://flower.readthedocs.io/
- **Configuration:** https://docs.celeryq.dev/en/stable/userguide/configuration.html

---

*Документация актуальна для Celery 5.6.3 (July 2026)*
