"""Worker-scoped singletons for Celery workers.

Один процесс = один набор ресурсов. Создаётся при старте worker,
переиспользуется всеми задачами в этом процессе, закрывается при остановке.

Ресурсы:
    - asyncpg pool (min/max из settings.db_min_connections/db_max_connections) — через db.pool.create_pool()
    - QdrantClient (sync, per process)
    - EmbeddingClient (lazy init — объект при старте, httpx при первом embed())

Сигналы:
    worker_process_init  → get_pool() + get_qdrant()
    worker_process_shutdown → close_all()

Pool sizing (plan v3):
    min=2, max=4 per process × 4 concurrency = 8–16 total connections.
    Настройки берутся из settings.db_min_connections / settings.db_max_connections.
    Timeout на acquire: 15s (в db.pool.create_pool). При exhaustion — PoolTimeoutError → retry в task.

Архитектура:
    Worker Process Start
      → create asyncpg pool (lazy, через get_pool())
      → create QdrantClient (lazy, через get_qdrant())
      → EmbeddingClient: объект создан, httpx — lazy

    Worker Process Shutdown
      → close asyncpg pool
      → close QdrantClient
      → close EmbeddingClient
"""

import logging
import threading
from typing import Optional

import asyncpg
from qdrant_client import QdrantClient

from memory_server.config import settings
from memory_server.tasks.async_bridge import run_async

logger = logging.getLogger(__name__)

# ── Singletons (per worker process) ─────────────────────────────

_pool: Optional[asyncpg.Pool] = None
_qdrant: Optional[QdrantClient] = None
_embedding: Optional["EmbeddingClient"] = None
_pool_lock = threading.Lock()  # Guard от гонки при lazy init


# ── asyncpg pool ────────────────────────────────────────────────

async def _create_pool() -> asyncpg.Pool:
    """Создать asyncpg pool через существующий db.pool.create_pool().

    Переиспользует:
    - DSN трансформацию (+asyncpg → plain postgresql)
    - jsonb codec инициализацию
    - statement_timeout = 45s
    - timeout=15s на acquire (защита от pool exhaustion)

    Pool sizing берётся из settings:
    - db_min_connections (default: 2) — минимальное соединений
    - db_max_connections (default: 10, worker: 4) — максимальное соединений
    """
    from memory_server.db.pool import create_pool

    min_size = settings.db_min_connections
    max_size = min(settings.db_max_connections, 4)  # Cap at 4 per worker process

    return await create_pool(
        dsn=settings.database_url,
        min_size=min_size,
        max_size=max_size,
    )


def _update_pool_metrics(pool: Optional[asyncpg.Pool]) -> None:
    """Обновить Prometheus метрики состояния pool.

    Вызывается после создания pool и при каждом get_pool().
    Метрики: athena_db_pool_size, athena_db_pool_available.
    """
    if pool is None:
        return
    try:
        from memory_server.metrics import DB_POOL_SIZE, DB_POOL_AVAILABLE

        DB_POOL_SIZE.set(pool.get_size())
        DB_POOL_AVAILABLE.set(pool.get_idle_size())
    except Exception:
        pass  # Метрики — best effort, не крашим task из-за них


