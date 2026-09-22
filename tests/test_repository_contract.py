"""Контракт полноты фасада MemoryRepository — инварианты интроспекции.

Прод-баг d64ce48 (2026-09-22, нашёл Рэй): service._search_activation и
_traverse_activation звали self.repository.fetch_by_ids — метод есть на
PostgreSQLRepository, но не продублирован делегатом на фасаде. AttributeError
на проде при любом activation-вызове с расширениями. Юнит-тесты этого класса
бага не видят: AsyncMock-репозиторий принимает ЛЮБОЙ атрибут (мок-дыра).
Здесь проверяются реальные классы через AST — рассинхрон «вызывающий ↔
фасад ↔ нижний слой» ловится до деплоя, без БД и без моков.

Инварианты:
  A. Каждое обращение self.repository.<name> в service.py — существующий
     callable на MemoryRepository (фасад полон для вызывающего слоя).
  B. Каждое обращение self.<base>.<name> внутри repository.py — существующий
     callable на соответствующем нижнем классе: pg → PostgreSQLRepository,
     qdrant → QdrantStore, ns_repo → NamespaceRepository (фасад не зовёт
     несуществующее уровнем ниже — зеркальная мок-дыра).
  C. Обратная полнота: каждый публичный метод фасада опирается на pg-слой
     (содержит self.pg.<name> в теле). Фасад устроен как «PG-метаданные +
     Qdrant-вектора», безопорных методов нет; чисто Qdrant/ns-метод будущего
     вносится в NON_PG_FACADE_METHODS осознанно, с обоснованием в ревью.

Ограничение: парсер видит только прямые обращения self.<base>.<name>;
алиасирование (r = self.repository) обходит проверку — в service.py таких
нет (гвардится ревью).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.memory.qdrant_store import QdrantStore
from memory_server.memory.repository import MemoryRepository

_MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory_server" / "memory"

# Публичные методы фасада, сознательно НЕ опирающиеся на pg-слой (инвариант C).
NON_PG_FACADE_METHODS: frozenset[str] = frozenset()


def _self_attr_names(source: str | ast.AST, base: str) -> set[str]:
    """Имена, читаемые как `self.<base>.<name>` (AST-обход, без исполнения)."""
    tree = ast.parse(source) if isinstance(source, str) else source
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
        and node.value.attr == base
    }


def _missing_callables(names: set[str], klass: type) -> set[str]:
    return {
        name
        for name in names
        if not callable(getattr(klass, name, None))
    }


def test_service_repository_calls_exist_on_facade() -> None:
    """Инвариант A: всё, что service зовёт на self.repository, есть на фасаде."""
    source = (_MEMORY_DIR / "service.py").read_text(encoding="utf-8")
    called = _self_attr_names(source, "repository")
    assert called, "service.py не обращается к self.repository — тест протух"
    missing = _missing_callables(called, MemoryRepository)
    assert not missing, (
        "service.py зовёт методы, отсутствующие на MemoryRepository "
        f"(прод-баг d64ce48 — этот тест обязан был его поймать): {sorted(missing)}"
    )


# Инвариант B: (файл, атрибут фасада, нижний класс)
@pytest.mark.parametrize(
    ("base", "lower_class"),
    [
        pytest.param("pg", PostgreSQLRepository, id="facade→pg"),
        pytest.param("qdrant", QdrantStore, id="facade→qdrant"),
        pytest.param("ns_repo", NamespaceRepository, id="facade→ns_repo"),
    ],
)
def test_facade_lower_layer_calls_exist(base: str, lower_class: type) -> None:
    """Инвариант B: фасад не зовёт несуществующее на нижнем слое."""
    source = (_MEMORY_DIR / "repository.py").read_text(encoding="utf-8")
    called = _self_attr_names(source, base)
    assert called, f"repository.py не обращается к self.{base} — тест протух"
    missing = _missing_callables(called, lower_class)
    assert not missing, (
        f"MemoryRepository зовёт self.{base}.<name>, отсутствующие на "
        f"{lower_class.__name__}: {sorted(missing)}"
    )


def test_facade_public_methods_backed_by_pg() -> None:
    """Инвариант C: публичный метод фасада обязан опираться на pg-слой."""
    tree = ast.parse((_MEMORY_DIR / "repository.py").read_text(encoding="utf-8"))
    facade_cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MemoryRepository"
    )
    public_methods = {
        node.name: node
        for node in facade_cls.body
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
    }
    # У фасада ~47 публичных методов: меньше 30 — парсер или структура сломаны
    assert len(public_methods) >= 30, "парсер не видит методы фасада — тест протух"
    unbacked = sorted(
        name
        for name, node in public_methods.items()
        if name not in NON_PG_FACADE_METHODS and not _self_attr_names(node, "pg")
    )
    assert not unbacked, (
        "Публичные методы фасада без опоры на pg-слой (добавить делегата "
        "self.pg.<...> или внести в NON_PG_FACADE_METHODS с обоснованием): "
        f"{unbacked}"
    )
