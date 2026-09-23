"""Юнит-тесты beat-слоя Ф2: build_beat_schedule + RuntimeScheduler.sync.

Проверяют «налету»-часть: расписание строится из runtime-значений и
перечитывается при изменении (без рестарта контейнера beat)."""

from unittest.mock import MagicMock, patch

import pytest
from celery.schedules import crontab

from memory_server.celery_app import SCHEDULE_TASKS, RuntimeScheduler, build_beat_schedule
from memory_server.runtime_config import RuntimeConfig
from memory_server.settings_store import get_default


def _defaults() -> dict:
    return {key: get_default(key) for key in SCHEDULE_TASKS}


class TestBuildBeatSchedule:
    def test_defaults_produce_all_13_entries(self):
        schedule = build_beat_schedule(_defaults())
        assert len(schedule) == 13
        assert schedule["update-worker-stats"]["task"] == "worker_stats.update"
        assert schedule["update-worker-stats"]["schedule"] == 30.0
        assert schedule["linker-l2-verdicts"]["schedule"] == 300.0

    def test_crontab_entries_are_celery_objects(self):
        schedule = build_beat_schedule(_defaults())
        assert schedule["refresh-clusters"]["schedule"] == crontab(hour=2, minute=0)
        assert schedule["gc-superseded"]["schedule"] == crontab(
            day_of_week="sun", hour=5, minute=0
        )

    def test_runtime_override_changes_interval(self):
        values = _defaults()
        values["schedule.linker_l2_verdicts"] = {"type": "interval", "seconds": 60}
        schedule = build_beat_schedule(values)
        assert schedule["linker-l2-verdicts"]["schedule"] == 60.0

    def test_broken_value_falls_back_to_default(self):
        values = _defaults()
        values["schedule.mark_stale"] = {"type": "crontab", "minute": "abc"}
        schedule = build_beat_schedule(values)
        assert schedule["mark-stale"]["schedule"] == crontab(hour=4, minute=0)


class TestRuntimeSchedulerSync:
    def _scheduler(self) -> tuple[RuntimeScheduler, MagicMock]:
        app = MagicMock()
        app.conf.beat_schedule = {}
        scheduler = RuntimeScheduler(app=app)
        return scheduler, app

    def test_setup_schedule_loads_from_runtime(self):
        scheduler, app = self._scheduler()
        with patch(
            "memory_server.celery_app.read_schedule_values", return_value=_defaults()
        ):
            scheduler.setup_schedule()
        assert len(app.conf.beat_schedule) == 13
        assert len(scheduler.data) == 13
        assert scheduler._current_raw == _defaults()

    def test_sync_without_change_keeps_entries(self):
        scheduler, app = self._scheduler()
        with patch(
            "memory_server.celery_app.read_schedule_values", return_value=_defaults()
        ):
            scheduler.setup_schedule()
            entries_before = dict(scheduler.data)
            scheduler.sync()
        assert scheduler.data == entries_before

    def test_sync_with_change_replaces_entries(self):
        scheduler, app = self._scheduler()
        changed = _defaults()
        changed["schedule.rebuild_contexts"] = {"type": "interval", "seconds": 120}
        with patch(
            "memory_server.celery_app.read_schedule_values", return_value=_defaults()
        ):
            scheduler.setup_schedule()
        with patch(
            "memory_server.celery_app.read_schedule_values", return_value=changed
        ):
            scheduler.sync()
        # Entry конвертирует float в celery schedule — сверяем по периоду
        assert scheduler.data["rebuild-contexts"].schedule.run_every.total_seconds() == 120.0
        assert app.conf.beat_schedule["rebuild-contexts"]["schedule"] == 120.0

    def test_read_schedule_values_maps_runtime_layer(self):
        """effective-слой: БД-значение важнее дефолта реестра."""
        runtime = RuntimeConfig(
            db_values={"schedule.linker_l2_verdicts": {"type": "interval", "seconds": 900}}
        )
        assert runtime.get("schedule.linker_l2_verdicts") == {
            "type": "interval",
            "seconds": 900,
        }


class TestRuntimeSchedulerTick:
    def test_tick_does_not_shadow_parent_event_t(self):
        """Регрессия прод-инцидента 23.09: tick передавал event_timeout=None
        ПЕРВЫМ позиционным аргументом родителю, у которого celery 5.6
        первым параметром идёт event_t (локальное замыкание heapq) —
        heappush(event_t(...)) падал "'NoneType' object is not callable"
        на каждом due-тике beat."""
        from celery.beat import Scheduler

        scheduler, _ = RuntimeScheduler(app=MagicMock()), None
        scheduler.should_sync = lambda: False  # type: ignore[method-assign]
        captured: dict = {}

        def fake_parent_tick(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return 0.5

        with patch.object(Scheduler, "tick", fake_parent_tick):
            assert scheduler.tick() == 0.5
        # кроме неявного self — ничего: event_t/min/heappop/heappush
        # родителя не подменены
        assert captured["args"] == (scheduler,)
        assert captured["kwargs"] == {}


class TestBeatHeartbeat:
    def _scheduler(self, tmp_path, monkeypatch) -> RuntimeScheduler:
        monkeypatch.setenv("BEAT_HEARTBEAT_FILE", str(tmp_path / "beat-heartbeat"))
        # атрибут класса читается при import — подменяем на инстансе
        scheduler = RuntimeScheduler(app=MagicMock())
        scheduler.heartbeat_file = str(tmp_path / "beat-heartbeat")
        scheduler.should_sync = lambda: False  # type: ignore[method-assign]
        return scheduler

    def test_successful_tick_touches_heartbeat(self, tmp_path, monkeypatch):
        """Прод-инцидент 23.09: in-memory RuntimeScheduler не пишет
        /data/celerybeat-schedule-wal — healthcheck по WAL навечно красный.
        Маркер: mtime beat-heartbeat обновляется после успешного тика."""
        from celery.beat import Scheduler

        hb = tmp_path / "beat-heartbeat"
        scheduler = self._scheduler(tmp_path, monkeypatch)
        with patch.object(Scheduler, "tick", return_value=0.5):
            scheduler.tick()
        assert hb.exists()

    def test_crashing_parent_tick_does_not_touch_heartbeat(self, tmp_path, monkeypatch):
        from celery.beat import Scheduler

        hb = tmp_path / "beat-heartbeat"
        scheduler = self._scheduler(tmp_path, monkeypatch)

        def crashing_tick(*args, **kwargs):
            raise TypeError("'NoneType' object is not callable")

        with patch.object(Scheduler, "tick", crashing_tick):
            with pytest.raises(TypeError):
                scheduler.tick()
        assert not hb.exists()
