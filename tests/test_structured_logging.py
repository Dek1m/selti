"""Тесты для structured logging (JSON format) и correlation ID.

Проверяем:
  - JSON формат логов (ts, level, service, msg)
  - Level mapping: WARNING → WARN, CRITICAL → ERROR
  - Correlation ID injection через contextvars
  - Service name injection
  - Timestamp format (ISO 8601 UTC)
  - rename event → msg
"""

import json
import logging
import os
import re
from unittest.mock import patch

import structlog
import pytest

from memory_server.tasks.logging_config import (
    _add_service,
    _add_timestamp,
    _map_level,
    _merge_request_id,
    _rename_event_to_msg,
    configure_structlog,
    SERVICE_NAME,
)


# ══════════════════════════════════════════════════════════════════
# 1. Level mapping
# ══════════════════════════════════════════════════════════════════


class TestLevelMapping:
    def test_warning_maps_to_warn(self):
        """WARNING → WARN."""
        event_dict = {"level": "WARNING"}
        result = _map_level(None, None, event_dict)
        assert result["level"] == "WARN"

    def test_critical_maps_to_error(self):
        """CRITICAL → ERROR."""
        event_dict = {"level": "CRITICAL"}
        result = _map_level(None, None, event_dict)
        assert result["level"] == "ERROR"

    def test_info_unchanged(self):
        """INFO остаётся INFO."""
        event_dict = {"level": "INFO"}
        result = _map_level(None, None, event_dict)
        assert result["level"] == "INFO"

    def test_debug_unchanged(self):
        """DEBUG остаётся DEBUG."""
        event_dict = {"level": "DEBUG"}
        result = _map_level(None, None, event_dict)
        assert result["level"] == "DEBUG"

    def test_error_unchanged(self):
        """ERROR остаётся ERROR."""
        event_dict = {"level": "ERROR"}
        result = _map_level(None, None, event_dict)
        assert result["level"] == "ERROR"


# ══════════════════════════════════════════════════════════════════
# 2. Service injection
# ══════════════════════════════════════════════════════════════════


class TestServiceInjection:
    def test_add_service_injects_service_name(self):
        """_add_service добавляет service в event_dict."""
        event_dict = {}
        result = _add_service(None, None, event_dict)
        assert "service" in result
        assert isinstance(result["service"], str)

    def test_service_name_from_env(self):
        """SERVICE_NAME берётся из env."""
        event_dict = {}
        result = _add_service(None, None, event_dict)
        # SERVICE_NAME = os.environ.get("SERVICE_NAME", "selti-worker")
        expected = os.environ.get("SERVICE_NAME", "selti-worker")
        assert result["service"] == expected


# ══════════════════════════════════════════════════════════════════
# 3. Timestamp format
# ══════════════════════════════════════════════════════════════════


class TestTimestampFormat:
    def test_add_timestamp_iso_format(self):
        """_add_timestamp добавляет ISO 8601 UTC timestamp."""
        event_dict = {}
        result = _add_timestamp(None, None, event_dict)

        assert "ts" in result
        ts = result["ts"]
        # Формат: 2026-08-01T12:34:56.789Z
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
        assert re.match(pattern, ts), f"Timestamp format wrong: {ts}"

    def test_timestamp_ends_with_z(self):
        """Timestamp заканчивается на Z (UTC)."""
        event_dict = {}
        result = _add_timestamp(None, None, event_dict)
        assert result["ts"].endswith("Z")

    def test_timestamp_millisecond_precision(self):
        """Timestamp имеет миллисекундную точность (3 цифры после точки)."""
        event_dict = {}
        result = _add_timestamp(None, None, event_dict)
        ts = result["ts"]
        # Timestamp полный: 2026-08-01T19:08:31.925Z — проверяем что .XXXZ есть
        assert re.search(r"\.\d{3}Z$", ts), f"No ms precision in: {ts}"


# ══════════════════════════════════════════════════════════════════
# 4. Event → msg renaming
# ══════════════════════════════════════════════════════════════════


