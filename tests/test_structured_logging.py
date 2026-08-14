"""Тесты для structured logging (Argenta Team standard).

Проверяем:
  - JSON формат логов (timestamp, level, service, message)
  - Level mapping: WARNING → WARN, CRITICAL → ERROR
  - Correlation ID injection через contextvars
  - Service name injection
  - Timestamp format (ISO 8601 UTC)
  - setup_logging / setup_worker_logging / setup_server_logging
"""

import json
import logging
import os
import re
from unittest.mock import patch

import pytest


# ══════════════════════════════════════════════════════════════════
# 1. JsonFormatter output format
# ══════════════════════════════════════════════════════════════════


class TestJsonFormatter:
    def test_json_format_has_required_fields(self):
        """JsonFormatter produces timestamp, level, service, message."""
        from argenta_logging import JsonFormatter

        formatter = JsonFormatter(service="test-service")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test_message", args=(), exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)

        assert "timestamp" in data
        assert data["level"] == "INFO"
        assert data["service"] == "test-service"
        assert data["message"] == "test_message"

    def test_json_timestamp_iso_format(self):
        """Timestamp is ISO 8601 UTC with millisecond precision."""
        from argenta_logging import JsonFormatter

        formatter = JsonFormatter(service="test")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        data = json.loads(formatter.format(record))
        ts = data["timestamp"]
        # 2026-08-14T12:00:00.000Z
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", ts)

    def test_json_level_mapping(self):
        """WARNING → WARN, CRITICAL → ERROR."""
        from argenta_logging import JsonFormatter

        formatter = JsonFormatter(service="test")
        for input_level, expected in [
            (logging.WARNING, "WARN"),
            (logging.CRITICAL, "ERROR"),
            (logging.INFO, "INFO"),
            (logging.DEBUG, "DEBUG"),
            (logging.ERROR, "ERROR"),
        ]:
            record = logging.LogRecord(
                name="test", level=input_level, pathname="", lineno=0,
                msg="msg", args=(), exc_info=None,
            )
            data = json.loads(formatter.format(record))
            assert data["level"] == expected, f"Level {input_level} should map to {expected}"

    def test_json_extra_fields(self):
        """Extra fields are included in JSON."""
        from argenta_logging import JsonFormatter

        formatter = JsonFormatter(service="test")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record.duration_ms = 42.3
        record.namespace = "code_knowledge"
        data = json.loads(formatter.format(record))

        assert data["duration_ms"] == 42.3
        assert data["namespace"] == "code_knowledge"


# ══════════════════════════════════════════════════════════════════
# 2. PosixFormatter output format
# ══════════════════════════════════════════════════════════════════


class TestPosixFormatter:
    def test_posix_format(self):
        """PosixFormatter: [ISO8601] [LEVEL] [service] message {json}."""
        from argenta_logging import PosixFormatter

        formatter = PosixFormatter(service="test-svc")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="batch_processed", args=(), exc_info=None,
        )
        record.batch_size = 100
        output = formatter.format(record)

        assert "[INFO]" in output
        assert "[test-svc]" in output
        assert "batch_processed" in output
        assert '"batch_size": 100' in output

    def test_posix_timestamp_format(self):
        """POSIX timestamp is ISO 8601 UTC."""
        from argenta_logging import PosixFormatter

        formatter = PosixFormatter(service="test")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        output = formatter.format(record)
        # [2026-08-14T12:00:00.000Z]
        assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\]", output)


# ══════════════════════════════════════════════════════════════════
# 3. Correlation ID (request_id)
# ══════════════════════════════════════════════════════════════════


class TestCorrelationId:
    def test_request_id_var_exists(self):
        """request_id_var is importable from argenta_logging."""
        from argenta_logging import request_id_var
        assert request_id_var is not None

    def test_request_id_in_json_log(self):
        """request_id appears in JSON log when set."""
        from argenta_logging import JsonFormatter, request_id_var

        token = request_id_var.set("test-req-123")
        try:
            formatter = JsonFormatter(service="test")
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="msg", args=(), exc_info=None,
            )
            # RequestContextFilter adds request_id to record
            from argenta_logging import RequestContextFilter
            f = RequestContextFilter()
            f.filter(record)

            data = json.loads(formatter.format(record))
            assert data.get("request_id") == "test-req-123"
        finally:
            request_id_var.reset(token)


# ══════════════════════════════════════════════════════════════════
# 4. setup_logging
# ══════════════════════════════════════════════════════════════════


class TestSetupLogging:
    def test_setup_logging_sets_handler(self):
        """setup_logging adds a handler to root logger."""
        from argenta_logging import setup_logging

        root = logging.getLogger()
        old_handlers = root.handlers[:]
        try:
            setup_logging(service="test-setup", level="DEBUG", fmt="json")
            assert len(root.handlers) >= 1
        finally:
            root.handlers = old_handlers

    def test_setup_worker_logging_silences_celery(self):
        """setup_worker_logging silences celery loggers."""
        from memory_server.tasks.logging_config import setup_worker_logging

        root = logging.getLogger()
        old_handlers = root.handlers[:]
        old_level = root.level
        try:
            setup_worker_logging(level="INFO")
            assert logging.getLogger("celery").level >= logging.WARNING
            assert logging.getLogger("kombu").level >= logging.WARNING
        finally:
            root.handlers = old_handlers
            root.level = old_level
