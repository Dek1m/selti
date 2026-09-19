"""REST облачка для ZCode-хука (Фаза 6.3): /context/{slug}, /projects + beat.

Стиль test_health.py: тестовое FastAPI-приложение с реальным роутером
api/context.py; SeltiState подменён моком — get_state не создаёт pool.
Beat: rebuild_contexts в расписании + eager-вызов с подменой сервиса.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory_server.exceptions import NotFoundError
from memory_server.memory.project_repository import ProjectRecord
from memory_server.models import ProjectContext

SELTI_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def mock_state():
    """SeltiState-мок: сервис с get_project_context + репозиторий реестра."""
    service = MagicMock()
    service.get_project_context = AsyncMock(
        return_value=ProjectContext(
            project_id=SELTI_ID,
            content="# selti — облачко знаний\n## Стек\n- PostgreSQL 16",
            sections={"stack": ["PostgreSQL 16"]},
            granule_count=1,
            computed_at=datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc),
            stale=False,
        )
    )
    project_repo = MagicMock()
    project_repo.list_all = AsyncMock(
        return_value=[
            ProjectRecord(
                id=SELTI_ID, slug="selti", name="selti", kind="code",
                status="active", local_path="E:\\Projects\\Python\\selti",
            )
        ]
    )
    state = MagicMock()
    state.get_memory_service = AsyncMock(return_value=service)
    state.get_project_repository = AsyncMock(return_value=project_repo)
    return state


@pytest.fixture
def api_client(mock_state, monkeypatch):
    """Тестовое приложение с реальным context-роутером; state подменён."""
    import memory_server.api.context as ctx_api

    monkeypatch.setattr(ctx_api, "get_state", lambda: mock_state)
    test_app = FastAPI()
    test_app.include_router(ctx_api.router)
    with TestClient(test_app) as client:
        yield client


class TestContextEndpoint:
    def test_returns_snapshot_json(self, api_client):
        """GET /context/{slug} → 200, тот же JSON, что тул memory_context."""
        response = api_client.get("/context/selti")

        assert response.status_code == 200
        data = response.json()
        assert data["project_id"] == SELTI_ID
        assert data["sections"]["stack"] == ["PostgreSQL 16"]
        assert data["stale"] is False
        assert data["computed_at"]

    def test_unknown_project_404(self, api_client, mock_state):
        """Неизвестный slug → 404 (хук деградирует молча)."""
        mock_state.get_memory_service.return_value.get_project_context = AsyncMock(
            side_effect=NotFoundError("nope", message="Project not found: 'nope'")
        )

        response = api_client.get("/context/nope")

        assert response.status_code == 404
        assert "detail" in response.json()

    def test_stale_flag_passthrough(self, api_client, mock_state):
        """stale=true снапшота виден в REST-ответе (честность хука)."""
        service = mock_state.get_memory_service.return_value
        service.get_project_context.return_value = ProjectContext(
            project_id=SELTI_ID, stale=True,
        )

        response = api_client.get("/context/selti")

        assert response.json()["stale"] is True


class TestDigestEndpoint:
    def test_digest_is_sha256_of_content_and_sections(self, api_client):
        """GET /context/{slug}/digest → sha256 контента + секций (хеш-протокол)."""
        import hashlib

        response = api_client.get("/context/selti/digest")

        assert response.status_code == 200
        data = response.json()
        expected_content = hashlib.sha256(
            "# selti — облачко знаний\n## Стек\n- PostgreSQL 16".encode()
        ).hexdigest()
        expected_stack = hashlib.sha256(b"PostgreSQL 16").hexdigest()
        assert data["digest"] == expected_content
        assert data["sections"]["stack"] == expected_stack
        assert data["stale"] is False
        assert data["computed_at"]

    def test_digest_stable_for_same_content(self, api_client):
        """Тот же контент → тот же digest (детерминизм content-addressed)."""
        first = api_client.get("/context/selti/digest").json()["digest"]
        second = api_client.get("/context/selti/digest").json()["digest"]
        assert first == second

    def test_digest_unknown_project_404(self, api_client, mock_state):
        """Неизвестный slug → 404 на digest тоже (хук молчит)."""
        mock_state.get_memory_service.return_value.get_project_context = AsyncMock(
            side_effect=NotFoundError("nope", message="Project not found: 'nope'")
        )

        response = api_client.get("/context/nope/digest")

        assert response.status_code == 404


class TestProjectsEndpoint:
    def test_lists_registry_with_local_path(self, api_client):
        """/projects: slug+name+local_path — диагностике хука для матча."""
        response = api_client.get("/projects")

        assert response.status_code == 200
        projects = response.json()["projects"]
        assert projects[0]["slug"] == "selti"
        assert projects[0]["local_path"] == "E:\\Projects\\Python\\selti"
        assert projects[0]["kind"] == "code"


class TestBeatSchedule:
    def test_rebuild_contexts_scheduled_hourly(self):
        """beat: rebuild-contexts ежечасно, имя задачи из lifecycle_tasks."""
        from memory_server.celery_app import app

        entry = app.conf.beat_schedule.get("rebuild-contexts")
        assert entry is not None, "rebuild-contexts отсутствует в beat_schedule"
        assert (
            entry["task"]
            == "memory_server.tasks.lifecycle_tasks.rebuild_contexts"
        )
        assert entry["schedule"] == 3600.0

    def test_rebuild_contexts_task_runs(self, monkeypatch):
        """Eager-вызов: перебор реестра → пересборка dirty → отчёт.

        apply() (не delay()) — инлайн без брокера/бэкенда: полный набор
        тестов поднимает и гасит свой Redis, PubSub-ожидание .delay().get()
        ловит ConnectionReset на границе фикстур.
        """
        from memory_server.tasks import lifecycle_tasks

        service = MagicMock()
        service.rebuild_dirty_contexts = AsyncMock(
            return_value={"scanned": 2, "rebuilt": ["selti"]}
        )
        monkeypatch.setattr(lifecycle_tasks, "_get_service", lambda: service)

        result = lifecycle_tasks.rebuild_contexts.apply().get()

        assert result == {"scanned": 2, "rebuilt": ["selti"]}
