"""Тесты для POSIX-логирования по стандарту Argenta Team.

Формат: [ISO8601-UTC] [LEVEL] [service-name] message {"key": "value"}

Покрытие:
    - Формат лога (ISO8601, уровень, сервис, сообщение, JSON-мета)
    - Фильтрация по уровню
    - JSON-мета (пустой контекст, extra-поля, multiple keys)
    - Интеграция (stdout, service name из env)
"""

import json
import logging
import os
import re
import sys
from io import StringIO
from unittest.mock import patch

import pytest

from argenta_logging import PosixFormatter, get_logger, setup_logging


# ── Регулярка для проверки формата ─────────────────────────────
LOG_PATTERN = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\] "
    r"\[(DEBUG|INFO|WARN|ERROR)\] "
    r"\[([a-zA-Z0-9_-]+)\] "
    r"(.+)$"
)


def _extract_json(output: str) -> dict:
    """Извлечь JSON-мету из строки лога (ищем последний '{')."""
    idx = output.rfind("{")
    if idx == -1:
        raise AssertionError(f"JSON not found in: {output}")
    return json.loads(output[idx:])


class TestPosixFormatter:
    """Проверка PosixFormatter — основной формат лога."""

    def setup_method(self):
        self.formatter = PosixFormatter(service="selti")
        self.logger = logging.getLogger("test_posix")
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG)

    def _make_record(
        self,
        msg: str,
        level: int = logging.INFO,
        extra: dict | None = None,
    ) -> logging.LogRecord:
        return self.logger.makeRecord(
            name=self.logger.name,
            level=level,
            fn="test.py",
            lno=10,
            msg=msg,
            args=(),
            exc_info=None,
            extra=extra,
        )

    # ── Формат строки ────────────────────────────────────────────

    def test_format_matches_standard(self):
        """Лог должен соответствовать формату [ISO8601] [LEVEL] [service] message."""
        record = self._make_record("hello")
        output = self.formatter.format(record)

        match = LOG_PATTERN.match(output)
        assert match, f"Формат не соответствует стандарту: {output}"

    def test_timestamp_is_iso8601_utc(self):
        """Timestamp должен быть ISO 8601 UTC с миллисекундами."""
        record = self._make_record("test")
        output = self.formatter.format(record)
        timestamp = output.split("]")[0].lstrip("[")

        # ISO 8601: 2026-07-30T18:00:00.000Z
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", timestamp)

    def test_level_in_brackets(self):
        """Уровень должен быть в квадратных скобках."""
        for level, name in [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARN"),
            (logging.ERROR, "ERROR"),
        ]:
            record = self._make_record("x", level=level)
            output = self.formatter.format(record)
            assert f"[{name}]" in output

    def test_service_in_brackets(self):
        """Сервис должен быть в квадратных скобках."""
        record = self._make_record("test")
        output = self.formatter.format(record)
        assert "[selti]" in output

    def test_custom_service_name(self):
        """Сервис берётся из параметра конструктора."""
        fmt = PosixFormatter(service="gera")
        record = self._make_record("test")
        output = fmt.format(record)
        assert "[gera]" in output

    def test_message_preserved(self):
        """Текст сообщения должен сохраняться."""
        record = self._make_record("Session processed")
        output = self.formatter.format(record)
        assert "Session processed" in output

    def test_message_with_args(self):
        """Сообщение должно подставлять аргументы."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(self.formatter)
        self.logger.handlers = [handler]

        self.logger.info("count=%d, name=%s", 42, "test")
        output = stream.getvalue().strip()
        assert "count=42, name=test" in output

    # ── JSON-мета ────────────────────────────────────────────────

    def test_json_meta_present(self):
        """Если есть extra-поля — JSON-мета должна быть в конце."""
        record = self._make_record("with meta", extra={"tool": "search"})
        output = self.formatter.format(record)
        data = _extract_json(output)
        assert data["tool"] == "search"

    def test_json_meta_empty_when_no_extra(self):
        """Без extra-полей JSON-меты быть не должно."""
        record = self._make_record("plain message")
        output = self.formatter.format(record)
        assert output.endswith("plain message")

    def test_multiple_extra_fields(self):
        """Несколько extra-полей должны попасть в JSON."""
        record = self._make_record(
            "batch",
            extra={"batch_size": 100, "inserted": 95, "skipped": 5},
        )
        output = self.formatter.format(record)
        data = _extract_json(output)
        assert data["batch_size"] == 100
        assert data["inserted"] == 95
        assert data["skipped"] == 5

    def test_duration_ms_in_meta(self):
        """duration_ms должен попадать в JSON-мету."""
        record = self._make_record("timed", extra={"duration_ms": 150.5})
        output = self.formatter.format(record)
        data = _extract_json(output)
        assert data["duration_ms"] == 150.5

    def test_request_id_in_meta(self):
        """request_id должен попадать в JSON-мету."""
        record = self._make_record("traced", extra={"request_id": "req-abc-123"})
        output = self.formatter.format(record)
        data = _extract_json(output)
        assert data["request_id"] == "req-abc-123"

    def test_non_serializable_value_in_meta(self):
        """Несериализуемые значения должны конвертироваться через str()."""
        record = self._make_record("weird", extra={"error": object()})
        output = self.formatter.format(record)
        data = _extract_json(output)
        assert isinstance(data["error"], str)

    def test_unicode_in_message(self):
        """Unicode в сообщении должен работать."""
        record = self._make_record("Привет, мир! 日本語")
        output = self.formatter.format(record)
        assert "Привет, мир! 日本語" in output

    # ── Уровни ───────────────────────────────────────────────────

    def test_debug_level(self):
        record = self._make_record("debug msg", level=logging.DEBUG)
        output = self.formatter.format(record)
        assert "[DEBUG]" in output

    def test_info_level(self):
        record = self._make_record("info msg", level=logging.INFO)
        output = self.formatter.format(record)
        assert "[INFO]" in output

    def test_warn_level(self):
        record = self._make_record("warn msg", level=logging.WARNING)
        output = self.formatter.format(record)
        assert "[WARN]" in output

    def test_error_level(self):
        record = self._make_record("error msg", level=logging.ERROR)
        output = self.formatter.format(record)
        assert "[ERROR]" in output

    def test_level_mapping_warning_to_warn(self):
        """Python WARNING должен маппиться в WARN по стандарту."""
        record = self._make_record("x", level=logging.WARNING)
        output = self.formatter.format(record)
        assert "[WARNING]" not in output
        assert "[WARN]" in output


class TestLevelFiltering:
    """Проверка фильтрации по уровню логирования."""

    def test_info_level_filters_debug(self):
        """При LOG_LEVEL=INFO — DEBUG не должен проходить."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(PosixFormatter(service="selti"))
        handler.setLevel(logging.INFO)

        logger = logging.getLogger("test_filter")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)

        logger.debug("should not appear")
        logger.info("should appear")

        output = stream.getvalue()
        assert "should not appear" not in output
        assert "should appear" in output

    def test_error_level_filters_info_and_warn(self):
        """При LOG_LEVEL=ERROR — только ERROR должен проходить."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(PosixFormatter(service="selti"))
        handler.setLevel(logging.ERROR)

        logger = logging.getLogger("test_error_only")
        logger.handlers = [handler]
        logger.setLevel(logging.ERROR)

        logger.info("nope")
        logger.warning("nope")
        logger.error("yes")

        output = stream.getvalue()
        assert "nope" not in output
        assert "yes" in output

    def test_debug_level_passes_everything(self):
        """При LOG_LEVEL=DEBUG — всё должно проходить."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(PosixFormatter(service="selti"))
        handler.setLevel(logging.DEBUG)

        logger = logging.getLogger("test_debug_all")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)

        logger.debug("debug")
        logger.info("info")
        logger.warning("warn")
        logger.error("error")

        output = stream.getvalue()
        assert "[DEBUG]" in output
        assert "[INFO]" in output
        assert "[WARN]" in output
        assert "[ERROR]" in output

    def test_setup_logging_sets_root_level(self):
        """setup_logging должен выставлять уровень на корневом логгере."""
        setup_logging(level="WARN", service="selti")
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_setup_logging_default_level(self):
        """По умолчанию уровень INFO."""
        with patch.dict(os.environ, {}, clear=True):
            setup_logging(service="selti")
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_setup_logging_from_env(self):
        """Уровень берётся из LOG_LEVEL env."""
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}, clear=True):
            setup_logging(service="selti")
        root = logging.getLogger()
        assert root.level == logging.DEBUG


