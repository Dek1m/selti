"""Тесты T0.3: мосты между кластерами — чистые функции + betweenness.

Междуность: цепочка/звезда имеют аналитически известные значения;
механика мягкого таймаута проверяется подменой betweenness на sleep.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tools.diagnostics.bridges import (
    BridgesConfig,
    build_igraph,
    extract_bridges,
    top_betweenness,
)


@dataclass
class _Row:
    """Мок asyncpg.Record из BRIDGES_SQL."""

    source_id: str
    target_id: str
    link_type: str = "related_to"
    weight: float = 0.5
    rel_source: str | None = "linker_v3"
    layer: str | None = "l1c"
    src_cluster: str | None = "c1"
    tgt_cluster: str | None = "c2"
    src_name: str | None = "src entity"
    tgt_name: str | None = "tgt entity"
    src_namespace: str = "code_knowledge"
    tgt_namespace: str = "project_meta"
    created_at: datetime = field(default_factory=lambda: datetime(2026, 9, 22, tzinfo=timezone.utc))

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)


def test_extract_bridges_summary_and_fields() -> None:
    rows = [
        _Row(source_id="a", target_id="b"),                                  # мост
        _Row(source_id="a", target_id="b", link_type="solves", weight=0.9),  # мост, другой тип
        _Row(source_id="a", target_id="b", src_cluster="c2", tgt_cluster="c1"),  # мост (обратное направление пары кластеров)
        _Row(source_id="x", target_id="y", src_cluster=None),                # без кластера: SQL отсекает such рёбра сам
    ]
    bridges, summary = extract_bridges(rows)
    assert summary["total_bridges"] == 4  # функция только форматирует: фильтр живёт в SQL
    assert summary["by_link_type"]["related_to"] == 3
    assert summary["by_link_type"]["solves"] == 1
    assert summary["by_namespace_pair"]["code_knowledge→project_meta"] == 4
    assert bridges[0]["weight"] == 0.5
    assert bridges[0]["created_at"] == "2026-09-22T00:00:00+00:00"
    assert bridges[1]["weight"] == 0.9


def test_build_igraph_collapses_duplicates_and_selfloops() -> None:
    graph = build_igraph(["a", "b", "c"], [("a", "b"), ("b", "a"), ("a", "a"), ("b", "c")])
    assert graph.vcount() == 3
    assert graph.ecount() == 2  # (a,b) и (b,c); петля a-a отброшена


def test_betweenness_chain_center_wins() -> None:
    # Путь a-b-c-d: через b проходят пары (a,c),(a,d); через c — (a,d),(b,d)
    nodes = ["a", "b", "c", "d"]
    edges = [("a", "b"), ("b", "c"), ("c", "d")]
    result = top_betweenness(nodes, edges, top=4, timeout_s=30)
    assert result["computed"] is True
    scores = {item["node_id"]: item["betweenness"] for item in result["top"]}
    assert scores["a"] == 0.0 and scores["d"] == 0.0
    assert scores["b"] == 2.0 and scores["c"] == 2.0


def test_betweenness_star_hub_wins() -> None:
    # Звезда: hub соединяет C(4,2)=6 пар листьев
    nodes = ["hub", "l1", "l2", "l3", "l4"]
    edges = [("hub", leaf) for leaf in nodes[1:]]
    result = top_betweenness(nodes, edges, top=5, timeout_s=30)
    scores = {item["node_id"]: item["betweenness"] for item in result["top"]}
    assert scores["hub"] == 6.0
    assert all(scores[leaf] == 0.0 for leaf in nodes[1:])


def test_betweenness_soft_timeout() -> None:
    # Мягкий таймаут: betweenness «зависла» → computed=False, timeout=True,
    # результат не ждём (daemon-поток умрёт вместе с процессом)
    import time

    import igraph as ig

    original = ig.Graph.betweenness

    def _stuck(self: ig.Graph, *args: object, **kwargs: object) -> list[float]:
        time.sleep(5)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    ig.Graph.betweenness = _stuck  # type: ignore[method-assign]
    try:
        result = top_betweenness(["a", "b"], [("a", "b")], timeout_s=0.05)
    finally:
        ig.Graph.betweenness = original  # type: ignore[method-assign]
    assert result["computed"] is False
    assert result["timeout"] is True


def test_pipeline_end_to_end_with_fake_fetches() -> None:
    rows = [
        _Row(source_id="a", target_id="b", src_cluster="c1", tgt_cluster="c2"),
        _Row(source_id="b", target_id="c", src_cluster="c2", tgt_cluster="c3", weight=0.9),
    ]
    degree_rows = [
        _DegreeRow("a", 1, "alpha"),
        _DegreeRow("b", 2, "beta"),
        _DegreeRow("c", 1, "gamma"),
    ]
    subgraph_rows = [dict(source_id="a", target_id="b"), dict(source_id="b", target_id="c")]
    cfg = BridgesConfig(pg_dsn="unused", with_betweenness=True, top_k=3)

    async def fake_bridges() -> list[_Row]:
        return rows

    async def fake_degrees() -> list[_DegreeRow]:
        return degree_rows

    async def fake_subgraph(top_ids: list[str]) -> list[dict]:
        assert len(top_ids) == 3
        return subgraph_rows

    from tools.diagnostics.bridges import run

    report = asyncio.run(run(cfg, fetch_bridges=fake_bridges, fetch_degrees=fake_degrees, fetch_subgraph=fake_subgraph))
    assert report["total_bridges"] == 2
    assert report["by_link_type"]["related_to"] == 2
    assert {b["src_cluster"] for b in report["bridges"]} == {"c1", "c2"}
    btw = report["betweenness"]
    assert btw["computed"] is True
    assert btw["subgraph_nodes"] == 3 and btw["subgraph_edges"] == 2
    scores = {item["node_id"]: item["betweenness"] for item in btw["top"]}
    assert scores["b"] == 1.0  # цепочка a-b-c: через b единственная пара (a, c)
    assert scores["a"] == 0.0 and scores["c"] == 0.0
    names = {item["node_id"]: item["entity_name"] for item in btw["top"]}
    assert names["b"] == "beta"


@dataclass
class _DegreeRow:
    node_id: str
    degree: int
    entity_name: str | None

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)
