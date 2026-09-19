"""Project registry tasks (Фаза 5.1): list/get/create/update через воркер.

Стиль test_celery_tasks.py: tasks вызываются напрямую (task_always_eager
из conftest), репозиторий подменён моком на уровне _get_repo.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_server.exceptions import NotFoundError
from memory_server.tasks.errors import ValidationError


CARD = {
    "id": "11111111-1111-1111-1111-111111111111",
    "slug": "selti",
    "name": "selti",
    "description": None,
    "kind": "code",
    "status": "active",
    "local_path": None,
    "repo_url": None,
    "docs_url": None,
    "homepage_url": None,
    "default_branch": "main",
    "created_at": "2026-09-01T10:00:00+00:00",
    "updated_at": "2026-09-18T10:00:00+00:00",
    "links": [],
    "technologies": [],
}


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[])
    repo.fetch_card = AsyncMock(return_value=dict(CARD))
    repo.create_card = AsyncMock(return_value=dict(CARD, slug="gera", name="gera"))
    repo.update_card = AsyncMock(return_value=dict(CARD, description="updated"))
    return repo


class TestListProjects:
    def test_returns_records_as_dicts(self, mock_repo):
        from memory_server.memory.project_repository import ProjectRecord
        from memory_server.tasks.project_tasks import list_projects

        mock_repo.list_all = AsyncMock(return_value=[
            ProjectRecord(
                id=CARD["id"], slug="selti", name="selti", kind="code", status="active"
            )
        ])

        with patch("memory_server.tasks.project_tasks._get_repo", return_value=mock_repo):
            result = list_projects()

        assert result[0]["slug"] == "selti"
        assert result[0]["description"] is None


class TestGetProject:
    def test_happy_path(self, mock_repo):
        from memory_server.tasks.project_tasks import get_project

        with patch("memory_server.tasks.project_tasks._get_repo", return_value=mock_repo):
            card = get_project(slug="selti")

        mock_repo.fetch_card.assert_awaited_once_with("selti")
        assert card["slug"] == "selti"
        # Даты — ISO-строки (Celery JSON-сериализация)
        assert isinstance(card["created_at"], str)

    def test_unknown_slug_raises_not_found(self, mock_repo):
        from memory_server.tasks.project_tasks import get_project

        mock_repo.fetch_card = AsyncMock(return_value=None)
        with patch("memory_server.tasks.project_tasks._get_repo", return_value=mock_repo):
            with pytest.raises(NotFoundError):
                get_project(slug="nope")

    def test_empty_slug_raises_validation(self, mock_repo):
        from memory_server.tasks.project_tasks import get_project

        with patch("memory_server.tasks.project_tasks._get_repo", return_value=mock_repo):
            with pytest.raises(ValidationError):
                get_project(slug="  ")


class TestCreateProject:
    def test_passes_card_fields_to_repo(self, mock_repo):
        from memory_server.tasks.project_tasks import create_project

        links = [{"link_type": "repo", "url": "https://github.com/Dek1m/gera", "title": None}]
        technologies = [{"name": "Python", "category": None, "docs_url": None,
                         "version": "3.12", "purpose": None}]

        with patch("memory_server.tasks.project_tasks._get_repo", return_value=mock_repo):
            card = create_project(
                slug="gera", name="gera", links=links, technologies=technologies
            )

        assert card["slug"] == "gera"
        kwargs = mock_repo.create_card.await_args.kwargs
        assert kwargs["links"][0]["url"] == links[0]["url"]
        assert kwargs["technologies"][0]["version"] == "3.12"

    def test_empty_slug_raises_validation(self, mock_repo):
        from memory_server.tasks.project_tasks import create_project

        with patch("memory_server.tasks.project_tasks._get_repo", return_value=mock_repo):
            with pytest.raises(ValidationError):
                create_project(slug="", name="x")


class TestUpdateProject:
    def test_passes_partial_fields(self, mock_repo):
        from memory_server.tasks.project_tasks import update_project

        with patch("memory_server.tasks.project_tasks._get_repo", return_value=mock_repo):
            card = update_project(slug="selti", description="updated")

        assert card["description"] == "updated"
        kwargs = mock_repo.update_card.await_args.kwargs
        assert kwargs["name"] is None  # None-поля не меняются (PATCH-семантика)

    def test_unknown_slug_raises_not_found(self, mock_repo):
        from memory_server.tasks.project_tasks import update_project

        mock_repo.update_card = AsyncMock(return_value=None)
        with patch("memory_server.tasks.project_tasks._get_repo", return_value=mock_repo):
            with pytest.raises(NotFoundError):
                update_project(slug="nope", name="x")
