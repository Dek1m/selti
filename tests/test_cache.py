"""Тесты для EmbeddingCache и EmbeddingClient с кешем.

EmbeddingCache (8 тестов):
  1. get() возвращает None при промахе
  2. set() + get() — сохраняет и возвращает эмбеддинг
  3. mget() — все найдены
  4. mget() — частичное попадание
  5. mget() — ничего не найдено
  6. mset() + mget() — batch запись и чтение
  7. TTL — устанавливается (проверяем через вызов setex)
  8. close() — закрывает соединение без ошибок
  9. Ключ формируется как embedding:{sha256hex} — формат
  10. Разные тексты дают разные ключи

EmbeddingClient с кешем (4 теста):
  11. embed() — при hit возвращает из кеша, не вызывает API
  12. embed() — при miss вызывает API и сохраняет в кеш
  13. embed_many() — batch с частичным hit/miss
  14. cache_hits/cache_misses счётчики обновляются
"""

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_server.cache.redis_client import EmbeddingCache
from memory_server.embedding.client import EmbeddingClient


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def mock_redis():
    """Fixture: мокнутый Redis client."""
    client = AsyncMock()
    client.get = AsyncMock()
    client.setex = AsyncMock()
    client.mget = AsyncMock()

    # pipeline() должен быть обычной функцией (не корутиной), возвращающей async context manager
    pipe = AsyncMock()
    pipe.__aenter__.return_value = pipe
    pipe.__aexit__.return_value = None
    client.pipeline = MagicMock(return_value=pipe)

    client.close = AsyncMock()
    return client


@pytest.fixture
def cache(mock_redis):
    """Fixture: EmbeddingCache с замоканным Redis."""
    c = EmbeddingCache(redis_url="redis://localhost:6379/0")
    c._client = mock_redis  # устанавливаем напрямую, чтобы _get_client() вернул его
    return c


@pytest.fixture
def cache_with_mock_redis(mock_redis):
    """Явный cache — чтобы тесты EmbeddingClient могли получить и cache, и mock_redis."""
    c = EmbeddingCache(redis_url="redis://localhost:6379/0")
    c._client = mock_redis
    return c


@pytest.fixture
def client_with_cache(cache_with_mock_redis, mock_redis):
    """Fixture: EmbeddingClient с кешем. mock_redis доступен через request."""
    client = EmbeddingClient(
        api_url="http://test-embed:8080/v1",
        api_key="test-key",
        model="test-model",
        dimension=3,
        cache=cache_with_mock_redis,
    )
    # Отключаем verify, чтобы не путать запросы
    client._verify_dimension = AsyncMock()
    return client


# =========================================================================
# EmbeddingCache — базовые операции
# =========================================================================

