"""SQL-верификация диагностик на одноразовом мини-кластере PostgreSQL.

Поднимает приватный кластер (initdb → pg_ctl на свободном порту, trust),
разворачивает канон-проекцию схем 005/006/018/022/023 c синтетическим
графом и прогоняет: (1) SQL-файлы T0.1, (2) read-only броню (psql-путь и
asyncpg server_settings — замечание Рэя из RUNBOOK_PROD.md п. 3.3),
(3) пайплайны T0.2/T0.3 end-to-end против настоящей базы (Qdrant-слой
T0.2 — in-memory с инъекцией retrieve: сэмпл-SQL исполняется настоящий).

Нет бинарей PostgreSQL (initdb) — весь модуль пропускается: на проде
этого не требуется. Ничего, кроме собственной temp-директории и порта,
не занимает; кластер останавливается в финализаторе.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

DIAG_DIR = Path(__file__).resolve().parent.parent
SQL_DIR = DIAG_DIR / "sql"

# Канон-проекция схем 005/006/018/022/023 + синтетика (12 гранул,
# кластеры c1..c4, l1c/l1a/l2/ручные рёбра, висячий target, мосты).
FIXTURE_SQL = """
CREATE TABLE namespaces (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memories (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT NOT NULL,
    content      TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}',
    namespace_id UUID NOT NULL REFERENCES namespaces(id),
    importance   INT DEFAULT 3,
    status       TEXT NOT NULL DEFAULT 'asserted',
    valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to     TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    cluster_id   UUID
);

CREATE TABLE relations (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id      UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_id      UUID REFERENCES memories(id) ON DELETE SET NULL,
    target_name    TEXT,
    link_type      TEXT NOT NULL,
    description    TEXT,
    weight         FLOAT NOT NULL DEFAULT 1.0,
    metadata       JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    inherited_from UUID,
    CONSTRAINT chk_link_type CHECK (link_type IN (
        'depends_on', 'used_by', 'extends', 'implements', 'contains',
        'contained_by', 'calls', 'called_by', 'related_to', 'contradicts',
        'solves', 'tested_by', 'implements_adr', 'references', 'follows',
        'precedes', 'alternative_to', 'causes', 'prevents', 'runs_on',
        'exposes', 'mounts', 'derived_from', 'motivates', 'informs',
        'informed_by', 'connected_to'
    ))
);

CREATE INDEX idx_relations_source ON relations (source_id);
CREATE INDEX idx_relations_target ON relations (target_id) WHERE target_id IS NOT NULL;
CREATE INDEX idx_relations_type   ON relations (link_type);

INSERT INTO namespaces (id, uid, name) VALUES
    ('00000000-0000-0000-0001-000000000001', 'code_knowledge', 'Code'),
    ('00000000-0000-0000-0001-000000000002', 'project_meta', 'Meta');

