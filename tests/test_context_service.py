"""Юниты «облачка знаний» (Фаза 6.1): кеш ctx:{slug}, dirty-флаг, пересборка.

FakeRedis повторяет контракт redis.asyncio (get/set/delete/exists) на dict —
BLPOP моста здесь не нужен. Репозиторий и реестр проектов — AsyncMock'и:
проверяем ОРКЕСТРАЦИЮ кеш/таблица/пересчёт, не SQL.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Settings
from memory_server.exceptions import NotFoundError
from memory_server.memory.project_repository import ProjectRecord
from memory_server.memory.service import MemoryService
from memory_server.models import MemoryListResult, MemoryRecord, ProjectContext

SELTI_ID = "11111111-1111-1111-1111-111111111111"
AKAME_ID = "22222222-2222-2222-2222-222222222222"

SELTI = ProjectRecord(
    id=SELTI_ID, slug="selti", name="selti", kind="code",
    status="active", local_path="E:\\Projects\\Python\\selti",
)
AKAME = ProjectRecord(
    id=AKAME_ID, slug="akame", name="akame", kind="code",
    status="active", local_path="E:\\Projects\\Python\\akame",
)


class FakeRedis:
    """Минимальный async-Redis на dict: get/set(+ex)/delete/exists."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.data[key] = value
        self.ttls[key] = ex

    async def delete(self, *keys: str):
        for key in keys:
            self.data.pop(key, None)

    async def exists(self, key: str) -> int:
        return int(key in self.data)


def snapshot_row(**overrides) -> dict:
    """Строка project_contexts — как SELECT_PROJECT_CONTEXT."""
    row = {
        "project_id": SELTI_ID,
        "content": "# selti — облачко знаний",
        "sections": {"decisions": ["ADR-1"]},
        "granule_count": 1,
        "computed_at": None,
    }
    row.update(overrides)
    return row


def granule_rows() -> list[dict]:
    """Ответ хранимки project_context_snapshot: по одной грануле на ns."""
    return [
        {"content": "ADR-9: единый путь через Celery", "namespace": "project_meta",
         "importance": 5, "updated_at": None},
        {"content": "SeltiState — composition root\nс ленивыми синглтонами",
         "namespace": "code_knowledge", "importance": 4, "updated_at": None},
        {"content": "Фаза 5 закрыта", "namespace": "dialogue_insights",
         "importance": 3, "updated_at": None},
        {"content": "PG 16 на ai.atom.ui", "namespace": "infrastructure",
         "importance": 4, "updated_at": None},
    ]


