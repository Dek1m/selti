"""SQL-инварианты микроконтрактов графа (вердикты Эны 23.09, приёмка Ф1/Ф2).

Фиксируют контракт на ТЕКСТАХ SQL (миграции + queries.py) — БД в юнитах
нет, но регрессия ловится статически:

  (а) никакой боевой путь не сортирует рёбра по весу/w_eff:
      traverse — ORDER BY rel_id, relations — ORDER BY created_at DESC;
  (б) PPR-основа — w_eff (SELECT_ACTIVATION_EDGES выставляет AS w_eff);
  (в) w_eff наружу не отдаётся: JSONB-выдающие SELECT не содержат w_eff
      в SELECT-списке; единственная колонка AS w_eff — внутренняя выборка
      PPR, её потребитель — только fetch_activation_edges → CSR.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.memory.activation import ActivationSpreader
from tests.test_traverse_activation import GRAPH6, ppr_reference

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = PROJECT_ROOT / "migrations"

# CREATE ... AS $$ тело $$; — берём ПОСЛЕДНЕЕ определение по порядку
# миграций (миграции применяются сортировкой имён файлов). SQL-комментарии
# срезаем: в DOWN-секциях закомментированные CREATE ... $$ ... $$ иначе
# маскируются под «последнюю версию» функции
_FUNCTION_DEF = re.compile(
    r"CREATE OR REPLACE FUNCTION\s+(\w+)\s*\(.*?AS\s*\$\$.*?\$\$;", re.S
)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _function_body(name: str) -> str:
    """Текст последнего CREATE OR REPLACE FUNCTION <name> по миграциям."""
    bodies: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = _LINE_COMMENT.sub("", path.read_text(encoding="utf-8"))
        for match in _FUNCTION_DEF.finditer(text):
            bodies[match.group(1)] = match.group(0)
    assert name in bodies, f"function not found in migrations: {name}"
    return bodies[name]


def _query_constants() -> list[str]:
    return [
        value for key, value in vars(q).items()
        if key.isupper() and isinstance(value, str)
    ]


def _order_by_clauses(*texts: str) -> list[str]:
    """Все строки с ORDER BY (в queries.py и телах функций они однострочные)."""
    return [
        line.strip()
        for text in texts
        for line in text.splitlines()
        if "ORDER BY" in line
    ]


class TestEdgeOrderingInvariant:
    """(а) детерминированный порядок рёбер — не по весу."""

    def test_traverse_orders_edges_by_rel_id(self):
        assert "ORDER BY e.rel_id" in _function_body("graph_traverse_full")

    def test_relations_ordered_by_created_at_desc(self):
        assert "ORDER BY created_at DESC" in _function_body("get_relations_unified")

    def test_no_battle_path_sorts_edges_by_weight(self):
        clauses = _order_by_clauses(
            *_query_constants(),
            _function_body("graph_traverse_full"),
            _function_body("get_relations_unified"),
        )
        offenders = [
            clause for clause in clauses
            if "weight" in clause.lower() or "w_eff" in clause.lower()
        ]
        assert not offenders, f"edge ordering by weight leaked: {offenders}"


class TestWEffEncapsulation:
    """(б)+(в): w_eff — основа PPR, но живёт только в SQL-вычислениях и PPR."""

    def test_ppr_feeds_on_w_eff(self):
        """(б) SELECT_ACTIVATION_EDGES отдаёт именно w_eff, не raw weight."""
        assert "AS w_eff" in q.SELECT_ACTIVATION_EDGES

    def test_jsonb_stored_procs_do_not_expose_w_eff(self):
        """(в) JSONB-выдающие хранимки не содержат w_eff вовсе."""
        assert "w_eff" not in _function_body("graph_traverse_full")
        assert "w_eff" not in _function_body("get_relations_unified")

    def test_w_eff_column_exists_only_in_activation_select(self):
        """(в) 'AS w_eff' в queries.py — ровно одна колонка: внутренняя
        выборка PPR; никакой другой SELECT не выставляет w_eff наружу."""
        holders = [
            name for name, value in vars(q).items()
            if name.isupper() and isinstance(value, str) and "AS w_eff" in value
        ]
        assert holders == ["SELECT_ACTIVATION_EDGES"]

    def test_activation_edges_reach_only_ppr(self):
        """(в) имя SELECT_ACTIVATION_EDGES в боевых модулях встречается
        только в определении (queries.py) и вызове fetch_activation_edges
        (pg_repository) — путь рёбер → CSR → PPR, в JSONB-выдачу не идёт."""
        server = PROJECT_ROOT / "memory_server"
        allowed = {
            Path("memory_server") / "db" / "queries.py",
            Path("memory_server") / "memory" / "pg_repository.py",
        }
        usages = [
            (path.relative_to(PROJECT_ROOT), line.strip())
            for path in server.rglob("*.py")
            if "__pycache__" not in path.parts
            for line in path.read_text(encoding="utf-8").splitlines()
            if "SELECT_ACTIVATION_EDGES" in line and "_w_eff_sql" not in line
        ]
        assert usages, "SELECT_ACTIVATION_EDGES must be used somewhere"
        leaked = [u for u in usages if u[0] not in allowed]
        assert not leaked, f"w_eff source leaked beyond PPR path: {leaked}"
        repo_calls = [line for path, line in usages if path.name == "pg_repository.py"]
        assert len(repo_calls) == 1
        assert "q.SELECT_ACTIVATION_EDGES" in repo_calls[0]


class TestSymmetricMirrorSQL:
    """Зеркала симметричных типов (вердикт Эны 23.09): текстовые инварианты."""

    def test_mirror_branch_union_all_swaps_ends(self):
        directed, mirror = q.SELECT_ACTIVATION_EDGES.split("UNION ALL")
        assert "r.source_id::text AS source_id" in directed
        assert "r.target_id::text AS source_id" in mirror
        assert "r.source_id::text AS target_id" in mirror

    def test_mirror_only_symmetric_types_within_request_filter(self):
        """Зеркальная ветка: link_type = ANY($4) И проходит фильтр $3 —
        зеркалим только рёбра, выбранные запросом; directed-типы
        (depends_on и прочие) второй дуги не получают."""
        _, mirror = q.SELECT_ACTIVATION_EDGES.split("UNION ALL")
        assert "ANY($4::text[])" in mirror
        assert "ANY($3)" in mirror  # не обходим фильтр link_types запроса

    def test_symmetric_config_default_related_to_only(self):
        config = Settings(
            dedup_enabled=False,
            hybrid_search_enabled=False,
            traverse_activation_enabled=True,
        )
        assert config.traverse_symmetric_link_types == ["related_to"]
        directed_types = (
            "depends_on", "contradicts", "supersedes", "solves", "references",
        )
        assert not set(directed_types) & set(config.traverse_symmetric_link_types)

    def test_mirrored_graph_matches_reference_ppr(self):
        """Что UNION ALL вернёт обе дуги — математика: зеркальный список
        рёбер через CSR и независимый оракул даёт один ранг (встречные
        дуги живут в своих клетках M)."""
        mirrored = GRAPH6 + [(t, s, w) for s, t, w in GRAPH6 if s != t]
        spreader = ActivationSpreader(mirrored, damping=0.85)
        got = dict(spreader.spread(["seed"], iterations=25))
        ref = ppr_reference(mirrored, ["seed"], 0.85, 25)
        for node, expected in ref.items():
            assert got[node] == pytest.approx(expected, abs=1e-6), node
        assert all(not np.isnan(value) for value in got.values())
