"""Tests for memory_server.tools.task_bridge.

Coverage: run_task(), celery_call(), error handling, timeout.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from memory_server.tools.task_bridge import TASK_RESULT_TIMEOUT, celery_call, run_task


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def mock_celery_app():
    """Fake Celery app with send_task mock."""
    app = MagicMock()
    return app


@pytest.fixture
def mock_async_result():
    """Fake Celery AsyncResult."""
    result = MagicMock()
    result.id = "test-task-id-123"
    result.get.return_value = {"status": "ok", "data": [1, 2, 3]}
    return result


# ── run_task ─────────────────────────────────────────────────────


class TestRunTask:
    def test_success(self, mock_celery_app, mock_async_result):
        """Happy path: send_task → result.get → return value."""
        mock_celery_app.send_task.return_value = mock_async_result

        result = run_task(
            mock_celery_app,
            "memory_server.tasks.memory_tasks.store_memory",
            content="hello",
            user_id="u1",
        )

        assert result == {"status": "ok", "data": [1, 2, 3]}
        mock_celery_app.send_task.assert_called_once_with(
            "memory_server.tasks.memory_tasks.store_memory",
            kwargs={"content": "hello", "user_id": "u1"},
        )
        mock_async_result.get.assert_called_once_with(timeout=TASK_RESULT_TIMEOUT)

    def test_custom_timeout(self, mock_celery_app, mock_async_result):
        """Custom timeout passed to result.get()."""
        mock_celery_app.send_task.return_value = mock_async_result

        run_task(mock_celery_app, "some.task", timeout=10, x=1)

        mock_async_result.get.assert_called_once_with(timeout=10)

    def test_task_failure_propagates(self, mock_celery_app):
        """Exception from Celery task propagates to caller."""
        mock_async_result = MagicMock()
        mock_async_result.id = "fail-id"
        mock_async_result.get.side_effect = RuntimeError("Task failed")
        mock_celery_app.send_task.return_value = mock_async_result

        with pytest.raises(RuntimeError, match="Task failed"):
            run_task(mock_celery_app, "failing.task")

    def test_connection_error_propagates(self, mock_celery_app):
        """ConnectionError from broker propagates."""
        mock_async_result = MagicMock()
        mock_async_result.id = "conn-id"
        mock_async_result.get.side_effect = ConnectionError("Broker unreachable")
        mock_celery_app.send_task.return_value = mock_async_result

        with pytest.raises(ConnectionError):
            run_task(mock_celery_app, "some.task")

    def test_timeout_propagates(self, mock_celery_app):
        """TimeoutError from result.get propagates."""
        mock_async_result = MagicMock()
        mock_async_result.id = "timeout-id"
        mock_async_result.get.side_effect = asyncio.TimeoutError()
        mock_celery_app.send_task.return_value = mock_async_result

        with pytest.raises(asyncio.TimeoutError):
            run_task(mock_celery_app, "slow.task", timeout=0.001)


# ── celery_call ─────────────────────────────────────────────────


class TestCeleryCall:
    @pytest.mark.asyncio
    async def test_calls_run_task_in_thread(self):
        """celery_call delegates to run_task via asyncio.to_thread."""
        with patch(
            "memory_server.tools.task_bridge.run_task", return_value={"ok": True}
        ) as mock_run, patch(
            "memory_server.tools.task_bridge.app", create=True
        ) as mock_app:
            # celery_call imports app internally, we mock at module level
            import memory_server.tools.task_bridge as bridge

            # Temporarily replace the app used by celery_call
            with patch.object(bridge, "app", mock_app):
                result = await celery_call(
                    "memory_server.tasks.memory_tasks.store_memory",
                    content="test",
                    user_id="u1",
                )

                assert result == {"ok": True}
                mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_kwargs(self):
        """celery_call forwards all kwargs to run_task."""
        with patch(
            "memory_server.tools.task_bridge.run_task", return_value=[]
        ) as mock_run, patch(
            "memory_server.tools.task_bridge.app", create=True
        ) as mock_app:
            import memory_server.tools.task_bridge as bridge

            with patch.object(bridge, "app", mock_app):
                await celery_call(
                    "some.task",
                    param1="a",
                    param2=42,
                )

                call_kwargs = mock_run.call_args
                assert call_kwargs[1]["param1"] == "a"
                assert call_kwargs[1]["param2"] == 42

    @pytest.mark.asyncio
    async def test_propagates_exception(self):
        """Exception from run_task propagates through celery_call."""
        with patch(
            "memory_server.tools.task_bridge.run_task",
            side_effect=ValueError("bad input"),
        ), patch(
            "memory_server.tools.task_bridge.app", create=True
        ) as mock_app:
            import memory_server.tools.task_bridge as bridge

            with patch.object(bridge, "app", mock_app):
                with pytest.raises(ValueError, match="bad input"):
                    await celery_call("some.task")
