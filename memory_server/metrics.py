"""Prometheus метрики для selti.

Префикс: динамический из SERVICE_NAME env var (lowercase).
Стиль: ёмко, по-русски комментарии, по-английски имя/описание.

История:
- v1: HTTP, DB pool, embedding, search, memory_count
- v2: MCP tools, embedding cache, dedup
- v3: Celery tasks, Redis cache, Qdrant operations (по плану CELERY_MIGRATION_PLAN_v3)
"""

import os

from prometheus_client import Counter, Gauge, Histogram

PREFIX = os.getenv("SERVICE_NAME", "selti").lower()

# ============================================================
# Health check
# ============================================================

HEALTH_STATUS = Gauge(
    f"{PREFIX}_health_status",
    "Health check status (1=ok, 0=error)",
    ["check"],
)

HEALTH_CHECKS_TOTAL = Counter(
    f"{PREFIX}_health_checks_total",
    "Total health check attempts",
    ["check"],
)

# ============================================================
# HTTP
# ============================================================

HTTP_REQUESTS_TOTAL = Counter(
    f"{PREFIX}_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    f"{PREFIX}_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ============================================================
# Database (asyncpg pool)
# ============================================================

DB_POOL_SIZE = Gauge(f"{PREFIX}_db_pool_size", "Current DB pool size")
DB_POOL_AVAILABLE = Gauge(f"{PREFIX}_db_pool_available", "Available connections in pool")

# ============================================================
# Embedding API
# ============================================================

EMBEDDING_DURATION = Histogram(
    f"{PREFIX}_embedding_duration_seconds",
    "Embedding API call duration",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

# ============================================================
# Search
# ============================================================

SEARCH_RESULTS = Histogram(
    f"{PREFIX}_search_results_count",
    "Number of results returned by search",
    ["tool"],
    buckets=(1, 5, 10, 20, 50, 100),
)

# ============================================================
# Memory count (per namespace)
# ============================================================

MEMORY_COUNT = Gauge(f"{PREFIX}_memory_count", "Total memories in DB", ["namespace"])

# ============================================================
# MCP tools (calls + duration)
# ============================================================

MCP_TOOL_CALLS_TOTAL = Counter(
    f"{PREFIX}_mcp_tool_calls_total",
    "Total MCP tool calls",
    ["tool", "status"],  # status: ok / error / timeout
)

MCP_TOOL_DURATION_SECONDS = Histogram(
    f"{PREFIX}_mcp_tool_duration_seconds",
    "MCP tool call duration in seconds",
    ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ============================================================
# Embedding cache (Redis)
# ============================================================

EMBEDDING_CACHE_HITS = Counter(
    f"{PREFIX}_embedding_cache_hits_total",
    "Total embedding cache hits",
)

EMBEDDING_CACHE_MISSES = Counter(
    f"{PREFIX}_embedding_cache_misses_total",
    "Total embedding cache misses",
)

# ============================================================
# Deduplication
# ============================================================

DEDUP_SKIPPED_TOTAL = Counter(
    f"{PREFIX}_dedup_skipped_total",
    "Total dedup skips by namespace and reason",
    ["namespace", "reason"],  # reason: exact / semantic
)

DEDUP_INSERTED_TOTAL = Counter(
    f"{PREFIX}_dedup_inserted_total",
    "Total new memories inserted after dedup check",
    ["namespace"],
)

# ============================================================
# Celery tasks (по плану v3, Фаза 4)
# ============================================================

# Общее количество выполненных задач по имени и статусу
CELERY_TASKS_TOTAL = Counter(
    f"{PREFIX}_celery_tasks_total",
    "Total Celery tasks completed",
    ["task", "status"],  # status: success / failure / retry
)

# Длительность выполнения задачи (от начала до конца)
CELERY_TASK_DURATION_SECONDS = Histogram(
    f"{PREFIX}_celery_task_duration_seconds",
    "Celery task execution duration in seconds",
    ["task"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

# Задержка в очереди (от send_task до начала выполнения)
CELERY_TASK_LATENCY_SECONDS = Histogram(
    f"{PREFIX}_celery_task_latency_seconds",
    "Celery task queue latency (send to start)",
    ["task"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Количество повторных попыток
CELERY_TASK_RETRIES_TOTAL = Counter(
    f"{PREFIX}_celery_task_retries_total",
    "Total Celery task retries",
    ["task"],
)

# Таймауты
CELERY_TASK_TIMEOUTS_TOTAL = Counter(
    f"{PREFIX}_celery_task_timeouts_total",
    "Total Celery task timeouts",
    ["task"],
)

# Ошибки (.failure)
CELERY_TASK_ERRORS_TOTAL = Counter(
    f"{PREFIX}_celery_task_errors_total",
    "Total Celery task errors",
    ["task", "exception_type"],
)

# Активные воркеры
CELERY_WORKERS_ACTIVE = Gauge(
    f"{PREFIX}_celery_workers_active",
    "Number of active Celery workers",
    multiprocess_mode="livesum",
)

# Длина очереди (оценочная, по task_ready)
CELERY_QUEUE_LENGTH = Gauge(
    f"{PREFIX}_celery_queue_length",
    "Estimated queue length (pending tasks)",
    ["queue"],
    multiprocess_mode="livesum",
)

# ============================================================
# Redis cache operations (НОВАЯ)
# ============================================================

REDIS_OPS_TOTAL = Counter(
    f"{PREFIX}_redis_ops_total",
    "Total Redis operations",
    ["operation"],  # operation: get / set / mget / mset / delete
)

REDIS_OPS_DURATION_SECONDS = Histogram(
    f"{PREFIX}_redis_ops_duration_seconds",
    "Redis operation duration in seconds",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# ============================================================
# Qdrant vector operations (НОВАЯ)
# ============================================================

QDRANT_OPS_TOTAL = Counter(
    f"{PREFIX}_qdrant_ops_total",
    "Total Qdrant operations",
    ["operation"],  # operation: search / upsert / batch_upsert / delete
)

QDRANT_OPS_DURATION_SECONDS = Histogram(
    f"{PREFIX}_qdrant_ops_duration_seconds",
    "Qdrant operation duration in seconds",
    ["operation"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

QDRANT_SEARCH_RESULTS = Histogram(
    f"{PREFIX}_qdrant_search_results_count",
    "Number of results returned by Qdrant search",
    buckets=(1, 5, 10, 20, 50, 100),
)

# ============================================================
# Qdrant Circuit Breaker
# ============================================================

QDRANT_CB_STATE = Gauge(
    f"{PREFIX}_qdrant_circuit_breaker_state",
    "Qdrant circuit breaker state (1=open, 0=closed/half-open)",
)

# ============================================================
# Business metrics (НОВАЯ) — шаг 3.9
# ============================================================

# Dedup ratio: skipped / (skipped + inserted) per namespace.
# Обновляется инлайн в dedup.py после каждого решения.
DEDUP_RATIO = Gauge(
    f"{PREFIX}_dedup_ratio",
    "Dedup skip ratio per namespace (skipped / total)",
    ["namespace"],
)

# Memory growth rate: новые записи в hour per namespace.
# Обновляется periodic task раз в час.
MEMORY_GROWTH_RATE = Gauge(
    f"{PREFIX}_memory_growth_rate",
    "Memory growth rate per namespace (records/hour)",
    ["namespace"],
)

# Embedding cache hit ratio: hits / (hits + misses).
# Обновляется инлайн в embedding/client.py после каждого cache-операции.
EMBEDDING_CACHE_HIT_RATIO = Gauge(
    f"{PREFIX}_embedding_cache_hit_ratio",
    "Embedding cache hit ratio (0.0 – 1.0)",
)
