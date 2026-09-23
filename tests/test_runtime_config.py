"""Юнит-тесты RuntimeConfig (Ф2): слои резолва, TTL, LISTEN-callback,
sync-bootstrap, env-детекция model_fields_set."""

from typing import Any

import pytest

from memory_server.config import Settings
from memory_server.runtime_config import (
    RuntimeConfig,
    compute_env_overrides,
    load_effective_values_sync,
)
from memory_server.settings_store import SettingRecord, SettingsRepository, get_default


def _record(key: str, value: Any) -> SettingRecord:
    from memory_server.settings_store import REGISTRY

    spec = REGISTRY[key]
    return SettingRecord(
        key=key, value=value, value_type=spec.value_type, group_key=spec.group,
        default_value=get_default(key),
    )


class StubRepo(SettingsRepository):
    """Репозиторий без пула: фиксированный словарь записей."""

    def __init__(self, records: dict[str, SettingRecord]) -> None:
        super().__init__(pool=None)  # type: ignore[arg-type]
        self._stub = records

    async def load_all(self) -> dict[str, SettingRecord]:
        return dict(self._stub)


# ════════════════════════ Слои резолва ════════════════════════


class TestLayers:
    def test_default_source(self):
        runtime = RuntimeConfig()
        assert runtime.get("stale_days") == 30
        assert runtime.effective_source("stale_days") == "default"

    def test_db_layer_overrides_default(self):
        runtime = RuntimeConfig(db_values={"stale_days": 14})
        assert runtime.get("stale_days") == 14
        assert runtime.effective_source("stale_days") == "db"

    def test_get_unknown_key_raises(self):
        runtime = RuntimeConfig()
        with pytest.raises(KeyError):
            runtime.get("no_such_key")

    def test_db_value_outside_registry_ignored(self):
        runtime = RuntimeConfig(db_values={"ghost_key": 1, "stale_days": 7})
        assert runtime.get("stale_days") == 7
        with pytest.raises(KeyError):
            runtime.get("ghost_key")  # в снапшот не попадает

    def test_snapshot_is_copy(self):
        runtime = RuntimeConfig()
        snap = runtime.snapshot()
        snap["stale_days"] = 999
        assert runtime.get("stale_days") == 30


# ════════════════════════ Env-детекция ════════════════════════


class TestEnvOverrides:
    def test_model_fields_set_detection(self):
        # kwargs попадают в model_fields_set так же, как env-переменные
        config = Settings(_env_file=None, stale_days=77)
        overrides = compute_env_overrides(config)
        assert overrides.get("stale_days") == 77
        assert "rrf_k" not in overrides  # совпало с дефолтом → не env

    def test_unknown_csv_key_ignored(self, monkeypatch):
        config = Settings(_env_file=None, runtime_env_overrides="ghost_key,schedule.nothing")
        overrides = compute_env_overrides(config)
        assert overrides == {}

    def test_csv_key_parsed_by_type(self, monkeypatch):
        monkeypatch.setenv("SCHEDULE.REBUILD_CONTEXTS".upper(), "")
        monkeypatch.setenv("STALE_DAYS", "42")
        config = Settings(_env_file=None, runtime_env_overrides="stale_days")
        assert config.runtime_env_overrides == "stale_days"
        overrides = compute_env_overrides(config)
        assert overrides.get("stale_days") == 42

    def test_csv_json_parsed(self, monkeypatch):
        monkeypatch.setenv("SCHEDULE.MARK_STALE", '{"type": "interval", "seconds": 900}')
        config = Settings(_env_file=None, runtime_env_overrides="schedule.mark_stale")
        overrides = compute_env_overrides(config)
        assert overrides["schedule.mark_stale"] == {"type": "interval", "seconds": 900}


# ════════════════════════ refresh / TTL / LISTEN ════════════════════════


class TestRefresh:
    @pytest.mark.asyncio
    async def test_refresh_pulls_db_values(self):
        runtime = RuntimeConfig()
        runtime.bind(StubRepo({"stale_days": _record("stale_days", 21)}))
        await runtime.refresh()
        assert runtime.get("stale_days") == 21
        assert runtime.effective_source("stale_days") == "db"

    @pytest.mark.asyncio
    async def test_refresh_failure_keeps_snapshot(self):
        class BrokenRepo(StubRepo):
            async def load_all(self):
                raise RuntimeError("db down")

        runtime = RuntimeConfig(db_values={"stale_days": 14})
        runtime.bind(BrokenRepo({}))
        await runtime.refresh()
        assert runtime.get("stale_days") == 14

    @pytest.mark.asyncio
    async def test_ttl_triggers_background_refresh_on_get(self):
        import time

        runtime = RuntimeConfig()
        repo = StubRepo({"stale_days": _record("stale_days", 55)})
        runtime.bind(repo)
        runtime._loaded_at = time.monotonic() - 120  # снапшот протух
        assert runtime.get("stale_days") == 30  # старое значение, без блокировки
        await asyncio_sleep_zero()  # фоновая догрузка успевает отработать
        assert runtime.get("stale_days") == 55

    @pytest.mark.asyncio
    async def test_notify_unknown_payload_no_refresh(self):
        runtime = RuntimeConfig()
        repo = StubRepo({"stale_days": _record("stale_days", 55)})
        runtime.bind(repo)
        runtime._on_notify(None, 1, "settings_changed", "ghost_key")
        await asyncio_sleep_zero()
        assert runtime.get("stale_days") == 30

    @pytest.mark.asyncio
    async def test_notify_known_payload_refreshes(self):
        """Smoke Ф2: изменение в БД → NOTIFY → снапшот обновился."""
        runtime = RuntimeConfig()
        repo = StubRepo({"stale_days": _record("stale_days", 55)})
        runtime.bind(repo)
        runtime._on_notify(None, 1, "settings_changed", "stale_days")
        await asyncio_sleep_zero()
        assert runtime.get("stale_days") == 55


async def asyncio_sleep_zero() -> None:
    import asyncio

    for _ in range(3):
        await asyncio.sleep(0)


# ════════════════════════ Sync-bootstrap ════════════════════════


class TestSyncBootstrap:
    def test_reads_db_values(self, monkeypatch):
        async def fake_read():
            return {"stale_days": 12, "ghost": 1}

        monkeypatch.setattr("memory_server.runtime_config._read_db_values", fake_read)
        values = load_effective_values_sync({"stale_days"})
        assert values == {"stale_days": 12}

    def test_db_failure_falls_back_to_defaults(self, monkeypatch):
        async def broken_read():
            raise RuntimeError("no pg")

        monkeypatch.setattr("memory_server.runtime_config._read_db_values", broken_read)
        values = load_effective_values_sync({"stale_days", "rrf_k"})
        assert values == {"stale_days": 30, "rrf_k": 60}
