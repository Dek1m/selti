"""REST Полной карты (PLAN_FULL_MAP_3D M1): /api/map/meta, /api/map/full.

Стиль test_web_api.py: реальный роутер api/web.py, celery_call-мост под
моком; чтение gz-байтов из Redis подменено моком _redis_get_bytes.
Проверяется ETag/304, Content-Encoding, проброс фильтров, ошибки билда.
"""

import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import memory_server.api.web as web_api
from memory_server.tasks import map_tasks

META = {
    "version": "abc123def456",
    "node_count": 14900,
    "edge_count": 25300,
    "cluster_count": 412,
    "layout_at": "2026-09-21T02:30:00+00:00",
    "layout_stale": False,
}
BUILD_REPORT = {"cached": False, "key": "map:snap:abc123def456:1|-|-", "bytes": 42, **META}


@pytest.fixture
def bridge(monkeypatch):
    """Мок celery_call: маршрутизация по имени задачи через side_effect."""
    calls: list[tuple[str, dict]] = []

    async def _call(task_name: str, **kwargs):
        calls.append((task_name, kwargs))
        if task_name == web_api.TASK_MAP_META:
            return dict(META)
        if task_name == web_api.TASK_MAP_BUILD:
            return dict(BUILD_REPORT)
        raise AssertionError(task_name)

    monkeypatch.setattr(web_api, "celery_call", _call)
    return SimpleNamespace(calls=calls)


@pytest.fixture
def api_client(bridge, monkeypatch):
    gz = gzip.compress(json.dumps({"v": META["version"], "nodes": [], "edges": []}).encode())
    monkeypatch.setattr(web_api, "_redis_get_bytes", AsyncMock(return_value=gz))
    test_app = FastAPI()
    test_app.include_router(web_api.router)
    with TestClient(test_app) as client:
        yield client


def _task_calls(bridge, task_name: str) -> list[dict]:
    return [kwargs for name, kwargs in bridge.calls if name == task_name]


class TestMapMeta:
    def test_contract(self, api_client, bridge):
        response = api_client.get("/api/map/meta")
        assert response.status_code == 200
        assert response.json() == META
        assert _task_calls(bridge, web_api.TASK_MAP_META)


