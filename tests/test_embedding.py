import json

import httpx
import pytest

from memory_server.embedding.client import EmbeddingClient
from memory_server.exceptions import EmbeddingError


@pytest.fixture
def client():
    return EmbeddingClient(
        api_url="http://test-embed:8080/v1",
        api_key="test-key",
        model="test-model",
        dimension=3,
    )


def _add_verify_response(httpx_mock, dimension=3):
    """Add a response for the _verify_dimension call (happens on first embed)."""
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [{"index": 0, "embedding": [0.0] * dimension, "object": "embedding"}],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


@pytest.mark.asyncio
async def test_embed_returns_vector(httpx_mock, client):
    """embed should POST to /embeddings and return the embedding vector."""
    _add_verify_response(httpx_mock, dimension=3)
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3], "object": "embedding"}],
            "model": "test-model",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        },
    )

    vector = await client.embed("hello")
    assert vector == [0.1, 0.2, 0.3]

    # Filter the actual embed request (second one, after verify)
    requests = httpx_mock.get_requests(method="POST", url="http://test-embed:8080/v1/embeddings")
    actual_request = [r for r in requests if json.loads(r.content).get("input") == "hello"][0]
    assert json.loads(actual_request.content) == {"model": "test-model", "input": "hello"}


@pytest.mark.asyncio
async def test_embed_many_returns_list_of_vectors(httpx_mock, client):
    """embed_many should return a list of embedding vectors sorted by index."""
    _add_verify_response(httpx_mock, dimension=3)
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [
                {"index": 1, "embedding": [0.4, 0.5, 0.6], "object": "embedding"},
                {"index": 0, "embedding": [0.1, 0.2, 0.3], "object": "embedding"},
            ],
            "model": "test-model",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        },
    )

    vectors = await client.embed_many(["hello", "world"])
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


@pytest.mark.asyncio
async def test_verify_dimension_updates_on_mismatch(httpx_mock):
    """When _verify_dimension finds actual != configured, it updates self.dimension."""
    client = EmbeddingClient(
        api_url="http://test-embed:8080/v1",
        api_key="",
        model="test-model",
        dimension=999,  # intentionally wrong
    )

    # verify call (dimension=999, actual=3 — will update)
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3], "object": "embedding"}],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )
    # actual embed call
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3], "object": "embedding"}],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )

    # _verify_dimension is called inside _get_client, which is used by embed
    vector = await client.embed("x")
    assert vector == [0.1, 0.2, 0.3]
    assert client.dimension == 3  # updated from 999 to 3


@pytest.mark.asyncio
async def test_verify_dimension_unchanged_when_match(httpx_mock):
    """When dimension matches, _verify_dimension leaves it as is."""
    client = EmbeddingClient(
        api_url="http://test-embed:8080/v1",
        api_key="",
        model="test-model",
        dimension=3,  # correct
    )

    _add_verify_response(httpx_mock, dimension=3)
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3], "object": "embedding"}],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )

    vector = await client.embed("x")
    assert vector == [0.1, 0.2, 0.3]
    assert client.dimension == 3


@pytest.mark.asyncio
async def test_embed_api_error_raises_embedding_error(httpx_mock, client):
    """Non-200 response from embedding API should raise EmbeddingError."""
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        status_code=502,
        text="Bad Gateway",
    )

    with pytest.raises(EmbeddingError) as exc_info:
        await client.embed("broken")
    assert exc_info.value.status_code == 502
    assert "Bad Gateway" in str(exc_info.value)


@pytest.mark.asyncio
async def test_embed_api_error_with_json_body(httpx_mock, client):
    """If API returns JSON error body, the detail should be extracted."""
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        status_code=400,
        json={"error": {"message": "invalid input", "type": "invalid_request_error"}},
    )

    with pytest.raises(EmbeddingError) as exc_info:
        await client.embed("bad")
    assert exc_info.value.status_code == 400
    assert "invalid input" in str(exc_info.value)


@pytest.mark.asyncio
async def test_embed_many_api_error_raises_embedding_error(httpx_mock, client):
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        status_code=429,
        text="Too Many Requests",
    )

    with pytest.raises(EmbeddingError) as exc_info:
        await client.embed_many(["a", "b"])
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_embed_without_api_key(httpx_mock):
    """When api_key is empty, no Authorization header is sent."""
    client = EmbeddingClient(
        api_url="http://test-embed:8080/v1",
        api_key="",
        model="m",
        dimension=2,
    )

    _add_verify_response(httpx_mock, dimension=2)
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [{"index": 0, "embedding": [0.5, 0.5], "object": "embedding"}],
            "model": "m",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )

    await client.embed("hi")
    requests = httpx_mock.get_requests(method="POST", url="http://test-embed:8080/v1/embeddings")
    actual_request = [r for r in requests if json.loads(r.content).get("input") == "hi"][0]
    assert "Authorization" not in actual_request.headers


@pytest.mark.asyncio
async def test_embed_with_api_key(httpx_mock):
    """When api_key is set, Authorization: Bearer header is sent."""
    client = EmbeddingClient(
        api_url="http://test-embed:8080/v1",
        api_key="sk-secret",
        model="m",
        dimension=2,
    )

    _add_verify_response(httpx_mock, dimension=2)
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [{"index": 0, "embedding": [0.5, 0.5], "object": "embedding"}],
            "model": "m",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )

    await client.embed("hi")
    requests = httpx_mock.get_requests(method="POST", url="http://test-embed:8080/v1/embeddings")
    actual_request = [r for r in requests if json.loads(r.content).get("input") == "hi"][0]
    assert actual_request.headers["Authorization"] == "Bearer sk-secret"


@pytest.mark.asyncio
async def test_aclose_clears_client(httpx_mock, client):
    """aclose should close the underlying httpx client and set it to None."""
    # Force client initialisation — needs 2 responses (verify + warm-up)
    _add_verify_response(httpx_mock, dimension=3)
    httpx_mock.add_response(
        url="http://test-embed:8080/v1/embeddings",
        method="POST",
        json={
            "data": [{"index": 0, "embedding": [0.0, 0.0, 0.0], "object": "embedding"}],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )
    await client.embed("warm-up")
    assert client._client is not None

    await client.aclose()
    assert client._client is None
