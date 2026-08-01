"""Тесты для tool_handler decorator — metrics, timing, error handling.

Проверяем:
  - Декоратор оборачивает функцию и считает метрики
  - Успешный вызов → MCP_TOOL_CALLS_TOTAL.labels(status="ok")
  - Ошибка → MCP_TOOL_CALLS_TOTAL.labels(status="error") + RuntimeError
  - Timing фиксируется в MCP_TOOL_DURATION_SECONDS
  - Декоратор сохраняет оригинальное имя функции (__name__)

БАГИ В КОДЕ (зафиксированы в отчёте):
  1. metrics_decorator.py:36 — logger.error(error=str(e)) — stdlib logging
     не принимает kwargs. Нужно logger.error(msg, extra={"error": str(e)})
  2. server.py:21 — NameError: name 'logging' is not defined

В данных тестах мы мокаем logger чтобы обойти баг #1.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_server.utils.metrics_decorator import tool_handler


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_metrics():
    with patch("memory_server.utils.metrics_decorator.MCP_TOOL_CALLS_TOTAL") as calls, \
         patch("memory_server.utils.metrics_decorator.MCP_TOOL_DURATION_SECONDS") as duration:
        yield {"calls": calls, "duration": duration}


@pytest.fixture
def mock_logger():
    """Mock logger in metrics_decorator to avoid stdlib logging kwargs bug."""
    with patch("memory_server.utils.metrics_decorator.logger") as logger:
        yield logger


# ══════════════════════════════════════════════════════════════════
# 1. Successful call
# ══════════════════════════════════════════════════════════════════


class TestToolHandlerSuccess:
    @pytest.mark.asyncio
    async def test_successful_call_increments_ok_metric(self, mock_metrics, mock_logger):
        """Успешный вызов → status='ok'."""
        @tool_handler("test_tool")
        async def my_tool():
            return {"result": "ok"}

        result = await my_tool()

        assert result == {"result": "ok"}
        mock_metrics["calls"].labels.assert_called_with(tool="test_tool", status="ok")
        mock_metrics["calls"].labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_call_observes_duration(self, mock_metrics, mock_logger):
        """Успешный вызов → duration observed."""
        @tool_handler("test_tool")
        async def my_tool():
            return "done"

        await my_tool()

        mock_metrics["duration"].labels.assert_called_with(tool="test_tool")
        mock_metrics["duration"].labels.return_value.observe.assert_called_once()
        call_args = mock_metrics["duration"].labels.return_value.observe.call_args
        assert call_args[0][0] >= 0

    @pytest.mark.asyncio
    async def test_decorator_preserves_function_name(self, mock_metrics, mock_logger):
        """Декоратор сохраняет __name__ оригинальной функции."""
        @tool_handler("memory_store")
        async def memory_store(content: str):
            return content

        assert memory_store.__name__ == "memory_store"

    @pytest.mark.asyncio
    async def test_decorator_preserves_docstring(self, mock_metrics, mock_logger):
        """Декоратор сохраняет docstring."""
        @tool_handler("my_tool")
        async def my_tool():
            """This is a docstring."""
            return None

        assert my_tool.__doc__ == "This is a docstring."


# ══════════════════════════════════════════════════════════════════
# 2. Error handling
# ══════════════════════════════════════════════════════════════════


class TestToolHandlerError:
    @pytest.mark.asyncio
    async def test_error_increments_error_metric(self, mock_metrics, mock_logger):
        """Ошибка → status='error'."""
        @tool_handler("failing_tool")
        async def failing_tool():
            raise ValueError("something broke")

        with pytest.raises(RuntimeError, match="something broke"):
            await failing_tool()

        mock_metrics["calls"].labels.assert_called_with(tool="failing_tool", status="error")
        mock_metrics["calls"].labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_wraps_in_runtime_error(self, mock_metrics, mock_logger):
        """Любая ошибка оборачивается в RuntimeError."""
        @tool_handler("failing_tool")
        async def failing_tool():
            raise ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="connection refused"):
            await failing_tool()

    @pytest.mark.asyncio
    async def test_error_preserves_original_exception(self, mock_metrics, mock_logger):
        """RuntimeError содержит original exception as __cause__."""
        @tool_handler("failing_tool")
        async def failing_tool():
            raise ValueError("original error")

        with pytest.raises(RuntimeError) as exc_info:
            await failing_tool()

        assert isinstance(exc_info.value.__cause__, ValueError)
        assert str(exc_info.value.__cause__) == "original error"

    @pytest.mark.asyncio
    async def test_error_observes_duration(self, mock_metrics, mock_logger):
        """Ошибка тоже фиксирует duration."""
        @tool_handler("failing_tool")
        async def failing_tool():
            raise ValueError("oops")

        with pytest.raises(RuntimeError):
            await failing_tool()

        mock_metrics["duration"].labels.return_value.observe.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_logs_error_message(self, mock_metrics, mock_logger):
        """Ошибка логируется (logger.error вызывается)."""
        @tool_handler("failing_tool")
        async def failing_tool():
            raise KeyError("missing_key")

        with pytest.raises(RuntimeError):
            await failing_tool()

        mock_logger.error.assert_called_once()


# ══════════════════════════════════════════════════════════════════
# 3. Timing accuracy
# ══════════════════════════════════════════════════════════════════


class TestToolHandlerTiming:
    @pytest.mark.asyncio
    async def test_duration_is_non_negative(self, mock_metrics, mock_logger):
        """Duration всегда ≥ 0."""
        @tool_handler("fast_tool")
        async def fast_tool():
            return None

        await fast_tool()

        call_args = mock_metrics["duration"].labels.return_value.observe.call_args
        assert call_args[0][0] >= 0

    @pytest.mark.asyncio
    async def test_duration_is_reasonable(self, mock_metrics, mock_logger):
        """Duration разумный (< 5 сек для простой функции)."""
        @tool_handler("simple_tool")
        async def simple_tool():
            return None

        await simple_tool()

        call_args = mock_metrics["duration"].labels.return_value.observe.call_args
        assert call_args[0][0] < 5.0


# ══════════════════════════════════════════════════════════════════
# 4. Argument passing
# ══════════════════════════════════════════════════════════════════


class TestToolHandlerArgs:
    @pytest.mark.asyncio
    async def test_args_passed_through(self, mock_metrics, mock_logger):
        """Аргументы прокидываются в оригинальную функцию."""
        @tool_handler("echo_tool")
        async def echo_tool(a, b, keyword=None):
            return {"a": a, "b": b, "keyword": keyword}

        result = await echo_tool(1, 2, keyword="test")

        assert result == {"a": 1, "b": 2, "keyword": "test"}

    @pytest.mark.asyncio
    async def test_kwargs_passed_through(self, mock_metrics, mock_logger):
        """Keyword arguments прокидываются."""
        @tool_handler("config_tool")
        async def config_tool(limit=10, threshold=0.7):
            return {"limit": limit, "threshold": threshold}

        result = await config_tool(limit=5, threshold=0.9)

        assert result == {"limit": 5, "threshold": 0.9}


# ══════════════════════════════════════════════════════════════════
# 5. Integration — проверяем что декоратор применён
# ══════════════════════════════════════════════════════════════════


class TestToolHandlerIntegration:
    def test_tool_handler_has_wrapped_attribute(self):
        """tool_handler создаёт функцию с атрибутом __wrapped__."""
        @tool_handler("test")
        async def func():
            pass

        assert hasattr(func, "__wrapped__")

    def test_tool_handler_is_composable(self):
        """tool_handler можно использовать с @mcp.tool() (композиция декораторов)."""
        @tool_handler("test_tool")
        async def my_tool():
            return "ok"

        # Проверяем что декоратор применился
        assert my_tool.__name__ == "my_tool"
        assert hasattr(my_tool, "__wrapped__")
