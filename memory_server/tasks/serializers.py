"""JSON serialization for Celery task arguments.

Handles Pydantic models, datetime, UUID, Decimal, and nested structures.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID


class SeltiJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for selti types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, Decimal):
            return float(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if hasattr(obj, "dict"):
            return obj.dict()
        return super().default(obj)


def serialize(data: Any) -> Any:
    """Serialize data to JSON-compatible dict.

    Handles nested structures, Pydantic models, datetime, UUID, Decimal.
    """
    return json.loads(json.dumps(data, cls=SeltiJSONEncoder))


def serialize_model(model: Any) -> dict[str, Any]:
    """Serialize a Pydantic model to dict."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if hasattr(model, "dict"):
        return model.dict()
    raise TypeError(f"Cannot serialize {type(model).__name__}")


def serialize_datetime(dt: datetime | None) -> str | None:
    """Serialize datetime to ISO format string."""
    if dt is None:
        return None
    return dt.isoformat()
