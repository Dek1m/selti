"""Координаты map_layout в ответах /api (созвездие web-морды, with_positions).

Слои: MapService.positions (мок pool: строки/нет таблицы/пустой батч) и
REST-контракт трёх эндпоинтов-источников узлов созвездия — /api/search,
/api/memories/{id}, /api/memories/{id}/relations — через мок celery-моста
(стиль test_web_api.py). Флаг off по умолчанию: выдача бит-в-бит прежняя.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory_server.config import Settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.db import queries as q
from memory_server.memory.map_service import MapService

import memory_server.api.web as web_api


# ══════════════════════════════════════════════════════════════════
# MapService.positions — батч-SELECT по ids, LEFT JOIN семантика
# ══════════════════════════════════════════════════════════════════


def _service_with_rows(rows, captured=None):
    """MapService с моком пула: fetch по MAP_LAYOUT_POSITIONS_SQL → rows."""
    pool = MagicMock()
    conn = MagicMock()

    async def fetch(sql, *args):
        if captured is not None:
            captured.append((sql, args))
        if sql is q.MAP_LAYOUT_POSITIONS_SQL:
            return rows
        raise AssertionError(f"unexpected fetch: {sql}")

    conn.fetch = fetch
    acm = AsyncMock()
    acm.__aenter__.return_value = conn
    pool.acquire.return_value = acm
    return MapService(
        pool=pool,
        redis_provider=AsyncMock(),
        project_repository=AsyncMock(),
        runtime=RuntimeConfig(db_values={}),
    )


class TestMapServicePositions:
    @pytest.mark.asyncio
    async def test_rows_become_id_to_xyz_map(self):
        service = _service_with_rows([
            {"node_id": "a" * 8 + "-0000-0000-0000-000000000000", "x": 1.5, "y": -2.0, "z": 900.25},
        ])
        positions = await service.positions(["a" * 8 + "-0000-0000-0000-000000000000"])
        assert positions == {
            "a" * 8 + "-0000-0000-0000-000000000000": [1.5, -2.0, 900.25],
        }

    @pytest.mark.asyncio
    async def test_missing_rows_absent_from_answer(self):
        """LEFT JOIN семантика: гранула без строки не попадает в ответ."""
        service = _service_with_rows([
            {"node_id": "b" * 8 + "-0000-0000-0000-000000000000", "x": 0.0, "y": 0.0, "z": 0.0},
        ])
        positions = await service.positions([
            "a" * 8 + "-0000-0000-0000-000000000000",  # нет строки
            "b" * 8 + "-0000-0000-0000-000000000000",
        ])
        assert "a" * 8 + "-0000-0000-0000-000000000000" not in positions
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_no_table_returns_empty(self):
        """До миграции 024 таблицы нет — тихий пустой словарь."""
        pool = MagicMock()
        conn = MagicMock()

        async def fetch(sql, *args):
            raise asyncpg.UndefinedTableError()

        conn.fetch = fetch
        acm = AsyncMock()
        acm.__aenter__.return_value = conn
        pool.acquire.return_value = acm
        service = MapService(
            pool=pool,
            redis_provider=AsyncMock(),
            project_repository=AsyncMock(),
            runtime=RuntimeConfig(db_values={}),
        )
        assert await service.positions(["c" * 8 + "-0000-0000-0000-000000000000"]) == {}

    @pytest.mark.asyncio
    async def test_empty_ids_skip_pool(self):
        captured: list = []
        service = _service_with_rows([], captured)
        assert await service.positions([]) == {}
        assert captured == []  # в пул не ходим вовсе


# ══════════════════════════════════════════════════════════════════
# REST: with_positions на трёх эндпоинтах-источниках созвездия
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def bridge(monkeypatch):
    """Мок celery_call-моста: dispatch маршрутизирует по имени задачи."""
    calls: list[tuple[str, dict]] = []

    async def _call(task_name: str, **kwargs):
        calls.append((task_name, kwargs))
        return await holder.dispatch(task_name, kwargs)

    holder = SimpleNamespace(dispatch=AsyncMock(return_value={}), calls=calls)
    monkeypatch.setattr(web_api, "celery_call", _call)
    return holder


@pytest.fixture
def api_client(bridge):
    test_app = FastAPI()
    test_app.include_router(web_api.router)
    with TestClient(test_app) as client:
        yield client


class TestSearchPositions:
    def test_hits_get_position_field(self, api_client, bridge):
        """Хит со строкой map_layout — position; без строки — поля нет."""
        hit_a = {"id": "a" * 8 + "-0000-0000-0000-000000000000", "content": "placed"}
        hit_b = {"id": "b" * 8 + "-0000-0000-0000-000000000000", "content": "rowless"}

        async def dispatch(task_name, kwargs):
            if task_name == web_api.TASK_SEARCH:
                return [hit_a, hit_b]
            if task_name == web_api.TASK_MAP_POSITIONS:
                assert kwargs["ids"] == [hit_a["id"], hit_b["id"]]  # батч одним SELECT
                return {hit_a["id"]: [10.0, -20.0, 340.5]}
            raise AssertionError(task_name)

        bridge.dispatch.side_effect = dispatch

        response = api_client.get("/api/search", params={"query": "x", "with_positions": "true"})

        assert response.status_code == 200
        results = response.json()
        assert results[0]["position"] == [10.0, -20.0, 340.5]
        assert "position" not in results[1]

    def test_flag_off_keeps_contract(self, api_client, bridge):
        """Без флага задача позиций не зовётся — прежний ответ бит-в-бит."""
        bridge.dispatch.side_effect = AsyncMock(return_value=[{"id": "mem-1"}])

        response = api_client.get("/api/search", params={"query": "x"})

        assert response.status_code == 200
        assert response.json() == [{"id": "mem-1"}]
        assert [name for name, _ in bridge.calls] == [web_api.TASK_SEARCH]


class TestMemoryPositions:
    def test_record_gets_position(self, api_client, bridge):
        memory_id = "a" * 8 + "-0000-0000-0000-000000000000"

        async def dispatch(task_name, kwargs):
            if task_name == web_api.TASK_GET:
                return {"id": memory_id, "content": "granule"}
            if task_name == web_api.TASK_MAP_POSITIONS:
                return {memory_id: [-100.0, 5.0, 999.0]}
            raise AssertionError(task_name)

        bridge.dispatch.side_effect = dispatch

        response = api_client.get(f"/api/memories/{memory_id}", params={"with_positions": "true"})

        assert response.status_code == 200
        assert response.json()["position"] == [-100.0, 5.0, 999.0]

    def test_relations_neighbors_map(self, api_client, bridge):
        """Соседи обеих сторон одним батчем: поле positions {id: [x, y, z]}."""
        source = "a" * 8 + "-0000-0000-0000-000000000000"
        out_neighbor = "b" * 8 + "-0000-0000-0000-000000000000"
        in_neighbor = "c" * 8 + "-0000-0000-0000-000000000000"

        async def dispatch(task_name, kwargs):
            if task_name == web_api.TASK_GET_RELATIONS:
                return {
                    "incoming": [{"id": "rel-1", "source_id": in_neighbor}],
                    "outgoing": [{"id": "rel-2", "target_id": out_neighbor}],
                }
            if task_name == web_api.TASK_MAP_POSITIONS:
                # висячий target_id None и дубль source не раздувают батч
                assert kwargs["ids"] == sorted([in_neighbor, out_neighbor])
                return {out_neighbor: [1.0, 2.0, 3.0]}
            raise AssertionError(task_name)

        bridge.dispatch.side_effect = dispatch

        response = api_client.get(f"/api/memories/{source}/relations", params={"with_positions": "true"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["positions"] == {out_neighbor: [1.0, 2.0, 3.0]}
        assert payload["incoming"][0]["id"] == "rel-1"

    def test_relations_hanging_targets_excluded(self, api_client, bridge):
        """target_id NULL (висячий конец линкера) в батч позиций не идёт."""
        async def dispatch(task_name, kwargs):
            if task_name == web_api.TASK_GET_RELATIONS:
                return {
                    "incoming": [],
                    "outgoing": [{"id": "rel-9", "target_id": None}],
                }
            if task_name == web_api.TASK_MAP_POSITIONS:
                assert kwargs["ids"] == []
                return {}
            raise AssertionError(task_name)

        bridge.dispatch.side_effect = dispatch

        response = api_client.get("/api/memories/x/relations", params={"with_positions": "true"})

        assert response.status_code == 200
        assert response.json()["positions"] == {}
