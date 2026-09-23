"""Фаза 3 (волна 3): ассоциативное расширение search — strategy="activation"
в СТАРОМ туле memory_search (требование Мастера: новый тул не заводим).

Фаза 1: гибридный RRF-поиск даёт seed-гранулы (топ search_activation_seed_limit).
Фаза 2: ActivationSpreader (тот же PPR-движок, что traverse-activation)
распространяет активацию по живому графу (fetch_activation_edges, ленивые
w_eff, зеркала related_to — в SQL). Фаза 3: seed-хиты + активированные
соседи НЕ из seed'ов (поле activated=true, score = PPR-ранг), общий топ-K =
limit. Default-ветка hybrid — бит-в-бит прежняя: путь и JSON без новых
ключей. Флаг search_activation_enabled (False до приёмки) — внятная ошибка,
НЕ тихий fallback.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.memory.service import MemoryService

SEED_A = "aaaaaaaa-0000-0000-0000-000000000001"
SEED_B = "aaaaaaaa-0000-0000-0000-000000000002"
NEIGHBOR_C = "aaaaaaaa-0000-0000-0000-000000000003"
NEIGHBOR_D = "aaaaaaaa-0000-0000-0000-000000000004"

# Живой граф для PPR: seed'ы связаны с соседями; C ближе к обоим seed'ам
# (двойная связь), D — только к A. Ожидаемый порядок активации: C, D.
GRAPH = [
    (SEED_A, NEIGHBOR_C, 1.0),
    (NEIGHBOR_C, SEED_A, 1.0),
    (SEED_B, NEIGHBOR_C, 1.0),
    (SEED_A, NEIGHBOR_D, 0.5),
]


def _candidate(cid: str, *, rank_dense: int = 0) -> MagicMock:
    """HybridCandidate-мок фазы 1 (RRF-ранги достаточно: MMR без векторов)."""
    moment = datetime.now(timezone.utc)
    from memory_server.memory.search_fusion import HybridCandidate

    return HybridCandidate(
        id=cid, content=f"content-{cid}", metadata={}, namespace="default",
        importance=3, project_id=None, status="asserted",
        created_at=moment, last_accessed_at=moment, frozen=False,
        rank_dense=rank_dense, rank_fts=None, vector=None,
    )


def _card(cid: str) -> dict:
    """Карточка fetch_by_ids — проекция _MEMORY_COLUMNS."""
    now = datetime.now(timezone.utc)
    return {
        "id": cid, "user_id": "u1", "content": f"content-{cid}", "metadata": {},
        "importance": 3, "created_at": now, "updated_at": now, "content_hash": None,
        "project_id": None, "status": "asserted", "confidence": 1.0,
        "valid_from": now, "valid_to": None, "ingested_at": now,
        "namespace": "default", "supersedes": None, "superseded_by": None,
        "frozen": False, "last_accessed_at": None, "access_count": 0,
    }


def activation_service(**cfg) -> tuple[MemoryService, MagicMock]:
    """Сервис с включённым флагом и замоканным репозиторием графа/карточек."""
    overrides = {
        "dedup_enabled": False,
        "hybrid_search_enabled": True,
        "search_activation_enabled": True,
    }
    overrides.update(cfg)
    config = RuntimeConfig(db_values=overrides)
    repo = MagicMock()
    repo.search_hybrid = AsyncMock(return_value=[_candidate(SEED_A, rank_dense=0), _candidate(SEED_B, rank_dense=1)])
    repo.fetch_activation_edges = AsyncMock(return_value=list(GRAPH))
    repo.fetch_by_ids = AsyncMock(return_value=[_card(NEIGHBOR_C), _card(NEIGHBOR_D)])
    repo.bump_access = AsyncMock(return_value=2)
    service = MemoryService(
        repository=repo,
        embedding_provider=MagicMock(),
        namespace_repository=MagicMock(),
        runtime=config,
        project_repository=MagicMock(),
    )
    service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    return service, repo


class TestSearchActivation:
    @pytest.mark.asyncio
    async def test_seed_spread_merge(self):
        """Полный путь: seed'ы гибридного поиска + активированные соседи,
        помеченные activated; общий размер = limit."""
        service, repo = activation_service()

        results = await service.search(query="q", limit=4, strategy="activation")

        assert [r.id for r in results] == [SEED_A, SEED_B, NEIGHBOR_C, NEIGHBOR_D]
        assert [r.activated for r in results] == [False, False, True, True]
        # активированные отсортированы по PPR-рангу (C — двойная связь — выше D)
        assert results[2].score >= results[3].score > 0
        # граф живых рёбер — с ленивыми параметрами decay и зеркалами related_to
        repo.fetch_activation_edges.assert_awaited_once_with(
            0.02, 0.002, None, ["related_to"]
        )
        repo.fetch_by_ids.assert_awaited_once_with([NEIGHBOR_C, NEIGHBOR_D])

    @pytest.mark.asyncio
    async def test_seeds_not_duplicated_as_activated(self):
        """Seed-хиты не попадают в расширения: seed уже в выдаче, PPR-топ
        соседей берётся ТОЛЬКО из не-seed узлов."""
        service, repo = activation_service()

        results = await service.search(query="q", limit=2, strategy="activation")

        # limit = размеру seed-выборки: расширений нет, дублей нет
        assert [r.id for r in results] == [SEED_A, SEED_B]
        assert all(not r.activated for r in results)
        repo.fetch_by_ids.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_seed_limit_capped_by_search_limit(self):
        """limit < seed-лимита конфига: seed'ов не больше limit (общий топ-K)."""
        service, _ = activation_service()

        results = await service.search(query="q", limit=1, strategy="activation")

        assert len(results) == 1
        assert results[0].id == SEED_A

    @pytest.mark.asyncio
    async def test_empty_seeds_return_empty(self):
        """Пустая фаза 1 → пустой ответ без обращения к графу."""
        service, repo = activation_service()
        repo.search_hybrid = AsyncMock(return_value=[])

        results = await service.search(query="q", strategy="activation")

        assert results == []
        repo.fetch_activation_edges.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_closed_granule_race_dropped(self):
        """Активированный сосед закрылся между графом и карточками — молча
        исключается из выдачи (гонка микросекунд)."""
        service, repo = activation_service()
        repo.fetch_by_ids = AsyncMock(return_value=[_card(NEIGHBOR_C)])

        results = await service.search(query="q", limit=4, strategy="activation")

        assert [r.id for r in results] == [SEED_A, SEED_B, NEIGHBOR_C]

    @pytest.mark.asyncio
    async def test_disabled_flag_explicit_error(self):
        """Флаг выключен: внятная ошибка, НЕ тихий fallback на hybrid."""
        service, repo = activation_service(search_activation_enabled=False)

        with pytest.raises(ValueError, match="search_activation_enabled"):
            await service.search(query="q", strategy="activation")
        repo.search_hybrid.assert_not_awaited()
        repo.fetch_activation_edges.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_strategy_rejected(self):
        service, _ = activation_service()
        with pytest.raises(ValueError, match="strategy"):
            await service.search(query="q", strategy="semantic")

    @pytest.mark.asyncio
    async def test_offset_rejected_for_activation(self):
        """Пагинация несовместима с расширением: явный отказ, не молчаливый
        игнор параметра."""
        service, _ = activation_service()
        with pytest.raises(ValueError, match="offset"):
            await service.search(query="q", offset=5, strategy="activation")

    @pytest.mark.asyncio
    async def test_hybrid_default_path_bitwise(self):
        """Дефолт: прежний гибридный путь — выборка гибрида с исходным limit,
        граф активации НЕ читается."""
        service, repo = activation_service()
        repo.search_hybrid = AsyncMock(
            return_value=[_candidate(SEED_A, rank_dense=0)]
        )

        results = await service.search(query="q", limit=10)

        assert len(results) == 1
        assert results[0].activated is False
        assert repo.search_hybrid.await_args.kwargs["prefetch"] == 100
        repo.fetch_activation_edges.assert_not_awaited()
        repo.fetch_by_ids.assert_not_awaited()