class TestEmbeddingCacheBase:
    """Базовые get/set/mget/mset операции."""

    @pytest.mark.asyncio
    async def test_get_returns_none_on_miss(self, cache, mock_redis):
        """get() возвращает None при промахе кеша."""
        mock_redis.get.return_value = None
        result = await cache.get("hello")
        assert result is None
        mock_redis.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_and_get(self, cache, mock_redis):
        """set() + get() — сохраняет и возвращает эмбеддинг."""
        text = "hello"
        embedding = [0.1, 0.2, 0.3]
        key = cache._make_key(text)
        cached_json = json.dumps(embedding)

        await cache.set(text, embedding)
        mock_redis.setex.assert_awaited_once_with(key, cache.ttl, cached_json)

        mock_redis.get.return_value = cached_json
        result = await cache.get(text)
        assert result == embedding

    @pytest.mark.asyncio
    async def test_mget_all_found(self, cache, mock_redis):
        """mget() возвращает все эмбеддинги, если все найдены."""
        texts = ["a", "b"]
        emb_a, emb_b = [0.1, 0.2], [0.3, 0.4]
        mock_redis.mget.return_value = [json.dumps(emb_a), json.dumps(emb_b)]

        results = await cache.mget(texts)
        assert results == [emb_a, emb_b]

    @pytest.mark.asyncio
    async def test_mget_partial_hit(self, cache, mock_redis):
        """mget() возвращает список с None для промахов."""
        texts = ["a", "b", "c"]
        emb_a = [0.1, 0.2]
        mock_redis.mget.return_value = [json.dumps(emb_a), None, None]

        results = await cache.mget(texts)
        assert results == [emb_a, None, None]

    @pytest.mark.asyncio
    async def test_mget_all_miss(self, cache, mock_redis):
        """mget() возвращает список None, если ничего не найдено."""
        texts = ["x", "y"]
        mock_redis.mget.return_value = [None, None]

        results = await cache.mget(texts)
        assert results == [None, None]

    @pytest.mark.asyncio
    async def test_mset_and_mget(self, cache, mock_redis):
        """mset() сохраняет пары, mget() их находит."""
        pairs = [("hello", [0.1, 0.2]), ("world", [0.3, 0.4])]
        await cache.mset(pairs)

        # mset использует pipeline
        mock_redis.pipeline.assert_called_once()
        pipe = mock_redis.pipeline.return_value
        assert pipe.setex.await_count == 2
        pipe.execute.assert_awaited_once()

        # mget
        mock_redis.mget.return_value = [json.dumps([0.1, 0.2]), json.dumps([0.3, 0.4])]
        results = await cache.mget(["hello", "world"])
        assert results == [[0.1, 0.2], [0.3, 0.4]]


class TestEmbeddingCacheTTLAndClose:
    """Проверка TTL и close."""

    @pytest.mark.asyncio
    async def test_ttl_is_set(self, cache, mock_redis):
        """set() вызывает setex с правильным TTL."""
        text = "ttl-test"
        embedding = [0.5, 0.6]
        await cache.set(text, embedding)

        key = cache._make_key(text)
        mock_redis.setex.assert_awaited_once_with(
            key,
            cache.ttl,
            json.dumps(embedding),
        )

    @pytest.mark.asyncio
    async def test_close_calls_redis_close(self, cache, mock_redis):
        """close() вызывает client.close() и обнуляет _client."""
        await cache._get_client()  # инициализируем клиент
        await cache.close()
        mock_redis.close.assert_awaited_once()
        assert cache._client is None

    @pytest.mark.asyncio
    async def test_close_no_client_does_not_raise(self, cache):
        """close() без инициализации клиента не вызывает ошибок."""
        # _client is None
        await cache.close()  # должно пройти без ошибок


class TestEmbeddingCacheKeyFormat:
    """Проверка формата ключей."""

    def test_key_format(self, cache):
        """Ключ формируется как embedding:{sha256hex}."""
        text = "test-text"
        key = cache._make_key(text)
        expected_hash = hashlib.sha256(text.encode()).hexdigest()
        assert key == f"embedding:{expected_hash}"
        # SHA256 hex — 64 символа
        assert len(expected_hash) == 64

    def test_different_texts_different_keys(self, cache):
        """Разные тексты дают разные ключи."""
        key_a = cache._make_key("hello")
        key_b = cache._make_key("world")
        assert key_a != key_b

    def test_same_text_same_key(self, cache):
        """Один и тот же текст даёт одинаковый ключ."""
        key_a = cache._make_key("hello")
        key_b = cache._make_key("hello")
        assert key_a == key_b


# =========================================================================
# EmbeddingClient с кешем
# =========================================================================

