"""Тесты для POSIX-форматтера логирования Argenta Team.

Формат: [ISO8601-UTC] [LEVEL] [service-name] message {"key": "value"}
POSIX-совместим: grep, jq, awk работают без проблем.
"""

import json
import logging
import re
import sys

from memory_server.logger import PosixFormatter, SERVICE_NAME


class TestPosixFormatter:
    """Проверка PosixFormatter."""

    def setup_method(self):
        self.formatter = PosixFormatter()
        self.logger = logging.getLogger("test_logger")
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG)

    def _make_record(
        self,
        msg: str,
        level: int = logging.INFO,
        exc_info: tuple | None = None,
        extra: dict | None = None,
    ) -> logging.LogRecord:
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

    # ── Базовый формат ────────────────────────────────────────────

    def test_format_matches_spec(self):
        """Формат: [ISO8601] [LEVEL] [service] message."""
        record = self._make_record("hello world")
        output = self.formatter.format(record)
        pattern = r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] \[INFO\] \[.*\] hello world$"
        assert re.match(pattern, output), f"Format mismatch: {output}"

    def test_timestamp_is_iso_utc(self):
        """Timestamp в формате ISO 8601 UTC с миллисекундами."""
        record = self._make_record("x")
        output = self.formatter.format(record)
        ts_match = re.match(r"^\[([^\]]+)\]", output)
        assert ts_match
        ts = ts_match.group(1)
        assert ts.endswith("Z"), f"Timestamp not UTC: {ts}"
        assert len(ts) == 24, f"Timestamp length wrong: {ts}"  # 2026-07-30T18:00:00.000Z

    def test_level_reflects_severity(self):
        """Уровень логирования соответствует record.levelname."""
        record = self._make_record("debug", level=logging.DEBUG)
        output = self.formatter.format(record)
        assert "[DEBUG]" in output

        record = self._make_record("error", level=logging.ERROR)
        output = self.formatter.format(record)
        assert "[ERROR]" in output

    def test_service_name_in_brackets(self):
        """Имя сервиса в квадратных скобках."""
        record = self._make_record("test")
        output = self.formatter.format(record)
        assert f"[{SERVICE_NAME}]" in output

    def test_message_with_args(self):
        """Сообщение форматируется с аргументами."""
        record = self.logger.makeRecord(
            name=self.logger.name,
            level=logging.INFO,
            fn="test.py",
            lno=10,
            msg="count=%d, name=%s",
            args=(42, "test"),
            exc_info=None,
        )
        output = self.formatter.format(record)
        assert "count=42, name=test" in output

    # ── JSON-мета ─────────────────────────────────────────────────

    def test_json_meta_from_extra(self):
        """Extra-атрибуты попадают в JSON-мету."""
        record = self._make_record("with meta")
        record.duration_ms = 150.5
        record.request_id = "req-abc-123"
        output = self.formatter.format(record)

        # JSON-мета идёт после сообщения
        meta_match = re.search(r"(\{.*\})$", output)
        assert meta_match, f"No JSON meta found: {output}"
        meta = json.loads(meta_match.group(1))
        assert meta["duration_ms"] == 150.5
        assert meta["request_id"] == "req-abc-123"

    def test_no_meta_when_no_extra(self):
        """Без extra-атриIBUTов JSON-мета не добавляется."""
        record = self._make_record("no meta")
        output = self.formatter.format(record)
        assert not re.search(r"\{.*\}$", output), f"Unexpected JSON meta: {output}"

    def test_all_extra_fields_in_meta(self):
        """В JSON-мету попадают все extra-поля (включая кастомные)."""
        record = self._make_record("filtered")
        record.duration_ms = 100
        record.custom_key = "appears"
        output = self.formatter.format(record)

        meta_match = re.search(r"(\{.*\})$", output)
        assert meta_match
        meta = json.loads(meta_match.group(1))
        assert "duration_ms" in meta
        assert meta["custom_key"] == "appears"

    def test_builtin_logrecord_attrs_excluded(self):
        """Встроенные атрибуты LogRecord (name, levelno, etc.) НЕ попадают в JSON."""
        record = self._make_record("clean")
        output = self.formatter.format(record)

        meta_match = re.search(r"(\{.*\})$", output)
        if meta_match:
            meta = json.loads(meta_match.group(1))
            assert "name" not in meta
            assert "levelno" not in meta
            assert "pathname" not in meta

    # ── Исключения ────────────────────────────────────────────────

    def test_exception_included_on_error(self):
        """При исключении stack trace добавляется после лога."""
        try:
            raise ValueError("something went wrong")
        except ValueError:
            record = self._make_record("error", level=logging.ERROR, exc_info=sys.exc_info())

        output = self.formatter.format(record)
        assert "ValueError" in output
        assert "something went wrong" in output
        # Stack trace идёт на следующей строке
        assert "\n" in output

    def test_no_exception_on_normal_log(self):
        """Без исключения stack trace не добавляется."""
        record = self._make_record("info msg")
        output = self.formatter.format(record)
        assert "\n" not in output

    # ── POSIX-совместимость ───────────────────────────────────────

    def test_grep_compatible(self):
        """Логи можно фильтровать через grep."""
        record = self._make_record("test message")
        output = self.formatter.format(record)
        assert "[INFO]" in output
        assert "[test message" in output or "test message" in output

    def test_unicode_supported(self):
        """Поддержка Unicode в сообщениях."""
        record = self._make_record("привет мир")
        output = self.formatter.format(record)
        assert "привет мир" in output

    def test_json_is_valid(self):
        """JSON-мета — валидный JSON."""
        record = self._make_record("test")
        record.duration_ms = 42
        output = self.formatter.format(record)
        meta_match = re.search(r"(\{.*\})$", output)
        assert meta_match
        json.loads(meta_match.group(1))  # Не должно быть исключения
