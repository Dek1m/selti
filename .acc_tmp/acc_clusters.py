# Приёмка Фазы 2 — сценарий (е): assign_clusters на изолированном PG 5433.
import asyncio
import sys
from unittest.mock import MagicMock

sys.path.insert(0, r"E:\Projects\Python\selti")

from memory_server.config import Settings
from memory_server.db.pool import create_pool
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.memory.service import MemoryService

DSN = "postgresql+asyncpg://svc_athene_ai:acctest@127.0.0.1:5433/memory"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


class FakeEmbedding:
    async def embed(self, text):
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for b in h[:16]]

    async def embed_many(self, texts):
        return [await self.embed(t) for t in texts]


GROUP1 = [
    "selti memory server deployed docker postgres redis",
    "selti memory server deployed docker postgres redis volumes",
]
GROUP2 = [
    "celery worker beat schedule nightly lifecycle maintenance tasks",
    "celery worker beat schedule nightly lifecycle maintenance jobs",
]
LONER = "mikrotik router firewall rules nat bridge network"


async def main():
    pool = await create_pool(DSN, min_size=1, max_size=3)
    pg = PostgreSQLRepository(pool=pool)
    ns_repo = NamespaceRepository(pool)
    repo = MemoryRepository(pg=pg, qdrant=MagicMock(), ns_repo=ns_repo)

    async def resolve_none(key):
        return None

    project_repo = MagicMock()
    project_repo.resolve_id = resolve_none
    service = MemoryService(
        repository=repo, embedding_provider=FakeEmbedding(),
        namespace_repository=ns_repo,
        config=Settings(dedup_enabled=False, hybrid_search_enabled=False),
        project_repository=project_repo,
    )

    async with pool.acquire() as conn:
        # повторяемость: чистим ns прошлого прогона
        await conn.execute("DELETE FROM memories WHERE namespace_id = (SELECT id FROM namespaces WHERE uid='acceptance_clusters')")
        await conn.execute("DELETE FROM clusters WHERE namespace_id = (SELECT id FROM namespaces WHERE uid='acceptance_clusters')")

    ids = {}
    for label, texts in (("g1a", GROUP1), ("g2a", GROUP2), ("loner", [LONER])):
        for i, t in enumerate(texts):
            rec, _ = await service.store(t, "u_clu", namespace="acceptance_clusters")
            ids[f"{label}{i}"] = rec.id

    async def val(sql, *args):
        async with pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    # фактическая близость пар сида (для протокола)
    async with pool.acquire() as conn:
        sims = await conn.fetch("""
            SELECT a.uid_pair, trigram_similarity(a.g1, a.g2) AS sim FROM (VALUES
              ('g1', granule_trigrams($1), granule_trigrams($2)),
              ('g2', granule_trigrams($3), granule_trigrams($4))
            ) a(uid_pair, g1, g2)""", GROUP1[0], GROUP1[1], GROUP2[0], GROUP2[1])
    for s in sims:
        print(f"      sim[{s['uid_pair']}] = {s['sim']}")

    r = await service.refresh_clusters("acceptance_clusters")
    check("refresh_clusters ok=True, 2 кластера", r.get("ok") is True and len(r["clusters"]) == 2,
          str(r)[:200])

    n_clustered = await val("""
        SELECT count(*) FROM memories m
        WHERE m.namespace_id=(SELECT id FROM namespaces WHERE uid='acceptance_clusters')
          AND m.cluster_id IS NOT NULL""")
    loner_cluster = await val("SELECT cluster_id IS NOT NULL FROM memories WHERE id=$1", ids["loner0"])
    check("размечены 4 гранулы (2+2)", n_clustered == 4, str(n_clustered))
    check("одиночка вне кластеров", not loner_cluster)

    lst = await service.cluster_list(namespace="acceptance_clusters")
    check("cluster_list: ok=True, 2 записи", lst.get("ok") is True and len(lst["clusters"]) == 2)
    mc = sorted(c["member_count"] for c in lst["clusters"])
    check("member_count = [2, 2]", mc == [2, 2], str(mc))
    coh = [c["coherence"] for c in lst["clusters"]]
    check("coherence в 0..1 и заполнен", all(c is not None and 0 <= c <= 1 for c in coh), str(coh))
    check("labels заполнены", all(c.get("label") for c in lst["clusters"]),
          str([c.get("label") for c in lst["clusters"]]))

    # детерминизм/идемпотентность: повтор не плодит
    before_ids = sorted(str(c["id"]) for c in lst["clusters"])
    r2 = await service.refresh_clusters("acceptance_clusters")
    lst2 = await service.cluster_list(namespace="acceptance_clusters")
    after_ids = sorted(str(c["id"]) for c in lst2["clusters"])
    check("повтор: те же детерминированные id", before_ids == after_ids, f"{before_ids} == {after_ids}")
    check("повтор: кластеров по-прежнему 2", len(r2["clusters"]) == 2 and len(lst2["clusters"]) == 2)

    # updated_at семантика (022 §5): кластерная разметка не «омолаживает» гранулы
    async with pool.acquire() as conn:
        old_ts = await conn.fetchval(
            "SELECT max(updated_at) FROM memories WHERE id = ANY($1::uuid[])", list(ids.values()))
    import asyncio as aio
    await aio.sleep(1.1)
    await service.refresh_clusters("acceptance_clusters")
    new_ts = await val("SELECT max(updated_at) FROM memories WHERE id = ANY($1::uuid[])", list(ids.values()))
    check("кластерная разметка не меняет updated_at гранул", old_ts == new_ts,
          f"{old_ts} == {new_ts}")

    # empty-namespace: no pairs → 0 кластеров, не падает
    await service.store("Единственная гранула пустого кластера", "u_clu", namespace="acceptance_clusters")
    r3 = await service.refresh_clusters("acceptance_clusters")
    check("пересчёт после изменения состава всё ещё ок", r3.get("ok") is True, str(r3)[:120])

    await pool.close()
    print(f"\n=== ИТОГ (е кластеры): PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("ПРОВАЛЫ:", *FAIL, sep="\n  - ")
        sys.exit(1)


asyncio.run(main())
