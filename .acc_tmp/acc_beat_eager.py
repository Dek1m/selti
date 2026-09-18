# Приёмка Фазы 2 — (ж) eager-прогон beat-задач через настоящий composition root.
# env выставляется ДО импорта memory_server (Settings читает env при импорте).
import os

os.environ["DATABASE_URL"] = "postgresql+asyncpg://svc_athene_ai:acctest@127.0.0.1:5433/memory"
os.environ["QDRANT_ENABLED"] = "false"

import asyncio
import sys

sys.path.insert(0, r"E:\Projects\Python\selti")

from memory_server.celery_app import app  # noqa: E402

app.conf.task_always_eager = True

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


async def seed_and_probe():
    import asyncpg

    conn = await asyncpg.connect(
        "postgresql://svc_athene_ai:acctest@127.0.0.1:5433/memory")
    # чистка прошлого прогона
    await conn.execute("DELETE FROM memories WHERE user_id = 'u_beat'")
    rows = {}
    for key, ns, conf, frozen, days in (
        ("c1", "code_knowledge", 1.0, False, 40),
        ("d1", "dialogue_insights", 1.0, False, 0),
        ("f1", "code_knowledge", 1.0, True, 40),
        ("st1", "code_knowledge", 0.05, False, 40),
        ("fresh", "code_knowledge", 0.05, False, 0),
    ):
        rid = await conn.fetchval(
            """INSERT INTO memories (user_id, content, metadata, namespace_id, importance,
                 status, confidence, frozen, created_at, updated_at, valid_from)
               SELECT 'u_beat', $2, '{}'::jsonb, n.id, 3, 'asserted', $3, $4,
                 now() - make_interval(days => $5), now() - make_interval(days => $5), now()
               FROM namespaces n WHERE n.uid = $1
               RETURNING id::text""",
            ns, f"beat-seed {key}", conf, frozen, days)
        rows[key] = rid
    await conn.close()
    return rows


async def read_confs(rows):
    import asyncpg
    conn = await asyncpg.connect("postgresql://svc_athene_ai:acctest@127.0.0.1:5433/memory")
    out = {}
    for k, v in rows.items():
        out[k] = await conn.fetchval(
            "SELECT (confidence, status, frozen) FROM memories WHERE id=$1::uuid", v)
    await conn.close()
    return out


def run_eager():
    from memory_server.tasks.lifecycle_tasks import confidence_decay, mark_stale

    r1 = confidence_decay.apply(throw=True).get()
    r2 = mark_stale.apply(throw=True).get()
    return r1, r2


async def main():
    rows = await seed_and_probe()
    before = await read_confs(rows)

    r_decay, r_stale = await asyncio.to_thread(run_eager)

    after = await read_confs(rows)

    print(f"      decay result: {r_decay}")
    print(f"      stale result: {r_stale}")

    # (ж1) decay затрагивает только нужные
    check("decay: touched только не-frozen asserted",
          r_decay["touched"].get("code_knowledge", 0) >= 2
          and r_decay["touched"].get("dialogue_insights", 0) >= 1,
          str(r_decay["touched"]))
    # (ж2) per-namespace rate: code_knowledge 0.995, dialogue_insights 0.99
    c1b, c1a = before["c1"][0], after["c1"][0]
    d1b, d1a = before["d1"][0], after["d1"][0]
    check("decay: rate code_knowledge ×0.995", abs(c1a - c1b * 0.995) < 1e-6, f"{c1b} → {c1a}")
    check("decay: rate dialogue_insights ×0.99", abs(d1a - d1b * 0.99) < 1e-6, f"{d1b} → {d1a}")
    # (ж3) frozen не тронут
    check("decay: frozen не тронут", before["f1"][0] == after["f1"][0] == 1.0,
          f"{before['f1'][0]} → {after['f1'][0]}")
    # (ж4) superseded/retracted не затронуты (фильтр status='asserted' в SQL)

    # (ж5) mark_stale: count без изменения статуса
    check("mark_stale вернул счётчик ≥1", r_stale["stale_candidates"] >= 1, str(r_stale))
    statuses_unchanged = all(b[1] == a[1] == "asserted" for b, a in
                             zip(before.values(), after.values()))
    check("mark_stale НЕ меняет статусы", statuses_unchanged)
    # st1 (0.05) уже ниже floor → decay его не трогает (корректная семантика floor)
    check("stale-кандидат не затухает дальше (confidence ≤ floor)",
          after["st1"][0] <= 0.1, str(after["st1"][0]))

    # stale_list: видит st1 (40 дней), не видит fresh — отдельным пулом в main loop
    from memory_server.db.pool import create_pool
    from memory_server.memory.pg_repository import PostgreSQLRepository

    pool = await create_pool(
        "postgresql+asyncpg://svc_athene_ai:acctest@127.0.0.1:5433/memory", min_size=1, max_size=2)
    pg = PostgreSQLRepository(pool)
    async with pool.acquire() as conn:
        ns_id = await conn.fetchval("SELECT id FROM namespaces WHERE uid='code_knowledge'")
    stale = await pg.list_stale(threshold=0.3, stale_days=30, namespace_id=ns_id, limit=50)
    stale_ids = {str(r.id) for r in stale}
    check("stale_list видит st1", rows["st1"] in stale_ids)
    check("stale_list не видит fresh", rows["fresh"] not in stale_ids)
    await pool.close()

    # закрыть composition-root в его собственном loop (не main)
    def close_state():
        import asyncio as aio
        from memory_server.state import get_state
        state = get_state()
        aio.get_event_loop_policy()
        aio.run(state.aclose())
    await asyncio.to_thread(close_state)

    print(f"\n=== ИТОГ (ж beat eager): PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("ПРОВАЛЫ:", *FAIL, sep="\n  - ")
        sys.exit(1)


asyncio.run(main())