def get_pool() -> asyncpg.Pool:
    """Получить или создать asyncpg pool (lazy init через run_async).

    Pool создаётся ОДИН раз на worker process.
    Все задачи переиспользуют один и тот же pool.

    Thread safety: используем threading.Lock() для guard от гонки
    при lazy init (теоретически possible если два потока одновременно
    вызовут get_pool() до инициализации).

    Pool sizing:
    - min_size: settings.db_min_connections (default 2)
    - max_size: min(settings.db_max_connections, 4) per process
    - total max: 4 workers × 4 = 16 connections (safe for PG default max_connections=100)
    """
    global _pool
    if _pool is not None:
        _update_pool_metrics(_pool)
        return _pool

    with _pool_lock:
        # Double-check after acquiring lock
        if _pool is not None:
            _update_pool_metrics(_pool)
            return _pool

        min_size = settings.db_min_connections
        max_size = min(settings.db_max_connections, 4)

        logger.info(
            "Creating asyncpg pool",
            extra={"min_size": min_size, "max_size": max_size},
        )
        try:
            _pool = run_async(_create_pool)
            _update_pool_metrics(_pool)
            logger.info(
                "asyncpg pool ready",
                extra={
                    "pool_size": _pool.get_size(),
                    "idle": _pool.get_idle_size(),
                },
            )
        except Exception as e:
            logger.error(
                "asyncpg pool creation FAILED",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise

    return _pool


# ── QdrantClient ────────────────────────────────────────────────

def get_qdrant() -> Optional[QdrantClient]:
    """Получить или создать QdrantClient (sync, per process).

    Если qdrant_enabled=False — возвращает None.
    Задачи должны проверять: if get_qdrant() is None → fallback.

    Lazy init: создаётся при первом вызове.
    Thread safety: single-threaded prefork, guard не нужен.
    """
    global _qdrant
    if _qdrant is not None:
        return _qdrant

    if not settings.qdrant_enabled:
        return None

    logger.info("Creating QdrantClient", extra={"url": settings.qdrant_url})
    try:
        # Парсим URL для host/port — consistent с vector/__init__.py
        url = settings.qdrant_url.replace("http://", "").replace("https://", "")
        host, port_str = url.split(":")
        port = int(port_str.rstrip("/"))
        _qdrant = QdrantClient(host=host, port=port, timeout=30)
        logger.info("QdrantClient ready")
    except Exception as e:
        logger.error(
            "QdrantClient creation FAILED",
            extra={"error": str(e), "url": settings.qdrant_url},
        )
        raise

    return _qdrant


# ── EmbeddingClient ─────────────────────────────────────────────

def get_embedding():
    """Получить или создать EmbeddingClient (lazy init).

    Объект создаётся при первом вызове, но httpx.AsyncClient
    и проверка dimension происходят при первом embed() — truly lazy.

    Hash-задачи могут не вызывать get_embedding() вообще.
    Thread safety: single-threaded prefork, guard не нужен.
    """
    global _embedding
    if _embedding is not None:
        return _embedding

    from memory_server.embedding.client import EmbeddingClient

    logger.info(
        "Creating EmbeddingClient",
        extra={
            "api_url": settings.embedding_api_url,
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
        },
    )
    try:
        _embedding = EmbeddingClient(
            api_url=settings.embedding_api_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
        logger.info("EmbeddingClient ready (httpx lazy)")
    except Exception as e:
        logger.error(
            "EmbeddingClient creation FAILED",
            extra={"error": str(e), "api_url": settings.embedding_api_url},
        )
        raise

    return _embedding


# ── Cleanup ─────────────────────────────────────────────────────

def close_all():
    """Закрыть все worker-scoped singletons.

    Вызывается при worker_process_shutdown.
    Порядок: EmbeddingClient → QdrantClient → asyncpg pool.
    (EmbeddingClient последний, т.к. может быть idle дольше всех.)

    Error handling: каждое закрытие обёрнуто в try/except,
    чтобы одно не удалось закрыть не повлияло на остальные.
    """
    global _pool, _qdrant, _embedding

    if _embedding is not None:
        logger.info("Closing EmbeddingClient")
        try:
            run_async(_embedding.aclose)
        except Exception as e:
            logger.warning(
                "EmbeddingClient close failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
        _embedding = None
        logger.info("EmbeddingClient closed")

    if _qdrant is not None:
        logger.info("Closing QdrantClient")
        try:
            _qdrant.close()
        except Exception as e:
            logger.warning(
                "QdrantClient close failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
        _qdrant = None
        logger.info("QdrantClient closed")

    if _pool is not None:
        logger.info("Closing asyncpg pool")
        try:
            run_async(_pool.close)
        except Exception as e:
            logger.warning(
                "asyncpg pool close failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
        _pool = None
        logger.info("asyncpg pool closed")

    # Reset metrics
    try:
        from memory_server.metrics import DB_POOL_SIZE, DB_POOL_AVAILABLE

        DB_POOL_SIZE.set(0)
        DB_POOL_AVAILABLE.set(0)
    except Exception:
        pass

    # Закрыть persistent event loop (после всех async ресурсов)
    from memory_server.tasks.async_bridge import close_worker_loop
    close_worker_loop()


# ── Health check ────────────────────────────────────────────────

def check_pool_health() -> dict:
    """Проверить здоровье asyncpg pool.

    Возвращает dict со статусом для /health endpoint.
    Используется в memory_server/__main__.py.
    """
    global _pool
    if _pool is None:
        return {"status": "not_initialized", "pool": None}

    try:
        return {
            "status": "ok",
            "pool_size": _pool.get_size(),
            "idle": _pool.get_idle_size(),
            "min_size": settings.db_min_connections,
            "max_size": min(settings.db_max_connections, 4),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Celery signals ──────────────────────────────────────────────

def setup_connection_signals(app):
    """Подключить сигналы lifecycle к Celery app.

    Вызывается из celery_app.py:
        from memory_server.tasks.connections import setup_connection_signals
        setup_connection_signals(app)

    Сигналы:
        worker_process_init → get_pool() + get_qdrant()
        worker_process_shutdown → close_all()
    """
    from celery.signals import worker_process_init, worker_process_shutdown

    @worker_process_init.connect
    def on_worker_init(**kwargs):
        """Worker process started — инициализируем логирование и ресурсы."""
        # ArgentaFormatter ДОЛЖЕН быть установлен ПЕРЕД любым другим логированием
        from memory_server.tasks.logging_config import setup_worker_logging
        setup_worker_logging()

        logger.info("worker_process_init: creating connections")
        get_pool()
        get_qdrant()
        # EmbeddingClient — lazy, создаётся при первом обращении задачей
        logger.info("worker_process_init: connections ready")

    @worker_process_shutdown.connect
    def on_worker_shutdown(**kwargs):
        """Worker process shutting down — закрываем ресурсы."""
        logger.info("worker_process_shutdown: closing connections")
        close_all()
        logger.info("worker_process_shutdown: connections closed")

    logger.info("Connection lifecycle signals connected")
