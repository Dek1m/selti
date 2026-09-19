"""SeltiState — composition root selti (план редизайна, Приложение A, Вариант B).

Единый владелец ресурсов и сервисов процесса по образцу mia Application:
web-процесс и каждый Celery-воркер создают один SeltiState (лениво, через
get_state()) и получают из него ВСЕ зависимости — pool, Redis, Qdrant,
embedding, сервисы. Разрозненных фабрик по коду больше нет.

Ленивые синглтоны: первое обращение создаёт ресурс, дальше — кеш инстанса.
Async-ресурсы живут на event loop процесса (web — uvicorn loop,
воркер — persistent loop из async_bridge).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

import asyncpg
import redis.asyncio as aioredis

from memory_server.config import settings
from memory_server.logger import get_logger

logger = get_logger(__name__)

# Cap соединений на процесс: 4 воркера × 4 = 16 при PG max_connections=100
_MAX_POOL_PER_PROCESS = 4

# Таймауты на Redis-операции (health-проверки, кеши)
_REDIS_TIMEOUT = 10.0


class SeltiState:
    """Composition root: инфраструктура + реестр сервисов процесса.

    Ленивые синглтоны с кешем инстанса; aclose() закрывает всё в порядке,
    обратном зависимостям. Бизнес-состояния не хранит — сервисы stateless.
    """

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None
        self._redis: Optional[aioredis.Redis] = None
        self._qdrant: Optional["CircuitBreakerQdrantClient"] = None
        self._embedding: Optional["EmbeddingClient"] = None
        self._memory_service: Optional["MemoryService"] = None
        self._hash_repository: Optional["HashRepository"] = None
        self._namespace_repository: Optional["NamespaceRepository"] = None
        self._project_repository: Optional["ProjectRepository"] = None
        # Отдельные lock'и: pool не захватывается под services_lock → дедлока нет
        self._pool_lock = asyncio.Lock()
        self._services_lock = asyncio.Lock()
        # sync-фабрики (QdrantClient, EmbeddingClient) — защита от гонки потоков
        self._sync_lock = threading.Lock()

    # ════════════════════════ Инфраструктура ════════════════════════

    async def get_pool(self) -> asyncpg.Pool:
        """asyncpg pool. DSN-трансформация, jsonb codec, statement_timeout — в db.pool."""
        if self._pool is not None:
            self._update_pool_metrics(self._pool)
            return self._pool
        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            from memory_server.db.pool import create_pool

            min_size = settings.db_min_connections
            max_size = min(settings.db_max_connections, _MAX_POOL_PER_PROCESS)
            logger.info("Creating asyncpg pool", extra={"min_size": min_size, "max_size": max_size})
            try:
                self._pool = await create_pool(
                    dsn=settings.database_url,
                    min_size=min_size,
                    max_size=max_size,
                )
            except Exception as exc:
                logger.error(
                    "asyncpg pool creation FAILED",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )
                raise
            self._update_pool_metrics(self._pool)
            logger.info(
                "asyncpg pool ready",
                extra={"pool_size": self._pool.get_size(), "idle": self._pool.get_idle_size()},
            )
        return self._pool

    async def get_redis(self) -> aioredis.Redis:
        """Redis-клиент процесса (health-проверки, кеши). Подключение ленивое."""
        if self._redis is not None:
            return self._redis
        async with self._pool_lock:
            if self._redis is None:
                self._redis = aioredis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_timeout=_REDIS_TIMEOUT,
                    socket_connect_timeout=_REDIS_TIMEOUT,
                )
                logger.info("Redis client ready", extra={"url": settings.redis_url})
        return self._redis

    def get_qdrant(self) -> Optional["CircuitBreakerQdrantClient"]:
        """QdrantClient с circuit breaker (sync). None, если qdrant_enabled=False."""
        if self._qdrant is not None:
            return self._qdrant
        if not settings.qdrant_enabled:
            return None
        with self._sync_lock:
            if self._qdrant is not None:
                return self._qdrant
            from qdrant_client import QdrantClient

            from memory_server.vector.circuit_breaker import CircuitBreakerQdrantClient

            logger.info("Creating QdrantClient", extra={"url": settings.qdrant_url})
            try:
                # Парсим URL на host/port — единый формат с настройками деплоя
                url = settings.qdrant_url.replace("http://", "").replace("https://", "")
                host, port_str = url.split(":")
                client = QdrantClient(host=host, port=int(port_str.rstrip("/")), timeout=30)
                self._qdrant = CircuitBreakerQdrantClient(client)
                logger.info("QdrantClient ready (circuit breaker enabled)")
            except Exception as exc:
                logger.error(
                    "QdrantClient creation FAILED",
                    extra={"error": str(exc), "url": settings.qdrant_url},
                )
                raise
        return self._qdrant

    def get_embedding_client(self) -> "EmbeddingClient":
        """EmbeddingClient: объект сейчас, httpx — лениво при первом embed()."""
        if self._embedding is not None:
            return self._embedding
        with self._sync_lock:
            if self._embedding is None:
                from memory_server.embedding.client import EmbeddingClient

                logger.info(
                    "Creating EmbeddingClient",
                    extra={
                        "api_url": settings.embedding_api_url,
                        "model": settings.embedding_model,
                        "dimension": settings.embedding_dimension,
                    },
                )
                self._embedding = EmbeddingClient(
                    api_url=settings.embedding_api_url,
                    api_key=settings.embedding_api_key,
                    model=settings.embedding_model,
                    dimension=settings.embedding_dimension,
                )
        return self._embedding

    # ════════════════════════ Реестр сервисов ═══════════════════════

    async def get_namespace_repository(self) -> "NamespaceRepository":
        """Реестр namespaces с TTL-кешем (общий для всех сервисов процесса)."""
        if self._namespace_repository is not None:
            return self._namespace_repository
        pool = await self.get_pool()
        async with self._services_lock:
            if self._namespace_repository is None:
                from memory_server.memory.namespace_repository import NamespaceRepository

                self._namespace_repository = NamespaceRepository(pool)
        return self._namespace_repository

    async def get_hash_repository(self) -> "HashRepository":
        """Репозиторий resource-хешей."""
        if self._hash_repository is not None:
            return self._hash_repository
        pool = await self.get_pool()
        async with self._services_lock:
            if self._hash_repository is None:
                from memory_server.memory.hash_repository import HashRepository

                self._hash_repository = HashRepository(pool)
        return self._hash_repository

    async def get_project_repository(self) -> "ProjectRepository":
        """Реестр проектов: slug/UUID → project_id с TTL-кешем."""
        if self._project_repository is not None:
            return self._project_repository
        pool = await self.get_pool()
        async with self._services_lock:
            if self._project_repository is None:
                from memory_server.memory.project_repository import ProjectRepository

                self._project_repository = ProjectRepository(pool)
        return self._project_repository

    async def get_memory_service(self) -> "MemoryService":
        """MemoryService: PG + Qdrant (fallback SQL) + dedup + namespaces."""
        if self._memory_service is not None:
            return self._memory_service
        pool = await self.get_pool()
        async with self._services_lock:
            if self._memory_service is not None:
                return self._memory_service
            from memory_server.memory.dedup import DedupEngine
            from memory_server.memory.namespace_repository import NamespaceRepository
            from memory_server.memory.pg_repository import PostgreSQLRepository
            from memory_server.memory.qdrant_store import QdrantStore
            from memory_server.memory.repository import MemoryRepository
            from memory_server.memory.service import MemoryService

            qdrant_client = self.get_qdrant()
            qdrant = (
                QdrantStore(qdrant_client, collection=settings.qdrant_collection)
                if qdrant_client
                else None
            )
            if self._namespace_repository is None:
                self._namespace_repository = NamespaceRepository(pool)
            if self._project_repository is None:
                from memory_server.memory.project_repository import ProjectRepository

                self._project_repository = ProjectRepository(pool)
            repository = MemoryRepository(
                pg=PostgreSQLRepository(pool),
                qdrant=qdrant,
                ns_repo=self._namespace_repository,
            )
            self._memory_service = MemoryService(
                repository=repository,
                embedding_provider=self.get_embedding_client(),
                namespace_repository=self._namespace_repository,
                config=settings,
                project_repository=self._project_repository,
                redis_provider=self.get_redis,
            )
        return self._memory_service

    # ════════════════════════ Shutdown ═══════════════════════

    async def aclose(self) -> None:
        """Graceful shutdown: закрыть ресурсы в порядке, обратном зависимостям.

        Embedding (httpx) → Qdrant → Redis → asyncpg pool. Ошибка закрытия
        одного ресурса не мешает закрыть остальные.
        """
        if self._embedding is not None:
            try:
                await self._embedding.aclose()
            except Exception as exc:
                logger.warning(
                    "EmbeddingClient close failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )
            self._embedding = None

        if self._qdrant is not None:
            try:
                self._qdrant.close()
            except Exception as exc:
                logger.warning(
                    "QdrantClient close failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )
            self._qdrant = None

        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.warning(
                    "Redis close failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )
            self._redis = None

        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception as exc:
                logger.warning(
                    "asyncpg pool close failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )
            self._pool = None
            self._reset_pool_metrics()

        # Сервисы — чистые ссылки на закрытые ресурсы, просто сбрасываем
        self._memory_service = None
        self._hash_repository = None
        self._namespace_repository = None
        self._project_repository = None
        logger.info("SeltiState closed")

    # ════════════════════════ Метрики ═══════════════════════

    @staticmethod
    def _update_pool_metrics(pool: asyncpg.Pool) -> None:
        """Gauge размера/занятости pool. Best effort — метрики не роняют работу."""
        try:
            from memory_server.metrics import DB_POOL_AVAILABLE, DB_POOL_SIZE

            DB_POOL_SIZE.set(pool.get_size())
            DB_POOL_AVAILABLE.set(pool.get_idle_size())
        except Exception:
            pass

    @staticmethod
    def _reset_pool_metrics() -> None:
        try:
            from memory_server.metrics import DB_POOL_AVAILABLE, DB_POOL_SIZE

            DB_POOL_SIZE.set(0)
            DB_POOL_AVAILABLE.set(0)
        except Exception:
            pass


# ── Per-process singleton ────────────────────────────────────────

_state: Optional[SeltiState] = None
_state_lock = threading.Lock()


def get_state() -> SeltiState:
    """Единственный SeltiState процесса (web-процесс / Celery-воркер)."""
    global _state
    if _state is None:
        with _state_lock:
            if _state is None:
                _state = SeltiState()
    return _state


# ── Celery worker lifecycle ──────────────────────────────────────


def setup_worker_signals(app) -> None:
    """Прогрев SeltiState при старте воркер-процесса, aclose при остановке.

    Вызывается из celery_app.py:
        from memory_server.state import setup_worker_signals
        setup_worker_signals(app)
    """
    from celery.signals import setup_logging as celery_setup_logging
    from celery.signals import worker_process_init, worker_process_shutdown

    from memory_server.tasks.async_bridge import close_worker_loop, run_async

    # Перехват setup_logging: True = Celery не добавляет свой handler.
    # Наш форматтер (ArgentaFormatter) ставится в worker_process_init.
    @celery_setup_logging.connect(weak=False)
    def on_setup_logging(**kwargs):
        return True

    @worker_process_init.connect(weak=False)
    def on_worker_init(**kwargs):
        # Форматтер ДОЛЖЕН стоять до первого лога задачи
        from memory_server.tasks.logging_config import setup_worker_logging

        setup_worker_logging()

        logger.info("worker_process_init: warming up state")
        state = get_state()
        run_async(state.get_pool)
        state.get_qdrant()
        # EmbeddingClient — ленивый, создаётся при первом embed()
        logger.info("worker_process_init: state ready")

    @worker_process_shutdown.connect(weak=False)
    def on_worker_shutdown(**kwargs):
        logger.info("worker_process_shutdown: closing state")
        run_async(get_state().aclose)
        # Persistent loop закрываем последним — ресурсы на нём уже закрыты
        close_worker_loop()
        logger.info("worker_process_shutdown: state closed")

    logger.info("Worker lifecycle signals connected")
