"""Юнит-тесты LinkType-валидации RelationCreate (memory_server/models.py).

link_type валидируется Literal'ом LinkType: недопустимый тип даёт pydantic
ValidationError со списком разрешённых значений ещё на входе (не PG CHECK
violation в глубине воркера). Источник истины допустимых типов — сам Literal.
"""
from typing import get_args

import pytest
from pydantic import ValidationError

from memory_server.models import LinkType, RelationCreate

VALID_LINK_TYPES = get_args(LinkType)


def test_relation_create_supersedes_valid():
    rel = RelationCreate(source_id="src", target_id="tgt", link_type="supersedes")
    assert rel.link_type == "supersedes"


def test_relation_create_default_link_type():
    rel = RelationCreate(source_id="src", target_id="tgt")
    assert rel.link_type == "related_to"


def test_relation_create_invalid_link_type_raises_with_enum():
    with pytest.raises(ValidationError) as exc_info:
        RelationCreate(source_id="src", target_id="tgt", link_type="bogus")

    msg = str(exc_info.value)
    # Сообщение pydantic перечисляет все допустимые Literal-значения
    for t in VALID_LINK_TYPES:
        assert repr(t) in msg or f"'{t}'" in msg, f"допустимый тип {t!r} не перечислен в ошибке"


def test_all_literal_types_present():
    """Защита от случайной деградации набора: все объявленные типы отслеживаются."""
    assert len(VALID_LINK_TYPES) == 32
    assert "supersedes" in VALID_LINK_TYPES
    assert "related_to" in VALID_LINK_TYPES


@pytest.mark.parametrize("link_type", VALID_LINK_TYPES)
def test_all_link_types_valid(link_type):
    rel = RelationCreate(source_id="src", target_id="tgt", link_type=link_type)
    assert rel.link_type == link_type