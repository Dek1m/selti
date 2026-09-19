"""Project registry tasks for Celery workers (Фаза 5.1, /api/projects).

Read-path реестра и CRUD-минимум карточек идут через воркер — тот же
единый путь исполнения, что и все операции REST-морды (принцип 3 плана:
никакого прямого доступа к репозиториям из web-процесса).
"""

from typing import Any

from celery import shared_task

from memory_server.exceptions import NotFoundError
from memory_server.logger import get_logger
from memory_server.state import get_state
from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask
from memory_server.tasks.errors import ValidationError

logger = get_logger(__name__)


def _get_repo():
    """ProjectRepository через процессный SeltiState."""
    return run_async(get_state().get_project_repository)


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.project_tasks.list_projects",
    soft_time_limit=60,
    time_limit=90,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def list_projects(self) -> list[dict[str, Any]]:
    """Весь реестр проектов (карточки без стека — список для таблицы UI)."""
    records = run_async(_get_repo().list_all)
    return [record._asdict() for record in records]


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.project_tasks.get_project",
    soft_time_limit=60,
    time_limit=90,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def get_project(self, slug: str) -> dict[str, Any]:
    """Карточка проекта + стек (links/technologies) для карточки UI."""
    if not slug or not slug.strip():
        raise ValidationError("slug cannot be empty")

    card = run_async(_get_repo().fetch_card, slug)
    if card is None:
        raise NotFoundError(slug, message=f"Project not found: '{slug}'")
    return card


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.project_tasks.create_project",
    soft_time_limit=60,
    time_limit=90,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def create_project(
    self,
    slug: str,
    name: str,
    description: str | None = None,
    kind: str = "code",
    status: str = "active",
    local_path: str | None = None,
    repo_url: str | None = None,
    docs_url: str | None = None,
    homepage_url: str | None = None,
    default_branch: str = "main",
    links: list[dict] | None = None,
    technologies: list[dict] | None = None,
) -> dict[str, Any]:
    """Создать проект (UniqueViolationError на дубль slug пробрасывается — REST 409)."""
    if not slug or not slug.strip():
        raise ValidationError("slug cannot be empty")
    if not name or not name.strip():
        raise ValidationError("name cannot be empty")

    return run_async(
        _get_repo().create_card,
        slug=slug,
        name=name,
        description=description,
        kind=kind,
        status=status,
        local_path=local_path,
        repo_url=repo_url,
        docs_url=docs_url,
        homepage_url=homepage_url,
        default_branch=default_branch,
        links=links,
        technologies=technologies,
    )


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.project_tasks.update_project",
    soft_time_limit=60,
    time_limit=90,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def update_project(
    self,
    slug: str,
    name: str | None = None,
    description: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    local_path: str | None = None,
    repo_url: str | None = None,
    docs_url: str | None = None,
    homepage_url: str | None = None,
    default_branch: str | None = None,
    links: list[dict] | None = None,
    technologies: list[dict] | None = None,
) -> dict[str, Any]:
    """Частичное обновление (None-поля не меняются); slug не найден → NotFoundError."""
    if not slug or not slug.strip():
        raise ValidationError("slug cannot be empty")

    card = run_async(
        _get_repo().update_card,
        slug=slug,
        name=name,
        description=description,
        kind=kind,
        status=status,
        local_path=local_path,
        repo_url=repo_url,
        docs_url=docs_url,
        homepage_url=homepage_url,
        default_branch=default_branch,
        links=links,
        technologies=technologies,
    )
    if card is None:
        raise NotFoundError(slug, message=f"Project not found: '{slug}'")
    return card
