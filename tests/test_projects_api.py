"""POST /projects/register (ADR-018): ветки upsert, X-SELTI-KEY, нормализация.

Стиль test_context_api.py: тестовое FastAPI-приложение с реальным роутером
api/projects.py; SeltiState подменён моком — пул из conftest.mock_pool,
таблица projects — side_effect'ы AsyncMock'ов соединения (SQL не проверяем).
"""

from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory_server.api.projects import normalize_repo_url
from memory_server.config import settings

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
OLD_REPO = "https://github.com/Dek1m/albedo-old"
ALBEDO_REPO = "https://github.com/Dek1m/albedo"


def project_row(**overrides) -> dict:
    """Строка projects — проекция _REGISTER_COLUMNS роутера."""
    row = {
        "id": PROJECT_ID,
        "slug": "albedo",
        "local_path": "E:\\Projects\\Python\\albedo",
        "repo_url": ALBEDO_REPO,
    }
    row.update(overrides)
    return row


def register_body(**overrides) -> dict:
    """Тело запроса плагина (ADR-018, решение A) — repo_url с .git."""
    body = {
        "slug": "albedo",
        "name": "albedo",
        "kind": "code",
        "local_path": "E:\\Projects\\Python\\albedo",
        "repo_url": ALBEDO_REPO + ".git",
    }
    body.update(overrides)
    return body


@pytest.fixture
def conn(mock_pool):
    """Соединение из conftest.mock_pool — AsyncMock-методы настраивает тест."""
    return mock_pool.acquire.return_value.__aenter__.return_value


@pytest.fixture
def api_client(mock_pool, conn, monkeypatch):
    """Тестовое приложение с реальным projects-роутером; state подменён."""
    import memory_server.api.projects as projects_api

    state = MagicMock()
    state.get_pool = AsyncMock(return_value=mock_pool)
    monkeypatch.setattr(projects_api, "get_state", lambda: state)
    test_app = FastAPI()
    test_app.include_router(projects_api.router)
    with TestClient(test_app) as client:
        yield client


class TestNormalizeRepoUrl:
    def test_strips_git_suffix_and_trailing_slash(self):
        assert normalize_repo_url("https://github.com/Dek1m/albedo.git") == ALBEDO_REPO
        assert normalize_repo_url("https://github.com/Dek1m/albedo/") == ALBEDO_REPO
        assert normalize_repo_url("https://github.com/Dek1m/albedo.git/") == ALBEDO_REPO

    def test_whitespace_is_trimmed(self):
        assert normalize_repo_url("  https://github.com/Dek1m/albedo  ") == ALBEDO_REPO

    def test_clean_url_untouched(self):
        assert normalize_repo_url(ALBEDO_REPO) == ALBEDO_REPO

    def test_scp_like_ssh_canonicalized_to_https(self):
        assert normalize_repo_url("git@github.com:Dek1m/selti.git") == "https://github.com/Dek1m/selti"
        assert normalize_repo_url("git@github.com:Dek1m/selti") == "https://github.com/Dek1m/selti"
        assert normalize_repo_url("git@github.com:/Dek1m/selti.git") == "https://github.com/Dek1m/selti"

    def test_ssh_url_canonicalized_to_https(self):
        assert normalize_repo_url("ssh://git@github.com/Dek1m/selti.git") == "https://github.com/Dek1m/selti"
        assert normalize_repo_url("ssh://git@github.com:22/Dek1m/selti.git") == "https://github.com/Dek1m/selti"
        assert normalize_repo_url("git://github.com/Dek1m/selti.git") == "https://github.com/Dek1m/selti"

    def test_https_host_lowercased_path_kept(self):
        assert normalize_repo_url("git@GitHub.com:Dek1m/Repo.git") == "https://github.com/Dek1m/Repo"
        assert normalize_repo_url("https://GitHub.com/Dek1m/Repo.git") == "https://github.com/Dek1m/Repo"

    def test_windows_drive_path_is_not_a_host(self):
        assert normalize_repo_url("E:/Projects/Python/selti") == "E:/Projects/Python/selti"

    def test_empty_or_none_is_free_for_binding(self):
        """NULL-семантика ADR: пустая строка = свободен для привязки, как NULL."""
        assert normalize_repo_url(None) is None
        assert normalize_repo_url("") is None
        assert normalize_repo_url("   ") is None


class TestRegisterCreated:
    def test_free_slug_inserts_and_returns_201(self, api_client, conn):
        """Slug свободен → INSERT → 201 created с id; repo_url пишется без .git."""
        conn.fetchrow = AsyncMock(side_effect=[None, None])
        conn.fetchval = AsyncMock(return_value="new-uuid")

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 201
        assert response.json() == {
            "status": "created", "slug": "albedo", "id": "new-uuid",
        }
        args = conn.fetchval.await_args.args
        assert args[0].endswith("RETURNING id::text")
        assert args[1:] == ("albedo", "albedo", "code", "E:\\Projects\\Python\\albedo", ALBEDO_REPO)

    def test_slug_frees_path_rebinds_to_owner(self, api_client, conn):
        """Slug свободен, но папка уже под другим slug → path_updated без дубля."""
        conn.fetchrow = AsyncMock(side_effect=[None, project_row(slug="albedo-old")])
        conn.execute = AsyncMock()

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 200
        assert response.json() == {"status": "path_updated", "slug": "albedo-old"}
        conn.fetchval.assert_not_awaited()  # INSERT не выполнялся
        args = conn.execute.await_args.args
        assert "SET repo_url" in args[0]  # перепривязка repo_url владельцу пути