def stack_payload() -> dict:
    return {
        "technologies": [
            {"name": "PostgreSQL", "category": "db", "version": "16", "purpose": "основная БД"},
            {"name": "Python", "category": "lang", "version": None, "purpose": None},
        ],
        "links": [
            {"link_type": "repo", "url": "https://github.com/Dek1m/selti", "title": None},
        ],
    }


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def repository():
    repo = MagicMock()
    repo.upsert_project_context = AsyncMock(
        return_value={"project_id": SELTI_ID, "computed_at": None}
    )
    repo.get_project_context = AsyncMock(return_value=None)
    repo.insert = AsyncMock(return_value="granule-id")
    repo.get_by_id = AsyncMock(return_value=None)
    repo.update = AsyncMock(return_value=None)

    def _list_items(namespace_id: str):
        """Кандидаты секции: granule_rows() → MemoryRecord (uid = namespace_id)."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        uid = namespace_id.removeprefix("ns-")
        items = [
            MemoryRecord(
                id=f"id-{uid}-{i}",
                user_id="akame",
                content=r["content"],
                namespace=uid,
                namespace_id=namespace_id,
                importance=r["importance"],
                created_at=now,
                updated_at=now,
                frozen=False,
            )
            for i, r in enumerate(granule_rows())
            if r["namespace"] == uid
        ]
        return MemoryListResult(items=items, total=len(items))

    repo.list = AsyncMock(
        side_effect=lambda **kw: _list_items(kw["namespace_id"])
    )
    return repo


@pytest.fixture
def project_repo():
    repo = MagicMock()
    repo.get_by_slug = AsyncMock(side_effect=lambda slug: SELTI if slug == "selti" else None)
    repo.get_by_id = AsyncMock(
        side_effect=lambda pid: SELTI if pid == SELTI_ID else None
    )
    repo.list_all = AsyncMock(return_value=[SELTI, AKAME])
    repo.fetch_stack = AsyncMock(return_value=stack_payload())
    repo.resolve_id = AsyncMock(side_effect=lambda key: key)
    return repo


@pytest.fixture
def context_service(repository, project_repo, fake_redis):
    async def provider():
        return fake_redis

    service = MemoryService(
        repository=repository,
        embedding_provider=MagicMock(embed=AsyncMock(return_value=[0.1])),
        namespace_repository=MagicMock(
            get_or_create=AsyncMock(return_value=MagicMock(id="ns-id")),
            get_by_uid=AsyncMock(side_effect=lambda uid: MagicMock(id=f"ns-{uid}")),
        ),
        config=Settings(dedup_enabled=False, context_cache_ttl=3600),
        project_repository=project_repo,
        redis_provider=provider,
    )
    return service


def ctx_key(slug: str) -> str:
    return f"ctx:{slug}"


def dirty_key(slug: str) -> str:
    return f"ctx:{slug}:dirty"


class TestGetProjectContext:
    """Асинхронные сценарии: asyncio.run на каждый кейс (стиль без плагина)."""

    def test_cache_hit_skips_table(self, context_service, repository, fake_redis):
        import asyncio

        snapshot = ProjectContext(project_id=SELTI_ID, content="cached", granule_count=1)
        fake_redis.data[ctx_key("selti")] = snapshot.model_dump_json()

        result = asyncio.run(context_service.get_project_context("selti"))

        assert result.content == "cached"
        assert result.stale is False
        repository.get_project_context.assert_not_awaited()

    def test_cache_hit_with_dirty_flag_marks_stale(self, context_service, fake_redis):
        """dirty + не refresh → stale-снапшот, но честно помечен stale=true."""
        import asyncio

        snapshot = ProjectContext(project_id=SELTI_ID, content="cached", granule_count=1)
        fake_redis.data[ctx_key("selti")] = snapshot.model_dump_json()
        fake_redis.data[dirty_key("selti")] = "1"

        result = asyncio.run(context_service.get_project_context("selti"))

        assert result.stale is True
        assert result.content == "cached"

    def test_cache_miss_falls_to_table_and_backfills_cache(self, context_service, repository, fake_redis):
        """Кеш пуст → таблица → снапшот прогрева кеша ctx:{slug}."""
        import asyncio

        repository.get_project_context.return_value = snapshot_row(content="from table")

        result = asyncio.run(context_service.get_project_context("selti"))

        assert result.content == "from table"
        assert ctx_key("selti") in fake_redis.data
        repository.upsert_project_context.assert_not_awaited()

    def test_no_snapshot_no_cache_rebuilds(self, context_service, repository, fake_redis):
        """Нигде нет снапшота → пересчёт из хранимки + UPSERT + кеш."""
        import asyncio

        result = asyncio.run(context_service.get_project_context("selti"))

        repository.upsert_project_context.assert_awaited_once()
        assert result.project_id == SELTI_ID
        assert ctx_key("selti") in fake_redis.data

    def test_refresh_ignores_cache(self, context_service, repository, fake_redis):
        """refresh=true пересчитывает, даже если кеш валиден."""
        import asyncio

        snapshot = ProjectContext(project_id=SELTI_ID, content="cached")
        fake_redis.data[ctx_key("selti")] = snapshot.model_dump_json()

        result = asyncio.run(context_service.get_project_context("selti", refresh=True))

        repository.upsert_project_context.assert_awaited_once()
        assert result.content != "cached"

    def test_redis_down_degrades_to_table(self, context_service, repository):
        """Redis-провайдер падает → путь жив на таблице (деградация, не ошибка)."""
        import asyncio

        async def broken_provider():
            raise ConnectionError("redis down")

        context_service.redis_provider = broken_provider
        repository.get_project_context.return_value = snapshot_row(content="table only")

        result = asyncio.run(context_service.get_project_context("selti"))

        assert result.content == "table only"
        assert result.stale is False

    def test_uuid_input_resolves_via_by_id(self, context_service, repository, fake_redis):
        """Вход UUID → резолв get_by_id → тот же кеш-ключ по slug."""
        import asyncio

        snapshot = ProjectContext(project_id=SELTI_ID, content="by uuid")
        fake_redis.data[ctx_key("selti")] = snapshot.model_dump_json()

        result = asyncio.run(context_service.get_project_context(SELTI_ID))

        assert result.content == "by uuid"

    def test_unknown_slug_raises_not_found(self, context_service):
        import asyncio

        with pytest.raises(NotFoundError):
            asyncio.run(context_service.get_project_context("no-such-project"))


class TestRebuild:
    def test_sections_structure_and_stack(self, context_service, repository, fake_redis):
        """Хранимка + стек → секции {stack, decisions, code, insights, infra}."""
        import asyncio

        result = asyncio.run(context_service.get_project_context("selti", refresh=True))

        assert set(result.sections) == {"stack", "decisions", "code", "insights", "infra"}
        # Секция stack из project_technologies + project_links
        assert "PostgreSQL 16 — основная БД" in result.sections["stack"]
        assert "Python" in result.sections["stack"]
        assert any(line.startswith("repo: ") for line in result.sections["stack"])
        # Гранулы разложены по каноническим секциям, а не по namespace-uid
        assert result.sections["decisions"] == ["ADR-9: единый путь через Celery"]
        assert result.granule_count == 4  # стек не считается гранулами

    def test_content_is_bounded_markdown_list(self, context_service):
        """content ≤100 строк; многострочная гранула → однострочный тезис."""
        import asyncio

        result = asyncio.run(context_service.get_project_context("selti", refresh=True))

        lines = result.content.splitlines()
        assert len(lines) <= 100
        assert lines[0].startswith("# selti")
        assert "## Стек" in result.content
        # Переносы исходной гранулы схлопнуты в одну строку списка
        assert "- SeltiState — composition root с ленивыми синглтонами" in lines

    def test_rebuild_clears_dirty_and_caches(self, context_service, repository, fake_redis):
        """После пересчёта dirty снят, снапшот в кеше с TTL из конфига."""
        import asyncio

        fake_redis.data[dirty_key("selti")] = "1"

        result = asyncio.run(context_service.get_project_context("selti", refresh=True))

        assert dirty_key("selti") not in fake_redis.data
        assert fake_redis.ttls[ctx_key("selti")] == 3600
        assert result.stale is False

    def test_empty_project_still_renders_stack(self, context_service, repository):
        """Гранул нет — снапшот из одного стека (облачко не пустое)."""
        import asyncio

        repository.list = AsyncMock(
            side_effect=lambda **kw: MemoryListResult(items=[], total=0)
        )

        result = asyncio.run(context_service.get_project_context("selti", refresh=True))

        assert result.granule_count == 0
        assert result.sections["stack"]
        assert "## Стек" in result.content


class TestDirtyFlag:
    def test_store_with_project_marks_dirty(self, context_service, repository, fake_redis):
        """store с project_id → Redis SET ctx:{slug}:dirty (TTL = context_cache_ttl)."""
        import asyncio

        record = MagicMock()
        record.project_id = SELTI_ID
        repository.get_by_id.return_value = record
        repository.insert.return_value = "new-id"

        asyncio.run(context_service.store("новый факт", user_id="u1", project_id="selti"))

        assert dirty_key("selti") in fake_redis.data
        assert fake_redis.ttls[dirty_key("selti")] == 3600

    def test_store_without_project_no_dirty(self, context_service, fake_redis):
        """Глобальный слой (project_id=None) облачко не пачкает."""
        import asyncio

        record = MagicMock()
        record.project_id = None
        repository = context_service.repository
        repository.get_by_id.return_value = record
        repository.insert.return_value = "new-id"

        asyncio.run(context_service.store("глобальный факт", user_id="u1"))

        assert not [k for k in fake_redis.data if k.endswith(":dirty")]

    def test_retract_marks_dirty(self, context_service, repository, fake_redis):
        import asyncio

        record = MagicMock()
        record.project_id = SELTI_ID
        repository.get_by_id.return_value = record
        repository.archive = AsyncMock(return_value=True)

        asyncio.run(context_service.retract("granule-id"))

        assert dirty_key("selti") in fake_redis.data


class TestRebuildDirtyContexts:
    def test_only_dirty_projects_rebuilt(self, context_service, repository, project_repo, fake_redis):
        """Beat-перебор: dirty только у selti → пересобран только он."""
        import asyncio

        fake_redis.data[dirty_key("selti")] = "1"

        report = asyncio.run(context_service.rebuild_dirty_contexts())

        assert report == {"scanned": 2, "rebuilt": ["selti"]}
        assert dirty_key("selti") not in fake_redis.data
        # akame не пересобран: upsert ровно один
        repository.upsert_project_context.assert_awaited_once()

    def test_no_redis_no_rebuilds(self, context_service):
        import asyncio

        async def broken_provider():
            raise ConnectionError("redis down")

        context_service.redis_provider = broken_provider

        report = asyncio.run(context_service.rebuild_dirty_contexts())

        assert report == {"scanned": 2, "rebuilt": []}


class TestCacheSerialization:
    def test_cached_context_roundtrip(self, context_service, fake_redis):
        """Кеш хранит валидный JSON ProjectContext (model_dump_json)."""
        import asyncio

        asyncio.run(context_service.get_project_context("selti", refresh=True))
        raw = fake_redis.data[ctx_key("selti")]
        parsed = json.loads(raw)
        assert parsed["project_id"] == SELTI_ID
        assert parsed["sections"]["stack"]


class TestRecencyRanking:
    """Recency-ранжирование облачка (решение Мастера 19.09): свежее важнее."""

    def test_fresh_beats_ancient(self, context_service, repository):
        """Важность 5 шестидесятидневная тонет под важностью 3 вчерашней."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)

        def item(content: str, importance: int, age_days: int) -> MemoryRecord:
            return MemoryRecord(
                id=f"id-{content}", user_id="akame", content=content,
                namespace="project_meta", namespace_id="ns-project_meta",
                importance=importance, created_at=now,
                updated_at=now - timedelta(days=age_days), frozen=False,
            )

        repository.list = AsyncMock(side_effect=lambda **kw: MemoryListResult(
            items=[item("древняя важность-5", 5, 60), item("свежая важность-3", 3, 1)],
            total=2,
        ))

        result = asyncio.run(context_service.get_project_context("selti", refresh=True))

        decisions = result.sections["decisions"]
        assert decisions.index("свежая важность-3") < decisions.index("древняя важность-5")

    def test_teaser_truncated_on_sentence(self, context_service, repository):
        """Длинный тезис обрезается на границе предложения с «…»."""
        import asyncio
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        long_text = "Первое предложение. " + "Второе очень длинное предложение " * 20
        repository.list = AsyncMock(side_effect=lambda **kw: MemoryListResult(
            items=[MemoryRecord(
                id="id-1", user_id="u", content=long_text,
                namespace="project_meta", namespace_id="ns-project_meta",
                importance=3, created_at=now, updated_at=now, frozen=False,
            )], total=1,
        ))

        result = asyncio.run(context_service.get_project_context("selti", refresh=True))

        teaser = result.sections["decisions"][0]
        assert len(teaser) <= 301 and teaser.endswith(("…", "."))