class TestEmbeddingClientWithCache:
    """EmbeddingClient с подключённым EmbeddingCache."""

    @pytest.mark.asyncio
    async def test_embed_hit_returns_from_cache(self, client_with_cache, mock_redis):
        """При попадании в кеш embed() возвращает данные из кеша и не вызывает API."""
        text = "cached-text"
        expected = [0.42, 0.43, 0.44]

        # cache hit
        mock_redis.get.return_value = json.dumps(expected)

        with patch.object(client_with_cache, '_request_embedding') as mock_request:
            result = await client_with_cache.embed(text)

        assert result == expected
        mock_request.assert_not_called()
        assert client_with_cache.cache_hits == 1
        assert client_with_cache.cache_misses == 0

    @pytest.mark.asyncio
    async def test_embed_miss_calls_api_and_caches(self, client_with_cache, mock_redis, httpx_mock):
        """При промахе embed() вызывает API и сохраняет результат в кеш."""
        text = "miss-text"
        expected = [0.7, 0.8, 0.9]

        # cache miss
        mock_redis.get.return_value = None

        # API response
        httpx_mock.add_response(
            url="http://test-embed:8080/v1/embeddings",
            method="POST",
            json={
                "data": [{"index": 0, "embedding": expected, "object": "embedding"}],
                "model": "test-model",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

        result = await client_with_cache.embed(text)

        assert result == expected
        # Проверяем, что сохранилось в кеш
        key = client_with_cache._cache._make_key(text)
        mock_redis.setex.assert_awaited_with(key, 86400, json.dumps(expected))
        assert client_with_cache.cache_hits == 0
        assert client_with_cache.cache_misses == 1

    @pytest.mark.asyncio
    async def test_embed_many_partial_hit_miss(self, client_with_cache, mock_redis, httpx_mock):
        """embed_many с частичным попаданием — miss-тексты уходят в API."""
        texts = ["hit-text", "miss-text"]
        hit_emb = [0.1, 0.2, 0.3]
        miss_emb = [0.4, 0.5, 0.6]

        # mget: hit-text найден, miss-text — нет
        mock_redis.mget.return_value = [json.dumps(hit_emb), None]

        # API ответит на miss-text
        httpx_mock.add_response(
            url="http://test-embed:8080/v1/embeddings",
            method="POST",
            json={
                "data": [{"index": 0, "embedding": miss_emb, "object": "embedding"}],
                "model": "test-model",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

        result = await client_with_cache.embed_many(texts)

        assert result == [hit_emb, miss_emb]
        # mset через pipeline должен сохранить miss-text
        miss_key = client_with_cache._cache._make_key("miss-text")
        pipe = mock_redis.pipeline.return_value
        pipe.setex.assert_awaited_with(miss_key, 86400, json.dumps(miss_emb))
        assert client_with_cache.cache_hits == 1
        assert client_with_cache.cache_misses == 1

    @pytest.mark.asyncio
    async def test_cache_counters_updated(self, client_with_cache, mock_redis, httpx_mock):
        """Счётчики cache_hits и cache_misses обновляются корректно."""
        text_hit = "known"
        text_miss = "unknown"
        known_emb = [0.9, 0.9, 0.9]
        unknown_emb = [0.1, 0.1, 0.1]

        # Первый запрос — miss (get → None), потом API
        mock_redis.get.return_value = None
        httpx_mock.add_response(
            url="http://test-embed:8080/v1/embeddings",
            method="POST",
            json={
                "data": [{"index": 0, "embedding": unknown_emb, "object": "embedding"}],
                "model": "test-model",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )
        await client_with_cache.embed(text_miss)
        assert client_with_cache.cache_hits == 0
        assert client_with_cache.cache_misses == 1

        # Второй запрос — hit (get → эмбеддинг)
        mock_redis.get.return_value = json.dumps(known_emb)
        await client_with_cache.embed(text_hit)
        assert client_with_cache.cache_hits == 1
        assert client_with_cache.cache_misses == 1

    @pytest.mark.asyncio
    async def test_embed_without_cache_calls_api(self, httpx_mock):
        """EmbeddingClient без кеша работает как обычно, ходит в API."""
        client = EmbeddingClient(
            api_url="http://test-embed:8080/v1",
            api_key="",
            model="m",
            dimension=2,
            cache=None,
        )
        client._verify_dimension = AsyncMock()

        httpx_mock.add_response(
            url="http://test-embed:8080/v1/embeddings",
            method="POST",
            json={
                "data": [{"index": 0, "embedding": [0.5, 0.5], "object": "embedding"}],
                "model": "m",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

        result = await client.embed("no-cache")
        assert result == [0.5, 0.5]
