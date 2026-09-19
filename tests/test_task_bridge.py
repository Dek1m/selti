"""Tests for memory_server.tools.task_bridge.

Coverage: run_task(), celery_call(), error handling, timeout,
event-driven ожидание (BLPOP на ключ события завершения).
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

import redis as redis_sync

from memory_server.tools.task_bridge import (
    NOTIFY_KEY_PREFIX,
    TASK_RESULT_TIMEOUT,
    celery_call,
    run_task,
)


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def mock_celery_app():
    """Fake Celery app with send_task mock."""
    app = MagicMock()
    return app


@pytest.fixture
def mock_async_result():
    """Fake Celery AsyncResult (polling-based: ready/failed/result)."""
    result = MagicMock()
    result.id = "test-task-id-123"
    result.ready.return_value = True
    result.failed.return_value = False
    result.result = {"status": "ok", "data": [1, 2, 3]}
    return result


# ── run_task ─────────────────────────────────────────────────────


class TestRunTask:
    def test_success(self, mock_celery_app, mock_async_result):
        """Happy path: send_task → ready → return value."""
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
            headers={"bridge_wait": True},
        )

    def test_custom_timeout(self, mock_celery_app, mock_async_result):
        """Custom timeout is respected (task finishes fast, no timeout)."""
        mock_celery_app.send_task.return_value = mock_async_result

        result = run_task(mock_celery_app, "some.task", timeout=10, x=1)
        assert result == {"status": "ok", "data": [1, 2, 3]}

    def test_task_failure_propagates(self, mock_celery_app):
        """Exception from Celery task propagates to caller."""
        mock_async_result = MagicMock()
        mock_async_result.id = "fail-id"
        mock_async_result.ready.return_value = True
        mock_async_result.failed.return_value = True
        mock_async_result.result = RuntimeError("Task failed")
        mock_celery_app.send_task.return_value = mock_async_result

        with pytest.raises(RuntimeError, match="Task failed"):
            run_task(mock_celery_app, "failing.task")

    def test_connection_error_propagates(self, mock_celery_app):
        """send_task raising an exception propagates."""
        mock_celery_app.send_task.side_effect = ConnectionError("Broker unreachable")

        with pytest.raises(ConnectionError):
            run_task(mock_celery_app, "some.task")

    def test_timeout_propagates(self, mock_celery_app):
        """TimeoutError when task doesn't finish in time."""
        mock_async_result = MagicMock()
        mock_async_result.id = "timeout-id"
        mock_async_result.ready.return_value = False  # never ready
        mock_celery_app.send_task.return_value = mock_async_result

        with pytest.raises(TimeoutError, match="timed out"):
            run_task(mock_celery_app, "slow.task", timeout=0.001)

    def test_wakes_on_bridge_notify_event(self, mock_celery_app):
        """Event-driven: BLPOP-событие завершения будит ожидание (Фаза 3.3)."""
        mock_async_result = MagicMock()
        mock_async_result.id = "notify-id"
        mock_async_result.ready.side_effect = [False, True, True]  # не готов → готов → готов
        mock_async_result.failed.return_value = False
        mock_async_result.result = {"ok": True}
        mock_celery_app.send_task.return_value = mock_async_result

        with patch(
            "memory_server.tools.task_bridge._blpop_notify", return_value=True
        ) as notify:
            result = run_task(mock_celery_app, "notified.task", timeout=10)

        assert result == {"ok": True}
        notify.assert_called_once()
        assert notify.call_args[0][0] == "notify-id"
        # Чанк ожидания: не длиннее остатка таймаута и не длиннее 30с
        assert 0 < notify.call_args[0][1] <= 10.0

    def test_lost_notify_falls_back_to_readiness_check(self, mock_celery_app):
        """Потерянное событие: контроль ready() между чанками подхватывает результат."""
        mock_async_result = MagicMock()
        mock_async_result.id = "lost-notify-id"
        mock_async_result.ready.side_effect = [False, True, True]
        mock_async_result.failed.return_value = False
        mock_async_result.result = {"ok": True}
        mock_celery_app.send_task.return_value = mock_async_result

        with patch(
            "memory_server.tools.task_bridge._blpop_notify", return_value=False
        ) as notify:
            result = run_task(mock_celery_app, "lost.task", timeout=10)

        assert result == {"ok": True}
        notify.assert_called_once()

    def test_short_timeout_skips_redis_wait(self, mock_celery_app):
        """Остаток < 1с не ходит в BLPOP (Redis не принимает субсекундные таймауты)."""
        mock_async_result = MagicMock()
        mock_async_result.id = "short-id"
        mock_async_result.ready.return_value = False
        mock_celery_app.send_task.return_value = mock_async_result

        with patch("memory_server.tools.task_bridge._blpop_notify") as notify:
            with pytest.raises(TimeoutError, match="timed out"):
                run_task(mock_celery_app, "short.task", timeout=0.05)

        notify.assert_not_called()


# ── event-driven: _blpop_notify (BLPOP на ключ события) ──────────


class TestBlpopNotify:
    def _patch_client(self, blpop_return):
        client = MagicMock()
        client.blpop.return_value = blpop_return
        return patch(
            "memory_server.tools.task_bridge._get_notify_client",
            return_value=client,
        ), client

    def test_event_received_returns_true(self):
        with self._patch_client((f"{NOTIFY_KEY_PREFIX}t1", "SUCCESS"))[0]:
            from memory_server.tools.task_bridge import _blpop_notify

            assert _blpop_notify("t1", timeout=5.0) is True

    def test_chunk_timeout_returns_false(self):
        with self._patch_client(None)[0]:
            from memory_server.tools.task_bridge import _blpop_notify

            assert _blpop_notify("t2", timeout=5.0) is False

    def test_redis_error_degrades_to_false(self):
        """Notify-канал недоступен → False (не исключение): результат
        всё равно читается из Celery backend по контролю ready()."""
        with patch(
            "memory_server.tools.task_bridge._get_notify_client",
            side_effect=redis_sync.RedisError("connection refused"),
        ):
            from memory_server.tools.task_bridge import _blpop_notify

            assert _blpop_notify("t3", timeout=5.0) is False

    def test_blpop_timeout_floored_to_one_second(self):
        """BLPOP не принимает субсекундные таймауты: минимум 1с."""
        patcher, client = self._patch_client(None)
        with patcher:
            from memory_server.tools.task_bridge import _blpop_notify

            _blpop_notify("t4", timeout=0.5)

        client.blpop.assert_called_once_with(
            [f"{NOTIFY_KEY_PREFIX}t4"], timeout=1
        )


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