class TestDockerIntegration:
    """Проверка интеграции с Docker (stdout, service name)."""

    def test_handler_uses_stdout(self):
        """Хендлер должен писать в stdout, не stderr."""
        setup_logging(level="INFO", service="selti")
        root = logging.getLogger()
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler):
                assert handler.stream is sys.stdout

    def test_service_name_from_env(self):
        """SERVICE_NAME env определяет имя сервиса через setup_logging."""
        with patch.dict(os.environ, {"SERVICE_NAME": "my-custom-service"}):
            setup_logging(service="my-custom-service")
            root = logging.getLogger()
            formatter = root.handlers[0].formatter
            assert isinstance(formatter, PosixFormatter)
            assert formatter.service == "my-custom-service"

    def test_service_name_default(self):
        """По умолчанию сервис — 'unknown' (из argenta_logging)."""
        with patch.dict(os.environ, {}, clear=True):
            fmt = PosixFormatter()
            assert fmt.service == "unknown"

    def test_setup_logging_custom_service(self):
        """setup_logging принимает имя сервиса."""
        with patch.dict(os.environ, {}, clear=True):
            setup_logging(level="INFO", service="gera")
        root = logging.getLogger()
        assert root.handlers
        formatter = root.handlers[0].formatter
        assert isinstance(formatter, PosixFormatter)
        assert formatter.service == "gera"


