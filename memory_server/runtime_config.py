"""RuntimeConfig — in-memory снапшот runtime-настроек (Ф2).

Резолв значения (§1 реестра): env/compose (явно заданный) > БД
app_settings > дефолт config.py. get(key) — sync и без IO: чтение из
снапшота; актуализация — pg LISTEN 'settings_changed' (мгновенно) +
TTL-страховка 60 с (ленивая проверка возраста на get: LISTEN-канал
мёртв — фоновой догрузкой). При недоступной БД — дефолты + WARN,
процесс не падает.

Отдельный sync-путь load_effective_values_sync() — для стартовых кодов
без event loop (celeryd_init/beat_init): одноразовое соединение, без
пула, чтобы не привязывать SeltiState к чужому loop.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Mapping

import asyncpg

from memory_server.config import settings
from memory_server.logger import get_logger
from memory_server.settings_store import (
    REGISTRY,
    SettingsRepository,
    compute_env_overrides,
    get_default,
)

logger = get_logger(__name__)

# TTL-страховка: возраст снапшота, после которого get() инициирует догрузку
SNAPSHOT_TTL_SECONDS = 60.0

# Период ping LISTEN-соединения и пауза реконнекта
_LISTEN_PING_SECONDS = 30.0
_LISTEN_RECONNECT_DELAY = 5.0

_SETTINGS_CHANNEL = "settings_changed"


class RuntimeConfig:
    """Снапшот конфигурации процесса: env-оверрайды + БД-значения + дефолты.

    Создаётся дёшево (дефолты, без IO) — сервисы и тесты работают сразу;
    bind(repository) + start() подключают БД-слой и LISTEN-канал.
    """

    def __init__(self, db_values: Mapping[str, Any] | None = None) -> None:
        # Env-детекция один раз за жизнь процесса: model_fields_set фиксирован
        # на старте (runtime_env_overrides читается при создании Settings).
        self._env: dict[str, Any] = compute_env_overrides()
        self._db: dict[str, Any] = dict(db_values or {})
        self._snapshot: dict[str, Any] = self._resolve()
        self._loaded_at: float = time.monotonic()
        self._repository: SettingsRepository | None = None
        self._refresh_lock: asyncio.Lock = asyncio.Lock()
        self._refreshing: bool = False
        self._listener_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ════════════════════════ Чтение ════════════════════════

    def get(self, key: str) -> Any:
        """Эффективное значение ключа: sync, ноль IO (dict lookup).

        TTL-страховка: снапшот старше 60 с при живом репозитории —
        фоновая догрузка (следующий get увидит свежее значение).
        """
        if self._repository is not None and not self._refreshing:
            age = time.monotonic() - self._loaded_at
            if age > SNAPSHOT_TTL_SECONDS:
                self._schedule_background_refresh()
        try:
            return self._snapshot[key]
        except KeyError:
            raise KeyError(f"unknown runtime setting: {key}") from None

    def snapshot(self) -> dict[str, Any]:
        """Копия снапшота (overlay для cross-field валидации в store)."""
        return dict(self._snapshot)

    def effective_source(self, key: str) -> str:
        """Откуда действует значение: 'env' | 'db' | 'default'."""
        if key in self._env:
            return "env"
        if key in self._db:
            return "db"
        return "default"

    def env_keys(self) -> frozenset[str]:
        return frozenset(self._env)

    def is_env_locked(self, key: str) -> bool:
        return key in self._env

    # ════════════════════════ Актуализация ════════════════════════

    def _resolve(self) -> dict[str, Any]:
        """Слои: дефолты ← БД-переопределения ← env-блокировки."""
        snapshot = {key: get_default(key) for key in REGISTRY}
        snapshot.update({k: v for k, v in self._db.items() if k in REGISTRY})
        snapshot.update(self._env)
        return snapshot

    def _rebuild(self) -> None:
        self._snapshot = self._resolve()
        self._loaded_at = time.monotonic()

    def bind(self, repository: SettingsRepository) -> None:
        """Подключить БД-слой (пул + валидация записи)."""
        self._repository = repository

    async def refresh(self) -> None:
        """Перечитать БД-значения и пересобрать снапшот. Ошибка — WARN,
        снапшот остаётся прежним (процесс не падает)."""
        if self._repository is None:
            return
        async with self._refresh_lock:
            self._refreshing = True
            try:
                records = await self._repository.load_all()
                self._db = {k: r.value for k, r in records.items() if k in REGISTRY}
                self._rebuild()
            except Exception as exc:
                logger.warning(
                    "runtime_config: refresh failed, keeping snapshot",
                    extra={"error": str(exc)[:300], "error_type": type(exc).__name__},
                )
                self._loaded_at = time.monotonic()
            finally:
                self._refreshing = False

    def _schedule_background_refresh(self) -> None:
        """TTL-страховка из sync get(): докачать снапшот, не блокируя."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # sync-контекст без loop — отдаём старый снапшот
        if not self._refresh_lock.locked():
            loop.create_task(self.refresh())

    # ════════════════════════ LISTEN-канал ════════════════════════

    async def start(self) -> None:
        """Прогрев снапшота из БД + запуск LISTEN-слушателя."""
        await self.refresh()
        if self._listener_task is None and self._repository is not None:
            self._stop_event.clear()
            self._listener_task = asyncio.get_running_loop().create_task(
                self._listen_loop(), name="settings-listen"
            )

    async def stop(self) -> None:
        """Остановить слушателя (graceful shutdown процесса)."""
        self._stop_event.set()
        task, self._listener_task = self._listener_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _on_notify(self, conn: asyncpg.Connection, pid: int, channel: str, payload: str) -> None:
        """pg_notify('settings_changed', key) → фоновый полный refresh.

        Полный (а не точечный): 97 строк дёшевы, а cross-field инварианты
        требуют консистентный снапшот. payload вне реестра → WARN + игнор.
        """
        if payload and payload not in REGISTRY:
            logger.warning("runtime_config: notify for unknown key ignored", extra={"key": payload})
            return
        self._schedule_background_refresh()

    async def _listen_loop(self) -> None:
        # Отдельное соединение: пул не держит LISTEN — acquire/release
        # переключает соединения между задачами и рвёт подписку.
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn: asyncpg.Connection | None = None
        while not self._stop_event.is_set():
            try:
                conn = await asyncpg.connect(dsn)
                await conn.add_listener(_SETTINGS_CHANNEL, self._on_notify)
                logger.info("runtime_config: LISTEN %s", _SETTINGS_CHANNEL)
                while not self._stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=_LISTEN_PING_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # keep-alive: мёртвое соединение ловим исключением,
                        # а не тишиною сигнала
                        await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "runtime_config: listener disconnected, reconnecting",
                    extra={"error": str(exc)[:300], "delay": _LISTEN_RECONNECT_DELAY},
                )
                await asyncio.sleep(_LISTEN_RECONNECT_DELAY)
            finally:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    conn = None


