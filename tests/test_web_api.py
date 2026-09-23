"""REST-каркас веб-морды (Фаза 5.1): /api/* через celery_call-мост.

Стиль test_context_api.py: тестовое FastAPI-приложение с реальным
роутером api/web.py; celery_call-мост подменён моком — проверяется
контракт REST-слоя (проброс параметров, маппинг ошибок воркера на
HTTP-статусы, caps) без Celery/брокера. Сами задачи покрыты
test_celery_tasks.py / test_project_tasks.py.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory_server.exceptions import NotFoundError

import memory_server.api.web as web_api


PROJECT_CARD = {
    "id": "11111111-1111-1111-1111-111111111111",
    "slug": "selti",
    "name": "selti",
    "description": "memory server",
    "kind": "code",
    "status": "active",
    "local_path": "E:\\Projects\\Python\\selti",
    "repo_url": "https://github.com/Dek1m/selti",
    "docs_url": None,
    "homepage_url": None,
    "default_branch": "main",
    "created_at": "2026-09-01T10:00:00+00:00",
    "updated_at": "2026-09-18T10:00:00+00:00",
    "links": [{"link_type": "repo", "url": "https://github.com/Dek1m/selti", "title": None}],
    "technologies": [{"name": "PostgreSQL", "category": "db", "version": "16", "purpose": None}],
}


@pytest.fixture
def bridge(monkeypatch):
    """Мок celery_call-моста: calls — журнал (task_name, kwargs)."""
    calls: list[tuple[str, dict]] = []
    mock = AsyncMock(return_value={})

    async def _call(task_name: str, **kwargs):
        calls.append((task_name, kwargs))
        return await mock(task_name, **kwargs)

    monkeypatch.setattr(web_api, "celery_call", _call)
    return SimpleNamespace(mock=mock, calls=calls)


@pytest.fixture
def api_client(bridge):
    test_app = FastAPI()
    test_app.include_router(web_api.router)
    with TestClient(test_app) as client:
        yield client


def _last_call(bridge) -> tuple[str, dict]:
    return bridge.calls[-1]


class TestSearchEndpoint:
    def test_returns_tool_shaped_results(self, api_client, bridge):
        bridge.mock.return_value = [{"id": "mem-1", "content": "found", "score": 0.9}]

        response = api_client.get("/api/search", params={"query": "celery bridge"})

        assert response.status_code == 200
        assert response.json() == [{"id": "mem-1", "content": "found", "score": 0.9}]

    def test_passes_filters_to_task(self, api_client, bridge):
        bridge.mock.return_value = []
        api_client.get("/api/search", params={
            "query": "selti",
            "namespace": "project_meta",
            "project_id": "selti",
            "status": "asserted",
            "created_after": "2026-09-01T00:00:00",
            "created_before": "2026-09-18T00:00:00",
            "include_historical": False,
            "limit": 20,
        })

        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_SEARCH
        assert kwargs["query"] == "selti"
        assert kwargs["namespace"] == "project_meta"
        assert kwargs["project_id"] == "selti"
        assert kwargs["status"] == "asserted"
        assert kwargs["limit"] == 20
        # Даты уезжают в задачу ISO-строками (Celery JSON не несёт datetime)
        assert kwargs["created_after"] == "2026-09-01T00:00:00"
        assert kwargs["created_before"] == "2026-09-18T00:00:00"

    def test_query_is_required(self, api_client):
        assert api_client.get("/api/search").status_code == 422

    def test_limit_capped_at_100(self, api_client):
        """Кап теперь runtime (max_search_limit): 422 от Query ушло, хендлер
        отдаёт 400 с человекочитаемой причиной (§6 реестра)."""
        response = api_client.get("/api/search", params={"query": "x", "limit": 200})
        assert response.status_code == 400
        assert "max_search_limit" in response.json()["detail"]

    def test_unknown_status_rejected(self, api_client):
        response = api_client.get("/api/search", params={"query": "x", "status": "bogus"})
        assert response.status_code == 422


class TestMemoryEndpoint:
    def test_get_memory_contract(self, api_client, bridge):
        bridge.mock.return_value = {"id": "mem-1", "content": "granule"}

        response = api_client.get("/api/memories/mem-1")

        assert response.status_code == 200
        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_GET
        assert kwargs["memory_id"] == "mem-1"

    def test_include_history_adds_lineage_field(self, api_client, bridge):
        bridge.mock.return_value = {"id": "mem-2", "content": "current"}
        history = {"items": [{"id": "mem-1"}, {"id": "mem-2"}], "current_id": "mem-2"}

        async def _history(task_name, **kwargs):
            if task_name == web_api.TASK_GET_HISTORY:
                return history
            return {"id": "mem-2", "content": "current"}

        bridge.mock.side_effect = _history

        response = api_client.get("/api/memories/mem-2", params={"include_history": "true"})

        assert response.status_code == 200
        data = response.json()
        assert data["history"] == history
        assert {name for name, _ in bridge.calls} == {web_api.TASK_GET, web_api.TASK_GET_HISTORY}

    def test_unknown_memory_404(self, api_client, bridge):
        bridge.mock.side_effect = NotFoundError("nope", message="Memory record not found: nope")

        response = api_client.get("/api/memories/nope")

        assert response.status_code == 404
        assert "detail" in response.json()

    def test_relations_contract(self, api_client, bridge):
        """Секция «Связи» карточки (§5.1): incoming/outgoing — контракт тулa."""
        bridge.mock.return_value = {
            "incoming": [{"id": "rel-1", "link_type": "supersedes"}],
            "outgoing": [{"id": "rel-2", "link_type": "references"}],
        }

        response = api_client.get(
            "/api/memories/mem-1/relations", params={"link_type": "supersedes"}
        )

        assert response.status_code == 200
        assert response.json()["outgoing"][0]["link_type"] == "references"
        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_GET_RELATIONS
        assert kwargs == {"source_id": "mem-1", "link_type": "supersedes"}

    def test_similar_excludes_seed_granule(self, api_client, bridge):
        """«Показать похожие»: контент гранулы — seed; сама гранула исключена."""

        async def _seed_then_similar(task_name, **kwargs):
            if task_name == web_api.TASK_GET:
                return {"id": "mem-1", "content": "seed text"}
            return [
                {"id": "mem-2", "content": "neighbour", "score": 0.9},
                {"id": "mem-1", "content": "seed text", "score": 0.99},
                {"id": "mem-3", "content": "far", "score": 0.8},
            ]

        bridge.mock.side_effect = _seed_then_similar

        response = api_client.get("/api/memories/mem-1/similar", params={"limit": 2})

        assert response.status_code == 200
        ids = [item["id"] for item in response.json()]
        assert ids == ["mem-2", "mem-3"]  # seed исключён, limit соблюдён
        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_FIND_SIMILAR
        assert kwargs["content"] == "seed text"
        assert kwargs["limit"] == 3  # limit+1 — запас под исключение seed


class TestGraphEndpoint:
    def test_traverse_contract(self, api_client, bridge):
        bridge.mock.return_value = {"nodes": [], "edges": [], "total_nodes": 0, "truncated": False}

        response = api_client.get(
            "/api/graph/mem-1",
            params={"depth": 2, "link_types": ["related_to", "supersedes"], "offset": 5},
        )

        assert response.status_code == 200
        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_TRAVERSE
        assert kwargs["start_id"] == "mem-1"
        assert kwargs["depth"] == 2
        assert kwargs["link_types"] == ["related_to", "supersedes"]
        assert kwargs["offset"] == 5

    def test_depth_capped_at_10(self, api_client):
        """Кап глубины — runtime max_graph_depth: 400 из хендлера."""
        response = api_client.get("/api/graph/mem-1", params={"depth": 11})
        assert response.status_code == 400
        assert "max_graph_depth" in response.json()["detail"]


class TestStatsEndpoint:
    def test_stats_contract(self, api_client, bridge):
        bridge.mock.return_value = [{"namespace": "project_meta", "count": 7, "last_updated": None}]

        response = api_client.get("/api/stats", params={"project_id": "selti"})

        assert response.status_code == 200
        assert response.json()[0]["count"] == 7
        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_STATS
        assert kwargs["project_id"] == "selti"


class TestNamespacesEndpoint:
    def test_namespaces_registry(self, api_client, bridge):
        """Реестр namespace — спектр цветов UI (WEB_UI_DESIGN §11)."""
        bridge.mock.return_value = [
            {"uid": "project_meta", "name": "Project Meta", "description": None}
        ]

        response = api_client.get("/api/namespaces")

        assert response.status_code == 200
        assert response.json()[0]["uid"] == "project_meta"
        assert _last_call(bridge)[0] == web_api.TASK_NAMESPACES


class TestProjectsEndpoints:
    def test_list_returns_registry(self, api_client, bridge):
        bridge.mock.return_value = [dict(PROJECT_CARD, links=PROJECT_CARD["links"])]

        response = api_client.get("/api/projects")

        assert response.status_code == 200
        assert response.json()["projects"][0]["slug"] == "selti"
        assert _last_call(bridge)[0] == web_api.TASK_PROJECT_LIST

    def test_get_project_card_with_stack(self, api_client, bridge):
        bridge.mock.return_value = PROJECT_CARD

        response = api_client.get("/api/projects/selti")

        assert response.status_code == 200
        assert response.json()["technologies"][0]["name"] == "PostgreSQL"
        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_PROJECT_GET
        assert kwargs["slug"] == "selti"

    def test_get_project_404(self, api_client, bridge):
        bridge.mock.side_effect = NotFoundError("nope", message="Project not found: 'nope'")

        assert api_client.get("/api/projects/nope").status_code == 404

    def test_create_201_with_defaults(self, api_client, bridge):
        bridge.mock.return_value = PROJECT_CARD

        response = api_client.post("/api/projects", json={
            "slug": "gera",
            "local_path": "E:\\Projects\\Python\\gera",
            "links": [{"link_type": "repo", "url": "https://github.com/Dek1m/gera"}],
            "technologies": [{"name": "Python", "version": "3.12"}],
        })

        assert response.status_code == 201
        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_PROJECT_CREATE
        assert kwargs["slug"] == "gera"
        # name не задан → slug (конвенция ADR-018)
        assert kwargs["name"] == "gera"
        assert kwargs["links"][0]["link_type"] == "repo"
        assert kwargs["technologies"][0]["version"] == "3.12"

    def test_create_slug_conflict_409(self, api_client, bridge):
        bridge.mock.side_effect = asyncpg.exceptions.UniqueViolationError()

        response = api_client.post("/api/projects", json={"slug": "selti"})

        assert response.status_code == 409

    def test_create_rejects_invalid_slug(self, api_client):
        response = api_client.post("/api/projects", json={"slug": "Bad Slug!"})
        assert response.status_code == 422

    def test_patch_partial_update(self, api_client, bridge):
        bridge.mock.return_value = dict(PROJECT_CARD, description="updated")

        response = api_client.patch("/api/projects/selti", json={"description": "updated"})

        assert response.status_code == 200
        task_name, kwargs = _last_call(bridge)
        assert task_name == web_api.TASK_PROJECT_UPDATE
        assert kwargs["slug"] == "selti"
        # None-поля не трогаются (PATCH-семантика)
        assert kwargs["name"] is None
        assert kwargs["links"] is None

    def test_patch_unknown_project_404(self, api_client, bridge):
        bridge.mock.side_effect = NotFoundError("nope", message="Project not found: 'nope'")

        response = api_client.patch("/api/projects/nope", json={"name": "x"})

        assert response.status_code == 404


class TestApiAuthRule:
    """Правило Фазы 5.1: localhost свободно; снаружи — только валидный Bearer."""

    def test_localhost_without_key(self):
        assert web_api.is_api_authorized("127.0.0.1", "", "") is True

    def test_ipv6_loopback(self):
        assert web_api.is_api_authorized("::1", "", "") is True

    def test_external_without_key_denied(self):
        assert web_api.is_api_authorized("10.0.0.5", "", "") is False

    def test_external_with_valid_bearer(self):
        assert web_api.is_api_authorized("10.0.0.5", "Bearer tok", "tok") is True

    def test_external_with_wrong_bearer_denied(self):
        assert web_api.is_api_authorized("10.0.0.5", "Bearer wrong", "tok") is False

    def test_localhost_allowed_even_with_key_configured(self):
        assert web_api.is_api_authorized("127.0.0.1", "", "tok") is True

    def test_unknown_client_denied(self):
        assert web_api.is_api_authorized(None, "", "") is False


class TestContextsAlias:
    """Алиас /api/contexts/{slug} — тот же fast-path снапшот, что /context/{slug}."""

    @pytest.fixture
    def ctx_client(self, bridge, mock_state, monkeypatch):
        import memory_server.api.context as ctx_api

        monkeypatch.setattr(ctx_api, "get_state", lambda: mock_state)
        test_app = FastAPI()
        test_app.include_router(ctx_api.router)
        with TestClient(test_app) as client:
            yield client

    @pytest.fixture
    def mock_state(self):
        from datetime import timezone
        from unittest.mock import MagicMock
        from memory_server.models import ProjectContext

        service = MagicMock()
        service.get_project_context = AsyncMock(return_value=ProjectContext(
            project_id="11111111-1111-1111-1111-111111111111",
            content="# selti",
            sections={"stack": ["PostgreSQL 16"]},
            granule_count=1,
            computed_at=datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc),
            stale=False,
        ))
        state = MagicMock()
        state.get_memory_service = AsyncMock(return_value=service)
        return state

    def test_api_alias_same_snapshot(self, ctx_client, mock_state):
        response = ctx_client.get("/api/contexts/selti")

        assert response.status_code == 200
        assert response.json()["sections"]["stack"] == ["PostgreSQL 16"]
        mock_state.get_memory_service.return_value.get_project_context.assert_awaited_once_with(
            "selti", refresh=False
        )

    def test_hook_contract_untouched(self, ctx_client):
        assert ctx_client.get("/context/selti").status_code == 200
