"""Prometheus метрики для athena-memory (selti).

Префикс: athena_
Стиль: ёмко, по-русски комментарии, по-английски имя/описание.

История:
- v1: HTTP, DB pool, embedding, search, memory_count
- v2: MCP tools, embedding cache, dedup
- v3: Celery tasks, Redis cache, Qdrant operations (по плану CELERY_MIGRATION_PLAN_v3)
"""

from prometheus_client import Counter, Gauge, Histogram

# ============================================================
# HTTP
# ============================================================

HTTP_REQUESTS_TOTAL = Counter(
    "athena_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "athena_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ============================================================
# Database (asyncpg pool)
# ============================================================

DB_POOL_SIZE = Gauge("athena_db_pool_size", "Current DB pool size")
DB_POOL_AVAILABLE = Gauge("athena_db_pool_available", "Available connections in pool")

# ============================================================
# Embedding API
# ============================================================

EMBEDDING_DURATION = Histogram(
    "athena_embedding_duration_seconds",
    "Embedding API call duration",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

# ============================================================
# Search
# ============================================================

SEARCH_RESULTS = Histogram(
    "athena_search_results_count",
    "Number of results returned by search",
    buckets=(1, 5, 10, 20, 50, 100),
)

# ============================================================
# Memory count (per namespace)
# ============================================================

MEMORY_COUNT = Gauge("athena_memory_count", "Total memories in DB", ["namespace"])

# ============================================================
# MCP tools (calls + duration)
# ============================================================

MCP_TOOL_CALLS_TOTAL = Counter(
    "athena_mcp_tool_calls_total",
    "Total MCP tool calls",
    ["tool", "status"],  # status: ok / error / timeout
)

MCP_TOOL_DURATION_SECONDS = Histogram(
    "athena_mcp_tool_duration_seconds",
    "MCP tool call duration in seconds",
    ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ============================================================
# Embedding cache (Redis)
# ============================================================

EMBEDDING_CACHE_HITS = Counter(
    "athena_embedding_cache_hits_total",
    "Total embedding cache hits",
)

EMBEDDING_CACHE_MISSES = Counter(
    "athena_embedding_cache_misses_total",
    "Total embedding cache misses",
)

# ============================================================
# Deduplication
# ============================================================

DEDUP_SKIPPED_TOTAL = Counter(
    "athena_dedup_skipped_total",
    "Total dedup skips by namespace and reason",
    ["namespace", "reason"],  # reason: exact / semantic
)

DEDUP_INSERTED_TOTAL = Counter(
    "athena_dedup_inserted_total",
    "Total new memories inserted after dedup check",
    ["namespace"],
)

# ============================================================
# Celery tasks (по плану v3, Фаза 4)
# ============================================================

# Общее количество выполненных задач по имени и статусу
CELERY_TASKS_TOTAL = Counter(
    "athena_celery_tasks_total",
    "Total Celery tasks completed",
    ["task", "status"],  # status: success / failure / retry
)

# Длительность выполнения задачи (от начала до конца)
CELERY_TASK_DURATION_SECONDS = Histogram(
    "athena_celery_task_duration_seconds",
    "Celery task execution duration in seconds",
    ["task"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

# Задержка в очереди (от send_task до начала выполнения)
CELERY_TASK_LATENCY_SECONDS = Histogram(
    "athena_celery_task_latency_seconds",
    "Celery task queue latency (send to start)",
    ["task"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Количество повторных попыток
CELERY_TASK_RETRIES_TOTAL = Counter(
    "athena_celery_task_retries_total",
    "Total Celery task retries",
    ["task"],
)

# Таймауты
CELERY_TASK_TIMEOUTS_TOTAL = Counter(
    "athena_celery_task_timeouts_total",
    "Total Celery task timeouts",
    ["task"],
)

# Ошибки (.failure)
CELERY_TASK_ERRORS_TOTAL = Counter(
    "athena_celery_task_errors_total",
    "Total Celery task errors",
    ["task", "exception_type"],
)

# Активные воркеры
CELERY_WORKERS_ACTIVE = Gauge(
    "athena_celery_workers_active",
    "Number of active Celery workers",
)

# Длина очереди (оценочная, по task_ready)
CELERY_QUEUE_LENGTH = Gauge(
    "athena_celery_queue_length",
    "Estimated queue length (pending tasks)",
    ["queue"],
)

# ============================================================
# Redis cache operations (НОВАЯ)
# ============================================================

REDIS_OPS_TOTAL = Counter(
    "athena_redis_ops_total",
    "Total Redis operations",
    ["operation"],  # operation: get / set / mget / mset / delete
)

REDIS_OPS_DURATION_SECONDS = Histogram(
    "athena_redis_ops_duration_seconds",
    "Redis operation duration in seconds",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# ============================================================
# Qdrant vector operations (НОВАЯ)
# ============================================================

QDRANT_OPS_TOTAL = Counter(
    "athena_qdrant_ops_total",
    "Total Qdrant operations",
    ["operation"],  # operation: search / upsert / batch_upsert / delete
)

QDRANT_OPS_DURATION_SECONDS = Histogram(
    "athena_qdrant_ops_duration_seconds",
    "Qdrant operation duration in seconds",
    ["operation"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

QDRANT_SEARCH_RESULTS = Histogram(
    "athena_qdrant_search_results_count",
    "Number of results returned by Qdrant search",
    buckets=(1, 5, 10, 20, 50, 100),
)
