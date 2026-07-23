from prometheus_client import Counter, Gauge, Histogram

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

DB_POOL_SIZE = Gauge("athena_db_pool_size", "Current DB pool size")
DB_POOL_AVAILABLE = Gauge("athena_db_pool_available", "Available connections in pool")

EMBEDDING_DURATION = Histogram(
    "athena_embedding_duration_seconds",
    "Embedding API call duration",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

SEARCH_RESULTS = Histogram(
    "athena_search_results_count",
    "Number of results returned by search",
    buckets=(1, 5, 10, 20, 50, 100),
)

MEMORY_COUNT = Gauge("athena_memory_count", "Total memories in DB", ["namespace"])

# --- Метрики MCP tools ---
MCP_TOOL_CALLS_TOTAL = Counter(
    "athena_mcp_tool_calls_total",
    "Total MCP tool calls",
    ["tool", "status"],  # status: ok / error
)

MCP_TOOL_DURATION_SECONDS = Histogram(
    "athena_mcp_tool_duration_seconds",
    "MCP tool call duration in seconds",
    ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# --- Кеш эмбеддингов ---
EMBEDDING_CACHE_HITS = Counter(
    "athena_embedding_cache_hits_total",
    "Total embedding cache hits",
)

EMBEDDING_CACHE_MISSES = Counter(
    "athena_embedding_cache_misses_total",
    "Total embedding cache misses",
)

# --- Дедупликация ---
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