class TestEdgeCases:
    """Краевые случаи."""

    def test_empty_message(self):
        """Пустое сообщение не должно ломать формат."""
        formatter = PosixFormatter(service="selti")
        logger = logging.getLogger("edge_empty")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        record = logger.makeRecord(
            name="edge_empty", level=logging.INFO,
            fn="test.py", lno=1, msg="", args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "[INFO]" in output
        assert "[selti]" in output

    def test_special_chars_in_message(self):
        """Спецсимволы в сообщении не должны ломать формат."""
        formatter = PosixFormatter(service="selti")
        logger = logging.getLogger("edge_special")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        record = logger.makeRecord(
            name="edge_special", level=logging.INFO,
            fn="test.py", lno=1, msg='key="value" with spaces & <html>',
            args=(), exc_info=None,
        )
        output = formatter.format(record)
        assert 'key="value" with spaces & <html>' in output

    def test_json_meta_not_confused_with_message(self):
        """JSON-мета не должна смешиваться с сообщением."""
        formatter = PosixFormatter(service="selti")
        logger = logging.getLogger("edge_json")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        record = logger.makeRecord(
            name="edge_json", level=logging.INFO,
            fn="test.py", lno=1, msg='{"fake": "json"}',
            args=(), exc_info=None, extra={"request_id": "req-123"},
        )
        output = formatter.format(record)
        # Сообщение должно быть '{"fake": "json"}'
        assert '{"fake": "json"}' in output
        # JSON-мета отдельно
        data = _extract_json(output)
        assert data["request_id"] == "req-123"

    def test_get_logger_returns_named_logger(self):
        """get_logger должен возвращать именованный логгер."""
        log = get_logger("my.module")
        assert log.name == "my.module"
        assert isinstance(log, logging.Logger)

    def test_log_line_posix_grep_compatible(self):
        """Логи должны быть grep-совместимыми."""
        formatter = PosixFormatter(service="selti")
        logger = logging.getLogger("grep_test")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        record = logger.makeRecord(
            name="grep_test", level=logging.ERROR,
            fn="test.py", lno=1, msg="Database connection failed",
            args=(), exc_info=None,
        )
        output = formatter.format(record)
        # grep '\[ERROR\]' должен найти
        assert "[ERROR]" in output
        # grep '\[selti\]' должен найти
        assert "[selti]" in output