class TestRegisterMatched:
    def test_same_slug_same_repo_noop_keeps_updated_at(self, api_client, conn):
        """Повторный POST по паре slug+repo_url → matched, БД не тронута."""
        conn.fetchrow = AsyncMock(side_effect=[project_row()])

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 200
        assert response.json() == {"status": "matched", "slug": "albedo"}
        conn.execute.assert_not_awaited()  # no-op: updated_at не дёргается
        conn.fetchval.assert_not_awaited()

    def test_same_repo_moved_path_updates_local_path(self, api_client, conn):
        """Тот же repo_url, папка переехала → path_updated, UPDATE local_path."""
        conn.fetchrow = AsyncMock(side_effect=[project_row(local_path="D:\\Old\\albedo")])
        conn.execute = AsyncMock()

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 200
        assert response.json() == {"status": "path_updated", "slug": "albedo"}
        args = conn.execute.await_args.args
        assert "local_path" in args[0]
        assert args[2] == "E:\\Projects\\Python\\albedo"

    def test_null_repo_binds_request_repo(self, api_client, conn):
        """Случай albedo: slug занят при repo_url IS NULL → matched + привязка."""
        conn.fetchrow = AsyncMock(side_effect=[project_row(repo_url=None)])
        conn.execute = AsyncMock()

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 200
        assert response.json() == {"status": "matched", "slug": "albedo"}
        args = conn.execute.await_args.args
        assert args[3] == ALBEDO_REPO  # repo_url записан


class TestRegisterConflict:
    def test_slug_taken_by_other_repo_409(self, api_client, conn):
        """Slug занят другим repo_url, путь свободен → 409 без записи."""
        conn.fetchrow = AsyncMock(side_effect=[project_row(repo_url=OLD_REPO), None])

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 409
        assert response.json() == {"status": "slug_conflict", "slug": "albedo"}
        conn.execute.assert_not_awaited()
        conn.fetchval.assert_not_awaited()

    def test_other_repo_but_path_owned_by_other_slug_rebinds(self, api_client, conn):
        """Порядок ADR: путь уже под другим slug → перепривязка раньше 409."""
        conn.fetchrow = AsyncMock(
            side_effect=[project_row(repo_url=OLD_REPO), project_row(slug="albedo-old")]
        )
        conn.execute = AsyncMock()

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 200
        assert response.json() == {"status": "path_updated", "slug": "albedo-old"}
        conn.fetchval.assert_not_awaited()

    def test_parallel_post_race_maps_to_409(self, api_client, conn):
        """Гонка двух POST одним slug: unique violation → slug_conflict, не 500."""
        conn.fetchrow = AsyncMock(side_effect=[None, None])
        conn.fetchval = AsyncMock(side_effect=asyncpg.exceptions.UniqueViolationError("dup"))

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 409
        assert response.json()["status"] == "slug_conflict"


class TestApiKeyGate:
    @pytest.fixture(autouse=True)
    def protected(self, monkeypatch):
        monkeypatch.setattr(settings, "selti_api_key", "s3cret")

    def test_missing_key_401(self, api_client):
        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 401
        assert response.json() == {"detail": "invalid or missing X-SELTI-KEY"}

    def test_wrong_key_401(self, api_client):
        response = api_client.post(
            "/projects/register", json=register_body(), headers={"x-selti-key": "wrong"}
        )

        assert response.status_code == 401

    def test_non_ascii_key_401_not_500(self, api_client):
        """Не-ASCII в заголовке не должен ронять compare_digest в 500 (баг приёмки).

        Реальный путь: сырые UTF-8 байты заголовка ASGI декодирует как latin-1 —
        поэтому шлём bytes, как это делает настоящий HTTP-клиент.
        """
        response = api_client.post(
            "/projects/register",
            json=register_body(),
            headers={"x-selti-key": "kэт".encode("utf-8")},
        )

        assert response.status_code == 401

    def test_correct_key_passes(self, api_client, conn):
        conn.fetchrow = AsyncMock(side_effect=[project_row()])

        response = api_client.post(
            "/projects/register", json=register_body(), headers={"x-selti-key": "s3cret"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "matched"

    def test_empty_server_key_leaves_endpoint_open(self, api_client, monkeypatch, conn):
        """Пустой SELTI_API_KEY → эндпоинт открыт (совместимость, решение B)."""
        monkeypatch.setattr(settings, "selti_api_key", "")
        conn.fetchrow = AsyncMock(side_effect=[project_row()])

        response = api_client.post("/projects/register", json=register_body())

        assert response.status_code == 200


class TestValidation:
    def test_unknown_kind_422(self, api_client):
        response = api_client.post("/projects/register", json=register_body(kind="web"))

        assert response.status_code == 422

    def test_missing_slug_422(self, api_client):
        body = register_body()
        del body["slug"]

        response = api_client.post("/projects/register", json=body)

        assert response.status_code == 422
