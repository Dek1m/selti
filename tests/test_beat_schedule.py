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
