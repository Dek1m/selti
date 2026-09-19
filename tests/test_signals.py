"""Tests for tasks/signals.py — bridge notify (Фаза 3.3, event-driven мост).

Воркер после задачи от task_bridge публикует LPUSH в ключ
selti:bridge:done:{task_id} — на него просыпается BLPOP в task_bridge.
"""

from unittest.mock import MagicMock, patch

import pytest

from memory_server.tasks import signals


@pytest.fixture(scope="module", autouse=True)
def _connected_signals():
    """Гарантируем подписку сигнал-хендлеров (celery_app мог не импортироваться).

    Повторная подписка идемпотентна для этих тестов: _bridge_waiters — set
    (prerun.add одного task_id), postrun первого подписчика discard'ит
    task_id, остальные — пустое множество, ровно один notify.
    """
    signals.setup_signals(MagicMock())
    yield


def _send_prerun(task_id: str, headers: dict) -> None:
    from celery.signals import task_prerun

    task = MagicMock()
    task.name = "memory_server.tasks.memory_tasks.get_stats"
    task_prerun.send(
        sender=task, task_id=task_id, task=task, args=[], kwargs={}, headers=headers
    )


def _send_postrun(task_id: str, state: str = "SUCCESS") -> None:
    from celery.signals import task_postrun

    task = MagicMock()
    task.name = "memory_server.tasks.memory_tasks.get_stats"
    task_postrun.send(
        sender=task, task_id=task_id, task=task, retval=None, state=state
    )


class TestBridgeNotifySignals:
    def test_prerun_registers_bridge_waiter(self):
        _send_prerun("t-reg", headers={"bridge_wait": True})
        assert "t-reg" in signals._bridge_waiters
        signals._bridge_waiters.discard("t-reg")

    def test_prerun_without_flag_not_registered(self):
        _send_prerun("t-noflag", headers={})
        assert "t-noflag" not in signals._bridge_waiters

    def test_postrun_notifies_registered_waiter_once(self):
        signals._bridge_waiters.add("t-notify")
        with patch.object(signals, "_notify_bridge_done") as notify:
            _send_postrun("t-notify", state="SUCCESS")
        notify.assert_called_once_with("t-notify", "SUCCESS")
        assert "t-notify" not in signals._bridge_waiters

    def test_postrun_ignores_unknown_task(self):
        with patch.object(signals, "_notify_bridge_done") as notify:
            _send_postrun("t-unknown", state="SUCCESS")
        notify.assert_not_called()


class TestNotifyBridgeDone:
    def test_publishes_lpush_with_ttl(self):
        client = MagicMock()
        with patch.object(signals, "_notify_client", client):
            signals._notify_bridge_done("t-1", "SUCCESS")

        pipe = client.pipeline.return_value
        pipe.lpush.assert_called_once_with("selti:bridge:done:t-1", "SUCCESS")
        pipe.expire.assert_called_once_with(
            "selti:bridge:done:t-1", signals._NOTIFY_TTL_SECONDS
        )
        pipe.execute.assert_called_once()

    def test_publish_failure_does_not_raise(self):
        """Сбой публикации не роняет postrun: bridge подхватит по ready-контролю."""
        import redis as redis_sync

        with patch.object(
            signals, "_notify_client"
        ) as client, patch.object(signals.logger, "warning") as warn:
            client.pipeline.side_effect = redis_sync.RedisError("down")
            signals._notify_bridge_done("t-2", "FAILURE")

        warn.assert_called_once()