INSERT INTO memories (id, user_id, content, namespace_id, metadata, status, valid_to, cluster_id, created_at) VALUES
    ('00000000-0000-0000-0002-000000000001', 'u1', 'hub granule', '00000000-0000-0000-0001-000000000001', '{"entity_name": "hub", "session_id": "s1"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c1', now() - interval '10 days'),
    ('00000000-0000-0000-0002-000000000002', 'u1', 'l1c singleton peer', '00000000-0000-0000-0001-000000000001', '{"entity_name": "peer", "session_id": "s1"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c1', now() - interval '10 days'),
    ('00000000-0000-0000-0002-000000000003', 'u1', 'confirmed left', '00000000-0000-0000-0001-000000000001', '{"entity_name": "cleft", "session_id": "s2"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c1', now() - interval '5 days'),
    ('00000000-0000-0000-0002-000000000004', 'u1', 'confirmed right', '00000000-0000-0000-0001-000000000001', '{"entity_name": "cright", "session_id": "s2"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c1', now() - interval '5 days'),
    ('00000000-0000-0000-0002-000000000005', 'u1', 'bridge left', '00000000-0000-0000-0001-000000000001', '{"entity_name": "bleft"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c2', now() - interval '3 days'),
    ('00000000-0000-0000-0002-000000000006', 'u1', 'bridge right', '00000000-0000-0000-0001-000000000002', '{"entity_name": "bright"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c3', now() - interval '3 days'),
    ('00000000-0000-0000-0002-000000000007', 'u1', 'intra left', '00000000-0000-0000-0001-000000000001', '{"entity_name": "ileft"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c2', now() - interval '2 days'),
    ('00000000-0000-0000-0002-000000000008', 'u1', 'intra right', '00000000-0000-0000-0001-000000000001', '{"entity_name": "iright"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c2', now() - interval '2 days'),
    ('00000000-0000-0000-0002-000000000009', 'u1', 'no cluster node', '00000000-0000-0000-0001-000000000001', '{"entity_name": "nocluster"}', 'asserted', NULL, NULL, now() - interval '1 day'),
    ('00000000-0000-0000-0002-00000000000a', 'u1', 'closed granule', '00000000-0000-0000-0001-000000000001', '{"entity_name": "closed"}', 'retracted', now() - interval '1 day', 'a0000000-0000-0000-0000-0000000000c4', now() - interval '95 days'),
    ('00000000-0000-0000-0002-00000000000b', 'u1', 'hub neighbor far', '00000000-0000-0000-0001-000000000001', '{"entity_name": "far"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c4', now() - interval '2 days'),
    ('00000000-0000-0000-0002-00000000000c', 'u1', 'isolated granule', '00000000-0000-0000-0001-000000000002', '{"entity_name": "hermit"}', 'asserted', NULL, 'a0000000-0000-0000-0000-0000000000c4', now());

INSERT INTO relations (source_id, target_id, link_type, weight, metadata, created_at) VALUES
    ('00000000-0000-0000-0002-000000000001', '00000000-0000-0000-0002-000000000002', 'related_to', 0.5,
     '{"source": "linker_v3", "layer": "l1c", "session_id": "s1"}', now() - interval '10 days'),
    ('00000000-0000-0000-0002-000000000003', '00000000-0000-0000-0002-000000000004', 'related_to', 0.5,
     '{"source": "linker_v3", "layer": "l1c", "session_id": "s2"}', now() - interval '5 days'),
    ('00000000-0000-0000-0002-000000000003', '00000000-0000-0000-0002-000000000004', 'solves', 0.9,
     '{"source": "linker_v3", "layer": "l2", "confidence": 0.9}', now() - interval '4 days'),
    ('00000000-0000-0000-0002-000000000005', '00000000-0000-0000-0002-000000000006', 'implements_adr', 0.88,
     '{"source": "linker_v3", "layer": "l2"}', now() - interval '3 days'),
    ('00000000-0000-0000-0002-000000000007', '00000000-0000-0000-0002-000000000008', 'related_to', 0.83,
     '{"source": "linker_v3", "layer": "l1a"}', now() - interval '2 days'),
    ('00000000-0000-0000-0002-000000000001', '00000000-0000-0000-0002-00000000000b', 'related_to', 1.0, '{}', now() - interval '2 days'),
    ('00000000-0000-0000-0002-000000000001', '00000000-0000-0000-0002-000000000009', 'references', 1.0, '{}', now() - interval '2 days'),
    ('00000000-0000-0000-0002-00000000000b', NULL, 'related_to', 0.5,
     '{"source": "linker_v3", "layer": "l1c"}', now() - interval '95 days');
