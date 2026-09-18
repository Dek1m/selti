# Пересоздание приёмочного PG-стенда (порт 5433, локальный кластер Windows).
# Рецепт: DROP memory → CREATE athene_memory → 001-012 → 013 (вне транзакции,
# ALTER DATABASE) → 014-022 → smoke от svc_athene_ai.
import asyncio
import re
from pathlib import Path

import asyncpg

MIGR = Path(r"E:\Projects\Python\selti\migrations")
SUPER = "postgresql://postgres@127.0.0.1:5433/postgres"
APP_DSN = "postgresql://svc_athene_ai:acctest@127.0.0.1:5433/memory"


STUB_OPERATOR = """
DO $stub$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_operator
                 WHERE oprname = '<=>'
                   AND oprleft = 'bytea'::regtype AND oprright = 'bytea'::regtype) THEN
    CREATE FUNCTION __acc_bytea_cos(bytea, bytea) RETURNS float8
      LANGUAGE sql IMMUTABLE RETURN 0.5;
    CREATE OPERATOR <=> (LEFTARG = bytea, RIGHTARG = bytea,
                         PROCEDURE = __acc_bytea_cos);
  END IF;
END
$stub$;
"""


def transform(sql: str) -> str:
    """Стенд (локальный PostgreSQL 1C) без pgvector: тип vector нужен только
    до миграции 011 (векторное хранилище → Qdrant, затем полный дроп).
    Заменяем vector → bytea и вырезаем CREATE EXTENSION; для LANGUAGE sql
    функций с <=> ставим заглушку-оператор (011 их дропает, семантика мертва)."""
    sql = re.sub(r"^\s*CREATE EXTENSION IF NOT EXISTS vector\s*;?\s*$",
                 "", sql, flags=re.IGNORECASE | re.MULTILINE)
    sql = sql.replace("vector(4096)", "bytea")
    sql = re.sub(r"\bvector\b", "bytea", sql)
    if "<=>" in sql:
        sql = STUB_OPERATOR + sql
    return sql

PRE_013 = [  # применяются в athene_memory до переименования
    "001_initial.sql", "002_dedup.sql", "003_athene_memory.sql",
    "004_infrastructure.sql", "005_relations.sql", "006_namespaces.sql",
    "007_drop_namespace_check.sql", "008_add_importance.sql",
    "009_resource_hashes.sql", "009_stored_procedures.sql",
    "010_qdrant_vector_store.sql", "011_drop_pgvector.sql",
    "012_backfill_relations_from_metadata.sql",
]
POST_013 = [  # применяются в memory после переименования.
    # Порядок 019-до-018b обязателен (018b: «Порядок Фазы 0: 017 → 018 → 019
    # → 018b → 018c»): project_context_snapshot из 019 создаётся с is_archived,
    # 018b дропает колонку, 020 пересоздаёт функцию уже со status-семантикой.
    "014_stored_procedures_optimizations.sql", "015_drop_duplicate_index.sql",
    "016_merge_similar_granules_stored_proc.sql", "017_projects_registry.sql",
    "018_memories_canonical.sql", "019_project_contexts.sql",
    "018b_drop_is_archived.sql", "018c_drop_namespace_text.sql",
    "020_stored_procedures_canonical.sql", "021_phase1_search_fixes.sql",
    "022_clusters.sql",
]


async def apply(conn: asyncpg.Connection, filename: str):
    sql = transform((MIGR / filename).read_text(encoding="utf-8"))
    # без явной transaction(): в файлах встречаются собственные BEGIN/COMMIT
    await conn.execute(sql)
    await conn.execute("INSERT INTO _migrations (filename) VALUES ($1)", filename)
    print(f"  applied {filename}")


async def main():
    admin = await asyncpg.connect(SUPER)

    print("[1/6] DROP DATABASE memory / athene_memory")
    await admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname IN ('memory', 'athene_memory') AND pid <> pg_backend_pid()")
    await admin.execute("DROP DATABASE IF EXISTS memory")
    await admin.execute("DROP DATABASE IF EXISTS athene_memory")

    print("[2/6] CREATE DATABASE athene_memory (owner postgres)")
    await admin.execute("CREATE DATABASE athene_memory")

    print("[3/6] миграции 001-012 в athene_memory")
    conn = await asyncpg.connect(SUPER.rsplit("/", 1)[0] + "/athene_memory")
    await conn.execute("""CREATE TABLE IF NOT EXISTS _migrations (
        filename TEXT PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    for f in PRE_013:
        await apply(conn, f)
    await conn.close()

    print("[4/6] 013: rename + grants (вне транзакции)")
    # PG18 запрещает rename текущей базы и GRANT ON TABLES из чужой базы —
    # вместо цельного файла 013 выполняем его же шаги в два подключения:
    # (1) terminate + rename + GRANT DATABASE от postgres,
    # (2) таблицевые GRANT/DEFAULT PRIVILEGES/OWNER уже подключённым к memory
    await admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = 'athene_memory' AND pid <> pg_backend_pid()")
    await admin.execute("ALTER DATABASE athene_memory RENAME TO memory")
    await admin.execute("GRANT ALL PRIVILEGES ON DATABASE memory TO svc_athene_ai")
    conn13 = await asyncpg.connect(SUPER.rsplit("/", 1)[0] + "/memory")
    await conn13.execute("""
        GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO svc_athene_ai;
        GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO svc_athene_ai;
        GRANT CREATE ON SCHEMA public TO svc_athene_ai;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO svc_athene_ai;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO svc_athene_ai;
    """)
    # передаём владение всеми таблицами (в 013 — явный список из 5 таблиц;
    # делаем по факту, чтобы захватить и таблицы 001-012)
    tables = await conn13.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    for t in tables:
        await conn13.execute(
            f'ALTER TABLE public."{t["tablename"]}" OWNER TO svc_athene_ai')
    await conn13.execute(
        "INSERT INTO _migrations (filename) "
        "VALUES ('013_rename_db_and_grant_rights.sql')")
    await conn13.close()

    print("[5/6] миграции 014-022 в memory")
    conn = await asyncpg.connect(SUPER.rsplit("/", 1)[0] + "/memory")
    for f in POST_013:
        await apply(conn, f)
    await conn.close()
    await admin.close()

    print("[6/6] smoke от svc_athene_ai")
    app = await asyncpg.connect(APP_DSN)
    n = await app.fetchval("SELECT count(*) FROM _migrations")
    tables = [r["tablename"] for r in await app.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")]
    await app.execute(
        "INSERT INTO namespaces (uid, name, description) "
        "VALUES ('__smoke__', 'smoke', 'x') ON CONFLICT DO NOTHING")
    await app.execute("DELETE FROM namespaces WHERE uid = '__smoke__'")
    await app.close()
    print(f"  _migrations: {n} (ожидаю {len(PRE_013) + 1 + len(POST_013)})")
    print(f"  tables: {', '.join(tables)}")
    assert n == len(PRE_013) + 1 + len(POST_013), "не все миграции в _migrations"
    print("STAND READY")


asyncio.run(main())
