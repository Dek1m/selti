"""Tests for memory_server.tasks.serializers.

Coverage: SeltiJSONEncoder, serialize(), serialize_model(), serialize_datetime().
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import BaseModel

from memory_server.tasks.serializers import (
    SeltiJSONEncoder,
    serialize,
    serialize_datetime,
    serialize_model,
)


# ── Helpers ──────────────────────────────────────────────────────


class SampleModel(BaseModel):
    id: str
    name: str
    value: int = 0


class NestedModel(BaseModel):
    inner: SampleModel
    tags: list[str]


# ── SeltiJSONEncoder ────────────────────────────────────────────


class TestSeltiJSONEncoder:
    def test_datetime_to_iso(self):
        dt = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
        result = serialize({"ts": dt})
        assert result["ts"] == "2026-07-31T12:00:00+00:00"

    def test_uuid_to_string(self):
        uid = UUID("550e8400-e29b-41d4-a716-446655440000")
        result = serialize({"id": uid})
        assert result["id"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_decimal_to_float(self):
        d = Decimal("3.14159")
        result = serialize({"price": d})
        assert result["price"] == pytest.approx(3.14159)

    def test_pydantic_model_via_model_dump(self):
        model = SampleModel(id="m1", name="test", value=42)
        result = serialize({"model": model})
        assert result["model"] == {"id": "m1", "name": "test", "value": 42}

    def test_nested_pydantic_model(self):
        inner = SampleModel(id="inner-1", name="nested", value=7)
        outer = NestedModel(inner=inner, tags=["a", "b"])
        result = serialize({"data": outer})
        assert result["data"]["inner"]["id"] == "inner-1"
        assert result["data"]["tags"] == ["a", "b"]

    def test_mixed_nested_structure(self):
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        uid = UUID("550e8400-e29b-41d4-a716-446655440000")
        data = {
            "records": [
                {"ts": dt, "id": uid, "price": Decimal("9.99")},
            ]
        }
        result = serialize(data)
        assert result["records"][0]["ts"] == "2026-01-01T00:00:00+00:00"
        assert result["records"][0]["id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert result["records"][0]["price"] == 9.99

    def test_primitives_pass_through(self):
        assert serialize(42) == 42
        assert serialize("hello") == "hello"
        assert serialize(None) is None
        assert serialize(True) is True
        assert serialize([1, 2, 3]) == [1, 2, 3]

    def test_datetime_naive(self):
        dt = datetime(2026, 6, 15, 10, 30, 0)
        result = serialize({"ts": dt})
        # Naive datetime isoformat doesn't include timezone offset
        assert "2026-06-15T10:30:00" in result["ts"]


# ── serialize_model ─────────────────────────────────────────────


class TestSerializeModel:
    def test_model_dump(self):
        model = SampleModel(id="x", name="y", value=1)
        result = serialize_model(model)
        assert result == {"id": "x", "name": "y", "value": 1}

    def test_dict_fallback(self):
        """Legacy Pydantic v1 model with .dict() method."""

        class LegacyModel:
            def dict(self):
                return {"legacy": True}

        obj = LegacyModel()
        result = serialize_model(obj)
        assert result == {"legacy": True}

    def test_raises_on_non_serializable(self):
        with pytest.raises(TypeError, match="Cannot serialize"):
            serialize_model("not a model")


# ── serialize_datetime ──────────────────────────────────────────


class TestSerializeDatetime:
    def test_none_returns_none(self):
        assert serialize_datetime(None) is None

    def test_utc_datetime(self):
        dt = datetime(2026, 7, 31, 14, 30, 0, tzinfo=timezone.utc)
        result = serialize_datetime(dt)
        assert result == "2026-07-31T14:30:00+00:00"

    def test_naive_datetime(self):
        dt = datetime(2026, 1, 1, 0, 0, 0)
        result = serialize_datetime(dt)
        assert result == "2026-01-01T00:00:00"


# ── Edge cases ─────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_dict(self):
        assert serialize({}) == {}

    def test_empty_list(self):
        assert serialize([]) == []

    def test_deeply_nested(self):
        data = {"level1": {"level2": {"level3": {"value": 42}}}}
        result = serialize(data)
        assert result["level1"]["level2"]["level3"]["value"] == 42

    def test_list_of_pydantic_models(self):
        models = [SampleModel(id=str(i), name=f"item-{i}") for i in range(3)]
        result = serialize({"items": models})
        assert len(result["items"]) == 3
        assert result["items"][0]["name"] == "item-0"