"""


def _find_pg_bin() -> Path | None:
    """initdb из PATH или типовых установочных путей Windows."""
    found = shutil.which("initdb")
    if found:
        return Path(found).parent
    for candidate in (
        r"C:\Program Files\PostgreSQL 1C\18\bin",
        r"C:\Program Files\PostgreSQL 1C\17\bin",
        r"C:\Program Files\PostgreSQL\18\bin",
        r"C:\Program Files\PostgreSQL\17\bin",
    ):
        path = Path(candidate) / "initdb.exe"
        if path.exists():
            return path.parent
    return None


PG_BIN = _find_pg_bin()

pytestmark = pytest.mark.skipif(PG_BIN is None, reason="PostgreSQL binaries (initdb) not found")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def fixture_dsn() -> str:
    """Одноразовый кластер + БД с фикстурой; DSN для asyncpg/psql."""
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="selti_diag_pg_"))
    port = _free_port()
    initdb = str(PG_BIN / "initdb")
    pg_ctl = str(PG_BIN / "pg_ctl")
    psql = str(PG_BIN / "psql")
    createdb = str(PG_BIN / "createdb")

    result = _run([initdb, "-D", str(work / "data"), "-U", "postgres", "-A", "trust", "-E", "UTF8"])
    assert result.returncode == 0, result.stderr

    # pg_ctl БЕЗ capture_output: postgres-демон наследует хэндлы пайпов,
    # и subprocess.run(capture_output=True) ждал бы их закрытия вечно
    # (проверено локально — дедлок на старте). Свой лог сервер пишет в -l.
    subprocess.run(
        [pg_ctl, "-D", str(work / "data"), "-o", f"-p {port} -c listen_addresses=127.0.0.1",
         "-l", str(work / "pg.log"), "-w", "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    host_port = f"127.0.0.1:{port}"
    try:
        assert _run([createdb, "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "diag_fixture"]).returncode == 0
        fixture_file = work / "fixture.sql"
        fixture_file.write_text(FIXTURE_SQL, encoding="utf-8")
        result = _run([psql, "-h", "127.0.0.1", "-p", str(port), "-U", "postgres",
                       "-d", "diag_fixture", "-v", "ON_ERROR_STOP=1", "-q", "-f", str(fixture_file)])
        assert result.returncode == 0, result.stderr
        yield f"postgresql://postgres@{host_port}/diag_fixture"
    finally:
        _run([pg_ctl, "-D", str(work / "data"), "-m", "fast", "stop"])
        shutil.rmtree(work, ignore_errors=True)


# ── T0.1: SQL-файлы ──────────────────────────────────────────────────


@pytest.mark.parametrize("sql_file, markers", [
    ("dead_edges_l1c.sql", ["edges_total", "singleton_pct", "l1c_degree", "l1c_dangling"]),
    ("degree_histogram.sql", ["p50", "bucket", "isolated_granules", "total_degree"]),
    ("edges_by_layer.sql", ["rel_source", "layer", "dangling_pct", "relations_total"]),
])
def test_sql_files_execute_and_report(fixture_dsn: str, sql_file: str, markers: list[str]) -> None:
    psql = str(PG_BIN / "psql")
    dsn = fixture_dsn.replace("postgresql://", "").split("@")
    result = _run([psql, "-h", dsn[1].split(":")[0], "-p", dsn[1].split(":")[1].split("/")[0],
                   "-U", dsn[0], "-d", "diag_fixture", "-v", "ON_ERROR_STOP=1",
                   "-f", str(SQL_DIR / sql_file)])
    assert result.returncode == 0, result.stderr
    for marker in markers:
        assert marker in result.stdout, f"{sql_file}: нет колонки {marker}"


def test_dead_edges_numbers_match_fixture(fixture_dsn: str) -> None:
    psql = str(PG_BIN / "psql")
    host_port = fixture_dsn.split("@")[1].split("/")[0]
    host, port = host_port.rsplit(":", 1)
    result = _run([psql, "-h", host, "-p", port, "-U", "postgres", "-d", "diag_fixture",
                   "-v", "ON_ERROR_STOP=1", "-A", "-t",
                   "-c", """
        BEGIN TRANSACTION READ ONLY;
        WITH pair_stats AS (
            SELECT least(source_id, target_id) AS a, greatest(source_id, target_id) AS b,
                   count(*) FILTER (WHERE metadata->>'source' = 'linker_v3'
                                 AND metadata->>'layer' = 'l1c') AS l1c_edges,
                   count(*) FILTER (WHERE NOT (metadata->>'source' = 'linker_v3'
                                          AND metadata->>'layer' = 'l1c')) AS non_l1c_edges
            FROM relations WHERE target_id IS NOT NULL GROUP BY 1, 2
        )
        SELECT count(*) FILTER (WHERE l1c_edges > 0),
               count(*) FILTER (WHERE l1c_edges > 0 AND non_l1c_edges = 0)
        FROM pair_stats;
        COMMIT;
                   """])
    assert result.returncode == 0, result.stderr
    # 2 l1c-пары (hub↔peer, cleft↔cright), из них singleton — только hub↔peer
    assert "2|1" in result.stdout


# ── Read-only броня ──────────────────────────────────────────────────


def test_psql_read_only_session_rejects_writes(fixture_dsn: str) -> None:
    psql = str(PG_BIN / "psql")
    host_port = fixture_dsn.split("@")[1].split("/")[0]
    host, port = host_port.rsplit(":", 1)
    result = _run([psql, "-h", host, "-p", port, "-U", "postgres", "-d", "diag_fixture",
                   "-c", "SET default_transaction_read_only = on;",
                   "-c", "INSERT INTO namespaces (uid, name) VALUES ('evil', 'x');"])
    assert result.returncode != 0
    # Сообщение локализовано («только чтение»); сверяемся по обоим вариантам
    assert "read-only" in (result.stderr + result.stdout) or "только чтение" in (result.stderr + result.stdout)


def test_asyncpg_server_settings_reject_writes(fixture_dsn: str) -> None:
    from tools.diagnostics.cosine_histogram import open_read_only_connection

    async def _try_write() -> None:
        conn = await open_read_only_connection(fixture_dsn, 30)
        try:
            await conn.execute("INSERT INTO namespaces (uid, name) VALUES ('evil', 'x')")
        finally:
            await conn.close()

    with pytest.raises(Exception, match="read-only|только чтение"):
        asyncio.run(_try_write())


# ── T0.2/T0.3: пайплайны против настоящей PG-базы ────────────────────


def test_cosine_pipeline_real_pg_sample(fixture_dsn: str) -> None:
    from qdrant_client import QdrantClient
    from qdrant_client import models as qm

    from tools.diagnostics.cosine_histogram import DiagConfig, retrieve_vectors_batched, run

    client = QdrantClient(":memory:")
    client.create_collection("diag", vectors_config=qm.VectorParams(size=4, distance=qm.Distance.COSINE))
    # векторы для всех гранул фикстуры; id — hex-суффиксы (…000a/…000b/…000c),
    # namespace-направления: code_knowledge ↔ project_meta ортогональны
    for i in range(1, 13):
        granule_id = f"00000000-0000-0000-0002-{i:012x}"
        vector = [1.0, 0.0, 0.0, 0.0] if i not in (0x6, 0xC) else [0.0, 1.0, 0.0, 0.0]
        client.upsert("diag", points=[qm.PointStruct(id=granule_id, vector=vector, payload={})])
    cfg = DiagConfig(pg_dsn=fixture_dsn, sample_per_ns=100, seed="selfcheck")

    report = asyncio.run(run(cfg, retrieve=lambda ids: retrieve_vectors_batched(client, "diag", ids, 256)))
    code = report["namespaces"]["code_knowledge"]
    meta = report["namespaces"]["project_meta"]
    # asserted по фикстуре: code_knowledge — 9 (retracted 0a не берётся),
    # project_meta — 2 (bright и hermit)
    assert code["sampled"] == 9 and meta["sampled"] == 2
    assert code["stats"]["pairs"] == 36 and meta["stats"]["pairs"] == 1
    # все векторы внутри namespace коллинеарны → 100% пар выше порога
    assert code["stats"]["shares"][">0.95"] == 1.0
    assert meta["stats"]["shares"][">0.95"] == 1.0
    client.close()


def test_bridges_pipeline_real_pg(fixture_dsn: str) -> None:
    from tools.diagnostics.bridges import BridgesConfig, run

    cfg = BridgesConfig(pg_dsn=fixture_dsn, with_betweenness=True, top_k=12)
    report = asyncio.run(run(cfg))
    # мосты: bleft(c2)→bright(c3) и hub(c1)→far(c4); 09 без кластера отсечён
    assert report["total_bridges"] == 2
    cluster_pairs = {(b["src_cluster"], b["tgt_cluster"]) for b in report["bridges"]}
    assert ("a0000000-0000-0000-0000-0000000000c2", "a0000000-0000-0000-0000-0000000000c3") in cluster_pairs
    assert report["by_link_type"]["implements_adr"] == 1
    btw = report["betweenness"]
    assert btw["computed"] is True
    # 8 рёбер − 1 висячее − 1 дубль-пара (03↔04: related_to и solves
    # схлопываются igraph'ом в одно неориентированное ребро) = 6
    assert btw["subgraph_nodes"] == 10 and btw["subgraph_edges"] == 6
