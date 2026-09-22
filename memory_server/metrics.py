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

# Фаза 3 (волна 3): L1c-гейт не смог оценить пары (Qdrant недоступен /
# нет вектора источника) — fail-closed, рёбер не создано, гранула
# осталась кандидатом на ретрай. Рост = деградация Qdrant на пути линкера.
LINKER_L1C_GATE_FAILURES_TOTAL = Counter(
    f"{PREFIX}_linker_l1c_gate_failures_total",
    "Total L1c co-occurrence cosine-gate failures (fail-closed, no edges created)",
)

# ============================================================
# Полная карта 3D (PLAN_FULL_MAP_3D, M1/M2)
# ============================================================

MAP_SNAPSHOT_BUILD_SECONDS = Histogram(
    f"{PREFIX}_map_snapshot_build_seconds",
    "Full-map snapshot cold build duration (Celery, under lock)",
    buckets=(0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0, 10.0),
)

MAP_LAYOUT_SECONDS = Histogram(
    f"{PREFIX}_map_layout_seconds",
    "3D layout rebuild duration (DrL + relaxation + normalization)",
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

# Сработки сегфолт-щита DrL (фикс F1 приёмки): subprocess умер/завис/ошибся,
# раскладка ушла в сферический fallback. Растёт — igraph на платформе нездоров.
MAP_LAYOUT_FALLBACKS = Counter(
    f"{PREFIX}_map_layout_fallbacks_total",
    "Total DrL subprocess failures answered by spherical fallback",
    ["reason"],  # reason: drl_failed
)

MAP_SNAPSHOT_BYTES = Histogram(
    f"{PREFIX}_map_snapshot_bytes",
    "Snapshot payload size (gz bytes, as stored in Redis)",
    buckets=(100_000, 500_000, 1_000_000, 2_500_000, 5_000_000, 10_000_000),
)

MAP_CACHE_HITS = Counter(
    f"{PREFIX}_map_cache_hits_total",
    "Total map meta/snapshot cache hits (Redis)",
)

# ============================================================
# Galactic Layout v2 (GALACTIC_LAYOUT.md, GL-1/GL-2)
# ============================================================

# Режимы: full — force-пересев по команде Мастера, incremental — beat-прирост
GALACTIC_LAYOUT_SECONDS = Histogram(
    f"{PREFIX}_galactic_layout_seconds",
    "Galactic layout run duration (spectral order + seeding + relaxation)",
    ["mode"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0),
)

# Посажено узлов по регионам галактики (arm/bulge/halo/satellite)
GALACTIC_LAYOUT_PLACED = Counter(
    f"{PREFIX}_galactic_layout_placed_total",
    "Nodes placed into map_layout by galactic layout, by region",
    ["mode", "region"],
)

# Медиана длины резолвленного ребра после прогона (§8.3: ≤150 при R_disk 900)
GALACTIC_EDGE_MEDIAN_LEN = Gauge(
    f"{PREFIX}_galactic_edge_median_len",
    "Median resolved-edge length after galactic layout (structure quality)",
)

# ============================================================
# Edge lifecycle + PPR traverse (V3.5, Ф1/Ф2)
# ============================================================

# Кампания edge_prune (beat 03:30 UTC): кандидаты по режиму прогона.
# mode: dry (только отчёт, edge_prune_dry_run=True) / live (бой)
EDGE_PRUNE_CANDIDATES_TOTAL = Counter(
    f"{PREFIX}_edge_prune_candidates_total",
    "Edges matching prune criteria per campaign run",
    ["mode"],  # mode: dry / live
)

# Фактически отсечённые (pruned_at=now), только live-прогоны
EDGE_PRUNED_TOTAL = Counter(
    f"{PREFIX}_edge_pruned_total",
    "Edges soft-pruned (pruned_at set) by live campaign runs",
    ["mode"],  # mode: live
)

# Касания рёбер reinforce'ом (usage → w к 1.0), считаем touched-строки
EDGE_REINFORCED_TOTAL = Counter(
    f"{PREFIX}_edge_reinforced_total",
    "Edges touched by reinforce batches (rows updated)",
)

# Ручное воскрешение pruned-ребра; noop = ребро не pruned / не найдено
EDGE_RESTORE_TOTAL = Counter(
    f"{PREFIX}_edge_restore_total",
    "Manual restore attempts of pruned edges",
    ["result"],  # result: ok / noop
)

# PPR-traverse (strategy="activation"): запросы и латентность полного пути
# (выборка графа → spread → карточки). status: ok / empty (старт вне графа)
TRAVERSE_ACTIVATION_REQUESTS_TOTAL = Counter(
    f"{PREFIX}_traverse_activation_requests_total",
    "PPR activation traverse requests by outcome",
    ["status"],  # status: ok / empty
)

TRAVERSE_ACTIVATION_LATENCY_SECONDS = Histogram(
    f"{PREFIX}_traverse_activation_latency_seconds",
    "PPR activation traverse duration (graph fetch + spread + cards)",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Длительность кампании edge_prune (SQL кандидаты + применение)
EDGE_PRUNE_DURATION_SECONDS = Histogram(
    f"{PREFIX}_edge_prune_duration_seconds",
    "Edge prune campaign duration (candidates scan + apply)",
    ["mode"],  # mode: dry / live
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 240.0),
)