class TestMapFull:
    def test_200_gzip_snapshot_with_etag_and_cache_control(self, api_client, bridge):
        response = api_client.get("/api/map/full")
        assert response.status_code == 200
        assert response.headers["etag"] == f'"{META["version"]}"'
        assert response.headers["cache-control"] == "public, max-age=60, stale-while-revalidate=600"
        assert response.headers["content-encoding"] == "gzip"
        # httpx декодирует content-encoding сам; если нет — распаковываем
        body = response.content
        payload = json.loads(body if body[:1] == b"{" else gzip.decompress(body))
        assert payload["v"] == META["version"]
        build_kwargs = _task_calls(bridge, web_api.TASK_MAP_BUILD)
        assert build_kwargs == [{"with_preview": True, "project_id": None, "namespace": None}]

    def test_304_on_matching_if_none_match(self, api_client, bridge):
        response = api_client.get(
            "/api/map/full", headers={"If-None-Match": f'"{META["version"]}"'}
        )
        assert response.status_code == 304
        assert response.headers["etag"] == f'"{META["version"]}"'
        assert response.content == b""
        # снапшот не собирается и из Redis не читается
        assert _task_calls(bridge, web_api.TASK_MAP_BUILD) == []

    def test_304_with_weak_etag(self, api_client, bridge):
        response = api_client.get(
            "/api/map/full", headers={"If-None-Match": f'W/"{META["version"]}"'}
        )
        assert response.status_code == 304

    def test_no_304_on_stale_etag(self, api_client, bridge):
        response = api_client.get("/api/map/full", headers={"If-None-Match": '"oldver"'})
        assert response.status_code == 200

    def test_filters_forwarded_and_etag_extended(self, api_client, bridge, monkeypatch):
        gz = gzip.compress(b'{"v":"x","nodes":[],"edges":[]}')
        monkeypatch.setattr(web_api, "_redis_get_bytes", AsyncMock(return_value=gz))
        response = api_client.get(
            "/api/map/full",
            params={"with_preview": False, "project_id": "selti", "namespace": "project_meta"},
        )
        assert response.status_code == 200
        build_kwargs = _task_calls(bridge, web_api.TASK_MAP_BUILD)
        assert build_kwargs == [{
            "with_preview": False, "project_id": "selti", "namespace": "project_meta",
        }]
        # тот же version, но другое тело → ETag обязан отличаться от чистой version
        assert response.headers["etag"] != f'"{META["version"]}"'

    def test_etag_taken_from_build_report_not_stale_meta(self, api_client, monkeypatch):
        """F5: ETag описывает отданное тело. Данные сменились между meta- и
        build-вызовами — метка берётся из ответа сборки (version B), не из
        устаревшей меты (version A)."""
        stale_meta = dict(META, version="aaaaaaaaaaaa")
        fresh_report = {"cached": False, "key": "map:snap:bbbbbbbbbbbb:1|-|-", "bytes": 42,
                        **dict(META, version="bbbbbbbbbbbb")}

        async def two_versions(task_name: str, **kwargs):
            if task_name == web_api.TASK_MAP_META:
                return stale_meta
            return fresh_report

        gz = gzip.compress(b'{"v":"bbbbbbbbbbbb","nodes":[],"edges":[]}')
        monkeypatch.setattr(web_api, "celery_call", two_versions)
        monkeypatch.setattr(web_api, "_redis_get_bytes", AsyncMock(return_value=gz))

        response = api_client.get("/api/map/full")
        assert response.status_code == 200
        assert response.headers["etag"] == '"bbbbbbbbbbbb"'
        # следующий запрос с этой меткой — честный 304 по свежей метe
        fresh_meta = dict(META, version="bbbbbbbbbbbb")
        monkeypatch.setattr(web_api, "celery_call",
                            AsyncMock(return_value=fresh_meta))
        again = api_client.get("/api/map/full", headers={"If-None-Match": '"bbbbbbbbbbbb"'})
        assert again.status_code == 304

    def test_invalid_with_preview_422(self, api_client):
        # pydantic-bool принимает yes/no/on/off — 422 даёт мусор
        assert api_client.get("/api/map/full", params={"with_preview": "bogus"}).status_code == 422

    def test_stalled_build_503(self, api_client, bridge, monkeypatch):
        async def stalled(task_name: str, **kwargs):
            if task_name == web_api.TASK_MAP_BUILD:
                return {"stalled": True, "key": "map:snap:x:1|-|-", **META}
            return dict(META)

        monkeypatch.setattr(web_api, "celery_call", stalled)
        assert api_client.get("/api/map/full").status_code == 503

    def test_missing_cache_after_build_502(self, api_client, bridge, monkeypatch):
        monkeypatch.setattr(web_api, "_redis_get_bytes", AsyncMock(return_value=None))
        assert api_client.get("/api/map/full").status_code == 502


class TestDirtyBumpTasks:
    def test_bump_map_dirty_delegates_to_service(self, monkeypatch):
        bumped = []

        def fake_run_async(coro_func, *args, **kwargs):
            return coro_func()

        monkeypatch.setattr(map_tasks, "run_async", fake_run_async)
        service = SimpleNamespace(bump_dirty=lambda: bumped.append(1))
        monkeypatch.setattr(map_tasks, "_get_map_service", lambda: service)

        map_tasks.bump_map_dirty()
        assert bumped == [1]

    def test_bump_survives_service_failure(self, monkeypatch):
        def broken_get_service():
            raise RuntimeError("redis down")

        monkeypatch.setattr(map_tasks, "_get_map_service", broken_get_service)
        # не поднимается: инвалидация не роняет кампанию-носитель
        map_tasks.bump_map_dirty()
