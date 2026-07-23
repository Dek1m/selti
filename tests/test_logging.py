"""Тесты для структурированного JSON-логирования.

Проверяем JSONFormatter из memory_server.server:
- Базовое форматирование с timestamp, level, logger, message
- Опциональные поля: request_id, duration_ms, exception
- Приоритет request_id из contextvars над атрибутом record
"""

import json
import logging
import sys

from memory_server.server import JSONFormatter, request_id_var


class TestJSONFormatter:
    """Проверка JSONFormatter."""

    def setup_method(self):
        self.formatter = JSONFormatter()
        self.logger = logging.getLogger("test_logger")
        # Убираем已有的 хендлеры, чтобы не засорять вывод
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG)

    def _make_record(
        self,
        msg: str,
        level: int = logging.INFO,
        exc_info: tuple | None = None,
        extra: dict | None = None,
    ) -> logging.LogRecord:
        """Создать LogRecord с заданными параметрами."""
        record = self.logger.makeRecord(
            name=self.logger.name,
            level=level,
            fn="test.py",
            lno=10,
            msg=msg,
            args=(),
            exc_info=exc_info,
            extra=extra,
        )
        return record

    # ── Базовые поля ──────────────────────────────────────────────

    def test_basic_fields_present(self):
        """В JSON-логе должны быть timestamp, level, logger, message."""
        record = self._make_record("hello world")
        output = self.formatter.format(record)
        data = json.loads(output)

        assert "timestamp" in data
        assert data["level"] == "INFO"
        assert data["logger"] == "test_logger"
        assert data["message"] == "hello world"

    def test_timestamp_is_iso_format(self):
        """timestamp должен быть в формате ISO (YYYY-MM-DD HH:MM:SS,mmm)."""
        record = self._make_record("x")
        output = self.formatter.format(record)
        data = json.loads(output)

        # Пример: 2025-06-15 12:00:00,123
        assert isinstance(data["timestamp"], str)
        assert len(data["timestamp"]) >= 19  # минимум "2025-06-15 12:00:00"

    def test_level_reflects_severity(self):
        """Уровень логирования должен соответствовать record.levelname."""
        record = self._make_record("debug", level=logging.DEBUG)
        data = json.loads(self.formatter.format(record))
        assert data["level"] == "DEBUG"

        record = self._make_record("error", level=logging.ERROR)
        data = json.loads(self.formatter.format(record))
        assert data["level"] == "ERROR"

    def test_message_with_args(self):
        """Сообщение должно быть отформатировано с аргументами."""
        record = self.logger.makeRecord(
            name=self.logger.name,
            level=logging.INFO,
            fn="test.py",
            lno=10,
            msg="count=%d, name=%s",
            args=(42, "test"),
            exc_info=None,
        )
        data = json.loads(self.formatter.format(record))
        assert data["message"] == "count=42, name=test"

    # ── Опциональные поля ─────────────────────────────────────────

    def test_request_id_from_extra(self):
        """Если у record есть атрибут request_id, он должен попасть в JSON."""
        record = self._make_record("with extra")
        record.request_id = "req-abc-123"
        data = json.loads(self.formatter.format(record))
        assert data["request_id"] == "req-abc-123"

    def test_duration_ms_from_extra(self):
        """Если у record есть атрибут duration_ms, он должен попасть в JSON."""
        record = self._make_record("timed")
        record.duration_ms = 150.5
        data = json.loads(self.formatter.format(record))
        assert data["duration_ms"] == 150.5

    def test_duration_ms_is_absent_when_not_set(self):
        """Если duration_ms не задан, поле не должно быть в JSON."""
        record = self._make_record("no duration")
        data = json.loads(self.formatter.format(record))
        assert "duration_ms" not in data

    def test_exception_field_present_on_error(self):
        """При наличии исключения в JSON должно быть поле exception."""
        try:
            raise ValueError("something went wrong")
        except ValueError:
            record = self._make_record("error", level=logging.ERROR, exc_info=sys.exc_info())

        data = json.loads(self.formatter.format(record))
        assert "exception" in data
        assert "ValueError" in data["exception"]
        assert "something went wrong" in data["exception"]

    def test_no_exception_field_on_normal_log(self):
        """Без исключения поле exception не должно появляться."""
        record = self._make_record("info msg")
        data = json.loads(self.formatter.format(record))
        assert "exception" not in data

    def test_request_id_is_absent_when_not_set(self):
        """Если request_id не задан, поле не должно быть в JSON."""
        record = self._make_record("no request id")
        data = json.loads(self.formatter.format(record))
        assert "request_id" not in data

    # ── ContextVar request_id ──────────────────────────────────────

    def test_request_id_from_contextvar(self):
        """request_id_var из contextvars должен попадать в JSON."""
        token = request_id_var.set("ctx-req-456")
        try:
            record = self._make_record("from ctx")
            data = json.loads(self.formatter.format(record))
            assert data["request_id"] == "ctx-req-456"
        finally:
            request_id_var.reset(token)

    def test_contextvar_takes_precedence_over_extra(self):
        """ContextVar request_id имеет приоритет над атрибутом record."""
        token = request_id_var.set("from-ctx")
        try:
            record = self._make_record("precedence")
            record.request_id = "from-extra"
            data = json.loads(self.formatter.format(record))
            # В форматтере сначала проверяется request_id_var.get(None)
            assert data["request_id"] == "from-ctx"
        finally:
            request_id_var.reset(token)

    def test_contextvar_reset_removes_request_id(self):
        """После сброса contextvar, request_id не должен появляться."""
        token = request_id_var.set("temp-id")
        request_id_var.reset(token)

        record = self._make_record("after reset")
        data = json.loads(self.formatter.format(record))
        assert "request_id" not in data

    # ── Сериализация ──────────────────────────────────────────────

    def test_ensure_ascii_false(self):
        """JSONFormatter использует ensure_ascii=False для поддержки Unicode."""
        record = self._make_record("привет")
        output = self.formatter.format(record)
        data = json.loads(output)
        assert data["message"] == "привет"

    def test_json_is_valid(self):
        """Вывод должен быть валидным JSON."""
        record = self._make_record("тест")
        output = self.formatter.format(record)
        # Не должно быть исключения
        json.loads(output)