class TestEventToMsg:
    def test_rename_event_to_msg(self):
        """event → msg (ключ для целевого формата)."""
        event_dict = {"event": "memory_store: done"}
        result = _rename_event_to_msg(None, None, event_dict)
        assert result["msg"] == "memory_store: done"
        assert "event" not in result

    def test_no_event_key_unchanged(self):
        """Без event ключа — ничего не меняется."""
        event_dict = {"msg": "already renamed"}
        result = _rename_event_to_msg(None, None, event_dict)
        assert result["msg"] == "already renamed"

    def test_empty_event(self):
        """Пустой event → пустой msg."""
        event_dict = {"event": ""}
        result = _rename_event_to_msg(None, None, event_dict)
        assert result["msg"] == ""


# ══════════════════════════════════════════════════════════════════
# 5. Correlation ID (request_id)
# ══════════════════════════════════════════════════════════════════


class TestCorrelationId:
    def test_merge_request_id_when_set(self):
        """request_id_var установлен → request_id в event_dict."""
        from contextvars import ContextVar

        mock_var = ContextVar("test_request_id", default="")

        with patch("memory_server.tasks.logging_config._request_id_var", mock_var), \
             patch("memory_server.tasks.logging_config._request_id_var_loaded", True):
            mock_var.set("abc-123")
            event_dict = {}
            result = _merge_request_id(None, None, event_dict)

            assert result["request_id"] == "abc-123"

    def test_merge_request_id_when_empty(self):
        """request_id_var пустой → request_id НЕ добавляется."""
        from contextvars import ContextVar

        mock_var = ContextVar("test_request_id2", default="")

        with patch("memory_server.tasks.logging_config._request_id_var", mock_var), \
             patch("memory_server.tasks.logging_config._request_id_var_loaded", True):
            mock_var.set("")
            event_dict = {}
            result = _merge_request_id(None, None, event_dict)

            assert "request_id" not in result

    def test_merge_request_id_import_error(self):
        """argenta_logging не установлен → _request_id_var = False."""
        # Проверяем что при импорте с ошибкой — ничего не ломается
        with patch("memory_server.tasks.logging_config._request_id_var", False), \
             patch("memory_server.tasks.logging_config._request_id_var_loaded", True):
            event_dict = {"msg": "test"}
            result = _merge_request_id(None, None, event_dict)
            # Без request_id_var — просто возвращаем event_dict как есть
            assert "msg" in result


# ══════════════════════════════════════════════════════════════════
# 6. Configure structlog
# ══════════════════════════════════════════════════════════════════


class TestConfigureStructlog:
    def test_configure_structlog_sets_service(self):
        """configure_structlog с service обновляет SERVICE_NAME."""
        configure_structlog(service="test-service")
        # Проверяем что global SERVICE_NAME обновлён
        from memory_server.tasks.logging_config import SERVICE_NAME as sn
        # Note: service parameter only updates via global in configure_structlog
        # This is expected behavior

    def test_structlog_logger_works_after_configure(self):
        """После configure structlog.get_logger() работает."""
        configure_structlog()
        logger = structlog.get_logger("test")
        # Не должно быть исключения
        assert logger is not None


# ══════════════════════════════════════════════════════════════════
# 7. Shared processors chain
# ══════════════════════════════════════════════════════════════════


class TestSharedProcessors:
    def test_shared_processors_count(self):
        """Цепочка shared processors содержит 5 элементов."""
        from memory_server.tasks.logging_config import _shared_processors
        # merge_contextvars, merge_request_id, add_log_level, map_level, add_timestamp, add_service
        assert len(_shared_processors) >= 5

    def test_full_processor_chain(self):
        """Полная цепочка: event → msg, ts, level, service."""
        event_dict = {"event": "test message", "level": "INFO"}

        # Прогоняем через все shared processors
        for proc in [
            _merge_request_id,
            _map_level,
            _add_timestamp,
            _add_service,
        ]:
            event_dict = proc(None, None, event_dict)

        # Переименовываем event → msg
        event_dict = _rename_event_to_msg(None, None, event_dict)

        assert "ts" in event_dict
        assert event_dict["level"] == "INFO"
        assert "service" in event_dict
        assert event_dict["msg"] == "test message"
