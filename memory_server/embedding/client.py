import asyncio
import time
from typing import Optional

import httpx
import structlog

from memory_server.cache.redis_client import EmbeddingCache
from memory_server.embedding.provider import EmbeddingProvider
from memory_server.exceptions import EmbeddingError
from memory_server.metrics import (
    EMBEDDING_CACHE_HITS,
    EMBEDDING_CACHE_MISSES,
    EMBEDDING_CACHE_HIT_RATIO,
    EMBEDDING_DURATION,
)

logger = structlog.get_logger()


class EmbeddingClient(EmbeddingProvider):
    """OpenAI-compatible embedding API client."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        dimension: int,
        cache: Optional[EmbeddingCache] = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._client_lock = asyncio.Lock()
        self._dimension_verified: bool = False
        self._cache = cache
        self.cache_hits: int = 0
        self.cache_misses: int = 0

    async def _get_client(self) -> httpx.AsyncClient:
        # Fast path — клиент уже создан и loop тот же
        if self._client is not None and self._client_loop is asyncio.get_running_loop():
            return self._client

        async with self._client_lock:
            loop = asyncio.get_running_loop()
            # Double-check после acquiring lock
            if self._client is not None and self._client_loop is loop:
                return self._client

            # Закрываем старый клиент при смене loop
            if self._client is not None:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
                self._client = None

            self._client_loop = loop
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                headers=headers,
                timeout=httpx.Timeout(30.0),
            )
            if not self._dimension_verified:
                await self._verify_dimension()
                self._dimension_verified = True
            return self._client

    async def _verify_dimension(self) -> None:
        """Verify the embedding dimension matches configuration."""
        test_embedding = await self._request_embedding("verify")
        actual = len(test_embedding)
        if actual != self.dimension:
            logger.warning(
                "Embedding dimension mismatch",
                extra={"configured": self.dimension, "actual": actual},
            )
            self.dimension = actual

    async def _request_embedding(self, text: str) -> list[float]:
        client = await self._get_client()
        start = time.monotonic()
        response = await client.post(
            "/embeddings",
            json={"model": self.model, "input": text},
        )
        duration = time.monotonic() - start
        EMBEDDING_DURATION.observe(duration)
        if response.status_code != 200:
            detail = response.text
            try:
                body = response.json()
                detail = body.get("error", {}).get("message", detail)
            except Exception:
                pass
            raise EmbeddingError(status_code=response.status_code, detail=detail)
        data = response.json()
        return data["data"][0]["embedding"]

    def _update_hit_ratio(self) -> None:
        """Обновить gauge cache hit ratio из instance-level счётчиков."""
        total = self.cache_hits + self.cache_misses
        if total > 0:
            EMBEDDING_CACHE_HIT_RATIO.set(self.cache_hits / total)

    async def embed(self, text: str) -> list[float]:
        if self._cache is not None:
            cached = await self._cache.get(text)
            if cached is not None:
                self.cache_hits += 1
                EMBEDDING_CACHE_HITS.inc()
                self._update_hit_ratio()
                return cached
            self.cache_misses += 1
            EMBEDDING_CACHE_MISSES.inc()

        embedding = await self._request_embedding(text)

        if self._cache is not None:
            await self._cache.set(text, embedding)

        return embedding

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if self._cache is not None:
            cached = await self._cache.mget(texts)
            miss_indices = [i for i, v in enumerate(cached) if v is None]
            miss_texts = [texts[i] for i in miss_indices]

            if miss_texts:
                self.cache_misses += len(miss_texts)
                EMBEDDING_CACHE_MISSES.inc(len(miss_texts))
                miss_embeddings = await self._batch_request(miss_texts)
                await self._cache.mset(list(zip(miss_texts, miss_embeddings)))
                for i, emb in zip(miss_indices, miss_embeddings):
                    cached[i] = emb

            hits = len(texts) - len(miss_texts)
            self.cache_hits += hits
            if hits:
                EMBEDDING_CACHE_HITS.inc(hits)
            self._update_hit_ratio()
            return cached
        else:
            return await self._batch_request(texts)

    async def _batch_request(self, texts: list[str]) -> list[list[float]]:
        client = await self._get_client()
        start = time.monotonic()
        response = await client.post(
            "/embeddings",
            json={"model": self.model, "input": texts},
        )
        duration = time.monotonic() - start
        EMBEDDING_DURATION.observe(duration)
        if response.status_code != 200:
            detail = response.text
            try:
                body = response.json()
                detail = body.get("error", {}).get("message", detail)
            except Exception:
                pass
            raise EmbeddingError(status_code=response.status_code, detail=detail)
        data = response.json()
        data["data"].sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data["data"]]

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def __aenter__(self):
        await self._get_client()
        return self

    async def __aexit__(self, *args):
        await self.aclose()