class TestSearchActivationPayload:
    """Контракт выдачи таски: помечаются только активированные расширения."""

    def test_payload_marks_only_activated(self, monkeypatch):
        """Активированные несут activated=true, seed'ы — БЕЗ ключа: гибридная
        выдача бит-в-бит прежняя (регрессия формата недопустима)."""
        import memory_server.tasks.memory_tasks as mt
        from memory_server.tasks.memory_tasks import search_memories

        service, _ = activation_service()
        monkeypatch.setattr(mt, "_get_service", lambda: service)

        payloads = search_memories(query="q", limit=4, strategy="activation")

        assert len(payloads) == 4
        for payload, expected in zip(payloads, (False, False, True, True)):
            assert payload.get("activated", False) is expected
        assert "activated" not in payloads[0]
        assert "activated" not in payloads[1]
        assert payloads[2]["activated"] is True

    def test_hybrid_payload_has_no_activated_key(self, monkeypatch):
        """Дефолтный вызов таски: JSON без нового ключа вовсе."""
        import memory_server.tasks.memory_tasks as mt
        from memory_server.tasks.memory_tasks import search_memories

        service, _ = activation_service()
        monkeypatch.setattr(mt, "_get_service", lambda: service)

        payloads = search_memories(query="q")

        assert payloads
        assert all("activated" not in p for p in payloads)
