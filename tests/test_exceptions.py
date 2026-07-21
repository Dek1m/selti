import pytest

from memory_server.exceptions import (
    DatabaseError,
    EmbeddingError,
    MemoryError,
    NotFoundError,
)


def test_memory_error_base():
    err = MemoryError("base error")
    assert str(err) == "base error"
    assert isinstance(err, Exception)


def test_not_found_error_default_message():
    err = NotFoundError(memory_id="mem-123")
    assert err.id == "mem-123"
    assert str(err) == "Memory record not found: mem-123"
    assert isinstance(err, MemoryError)


def test_not_found_error_custom_message():
    err = NotFoundError(memory_id="mem-456", message="Custom not found")
    assert err.id == "mem-456"
    assert str(err) == "Custom not found"


def test_embedding_error_default_message():
    err = EmbeddingError(status_code=500, detail="Internal Server Error")
    assert err.status_code == 500
    assert err.detail == "Internal Server Error"
    assert str(err) == "Embedding API error 500: Internal Server Error"
    assert isinstance(err, MemoryError)


def test_embedding_error_custom_message():
    err = EmbeddingError(status_code=401, detail="Unauthorized", message="Bad API key")
    assert err.status_code == 401
    assert str(err) == "Bad API key"


def test_database_error():
    err = DatabaseError("connection failed")
    assert str(err) == "connection failed"
    assert isinstance(err, MemoryError)
    assert isinstance(err, Exception)
