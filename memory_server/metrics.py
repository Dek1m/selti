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
    # Малые бакеты (Фаза 3.3): выдача тулов короткая — важна гранулярность
    # 0 (пустая) / 1 / 3 / 5 / 10 / 20+, а не хвост 50-100
    buckets=(0, 1, 3, 5, 10, 20),
)

# Качество поиска (Фаза 3.3): счётчик пустых выдач по namespace.
# Инкремент в MemoryService.search при пустом результате; namespace=None
# (поиск по всему корпусу) → label "all".
ZERO_RESULT_SEARCHES_TOTAL = Counter(
    f"{PREFIX}_zero_result_searches_total",
    "Total searches that returned no results",
    ["namespace"],
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
    # 0.8 — плотность вокруг p95; 1.2/1.6/2.0 — реальные прод-выбросы (Рэй),
    # чтобы p95 не проваливался в широкий бакет (1.0, 2.5]
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 0.8, 1.0, 1.2, 1.6, 2.0, 2.5, 5.0, 10.0),
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

# ============================================================
# Memory V3 (ADR-019): версионирование / confirm / REWIRE / GC
# ============================================================

MEMORIES_VERSIONED_TOTAL = Counter(
    f"{PREFIX}_memories_versioned_total",
    "New granule versions created via supersede (V3.0)",
    ["reason"],  # reason: edit / explicit (supersede tool)
)

DEDUP_CONFIRMED_TOTAL = Counter(
    f"{PREFIX}_dedup_confirmed_total",
    "Duplicate facts confirmed instead of stored (V3.0 confirm semantics)",
    ["action"],  # action: update (exact hash) / skip (semantic score)
)

RELATIONS_REWIRED_TOTAL = Counter(
    f"{PREFIX}_relations_rewired_total",
    "Relations rewired to the new version on supersede (V3.1)",
)

GC_PURGE_BLOCKED_TOTAL = Counter(
    f"{PREFIX}_gc_purge_blocked_total",
    "GC purge runs blocked by the stop-crank (V3.1 F)",
    ["reason"],  # reason: purge_disabled / mode_disabled
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

# ============================================================
# Linker V3 (ADR-019 C, фазы V3.2/V3.3) — резолв имён + автосвязи
# ============================================================

# Созданные автосвязи по слою пирамиды: l1a (synonym ANN), l1c (co-occurrence),
# l2 (LLM-вердикт).
LINKER_LINKS_CREATED_TOTAL = Counter(
    f"{PREFIX}_linker_links_created_total",
    "Total auto-links created by linker layer",
    ["layer"],
)

# Разрешённые имена: lateral-резолв в sync + кампания name_reconciler.
LINKER_NAMES_RESOLVED_TOTAL = Counter(
    f"{PREFIX}_linker_names_resolved_total",
    "Total dangling target_name edges resolved to target_id",
    ["path"],  # path: sync / reconciler
)

# Вердикты L2 по типу: link / duplicate / contradiction / none / error.
LINKER_LLM_VERDICTS_TOTAL = Counter(
    f"{PREFIX}_linker_llm_verdicts_total",
    "Total L2 LLM verdicts by type",
    ["verdict"],
)

# Latency LLM-вызова вердикта (один вызов на гранулу, ≤5 кандидатов).
LINKER_LLM_LATENCY_SECONDS = Histogram(
    f"{PREFIX}_linker_llm_latency_seconds",
    "Linker LLM verdict call latency in seconds",
    buckets=(0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0),
)

# Размер Redis-очереди L2 (кандидаты серой зоны, ждут вердикта).
LINKER_L2_QUEUE_SIZE = Gauge(
    f"{PREFIX}_linker_l2_queue_size",
    "Pending L2 verdict queue size (granules waiting for LLM)",
)