# ════════════════════════ Sync-стартовый путь ════════════════════════


async def _read_db_values() -> dict[str, Any]:
    """Разовое чтение app_settings отдельным соединением (без пула).

    Короткие таймауты: вызов сидит в тике beat — лежащая БД не должна
    вешать планировщик дольше 5 секунд. value::text + json.loads:
    соединение БЕЗ jsonb-кодека пула (db/pool.py) — сырой jsonb пришёл бы
    строкой и beat/worker-bootstrap тихо деградировал бы в дефолты.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn, timeout=5.0, command_timeout=5.0)
    try:
        rows = await conn.fetch("SELECT key, value::text AS value FROM app_settings")
        return {row["key"]: json.loads(row["value"]) for row in rows}
    finally:
        await conn.close()


def load_effective_values_sync(keys: set[str] | None = None) -> dict[str, Any]:
    """Effective-значения без event loop (celeryd_init / beat_init / Scheduler).

    Свой временный loop и соединение — SeltiState не трогаем (его пул
    привязан к loop веб-процесса/воркера). БД недоступна → дефолты + WARN.
    """
    env = compute_env_overrides()
    try:
        db_values = asyncio.run(_read_db_values())
    except Exception as exc:
        logger.warning(
            "runtime_config: sync bootstrap without DB (defaults in effect)",
            extra={"error": str(exc)[:300], "error_type": type(exc).__name__},
        )
        db_values = {}
    values: dict[str, Any] = {key: get_default(key) for key in REGISTRY}
    values.update({k: v for k, v in db_values.items() if k in REGISTRY})
    values.update(env)
    if keys is not None:
        return {k: values[k] for k in keys if k in values}
    return values
