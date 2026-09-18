# Приёмка Фазы 2 — пограничные на схеме БЕЗ 022 (схема = 001..021).
# Проверяет: cluster_list/refresh_clusters graceful, traverse-регресс (021),
# memory_update merge-on-place + version-триггер. Прод не трогается.
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


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


class FakeEmbedding:
    async def embed(self, text: str):
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for b in h[:16]]

    async def embed_many(self, texts):
        return [await self.embed(t) for t in texts]


async def main():
    pool = await create_pool(DSN, min_size=1, max_size=3)
    pg = PostgreSQLRepository(pool=pool)
    ns_repo = NamespaceRepository(pool)
    qdrant = MagicMock(name="qdrant-mock")  # методы: upsert_vector/set_payload/delete...
    repo = MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=ns_repo)
    cfg = Settings(dedup_enabled=False, hybrid_search_enabled=False)
    project_repo = MagicMock()
    project_repo.resolve_id = asyncio.run if False else None
    async def _resolve(key):
        return None
    project_repo.resolve_id = _resolve
    service = MemoryService(
        repository=repo, embedding_provider=FakeEmbedding(),
        namespace_repository=ns_repo, config=cfg, project_repository=project_repo,
    )

    # ── Пограничный 1: cluster_list без 022 ─────────────────────────
    res = await service.cluster_list(namespace="code_knowledge")
    check("cluster_list без 022 → ok=False", res.get("ok") is False, str(res.get("reason")))
    check("cluster_list без 022 → reason текст",
          "022" in str(res.get("reason", "")), str(res))

    # ── Пограничный 2: refresh_clusters без 022 ─────────────────────
    res = await service.refresh_clusters("code_knowledge")
    check("refresh_clusters без 022 → ok=False", res.get("ok") is False, str(res.get("reason")))

    # ── Пограничный 2b: refresh_clusters несуществующий ns ──────────
    from memory_server.exceptions import NotFoundError
    try:
        await service.refresh_clusters("no_such_ns_xyz")
        check("refresh_clusters unknown ns → NotFoundError", False, "не поднялось исключение")
    except NotFoundError:
        check("refresh_clusters unknown ns → NotFoundError", True)

    # ── Регресс: traverse после 021 ─────────────────────────────────
    a, _ = await service.store("Traverse root node alpha", "u_acc", namespace="code_knowledge")
    b, _ = await service.store("Traverse child node beta", "u_acc", namespace="code_knowledge")
    c, _ = await service.store("Traverse grandchild gamma", "u_acc", namespace="code_knowledge")
    await service.add_relation(a.id, target_id=b.id, link_type="contains")
    await service.add_relation(b.id, target_id=c.id, link_type="contains")
    tr = await service.traverse(a.id, depth=3)
    ids = {str(n["id"]) for n in tr.nodes}
    check("traverse видит 3 узла", {a.id, b.id, c.id} <= ids, f"nodes={len(tr.nodes)}")
    check("traverse edges содержит contains", any(e.link_type == "contains" for e in tr.edges))

    # ── Регресс: memory_update merge-on-place + version ─────────────
    async def version_of(mid: str) -> int:
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT version FROM memories WHERE id=$1", mid)

    m, _ = await service.store("Update merge subject original", "u_acc", namespace="code_knowledge",
                               metadata={"k": "v1", "keep": 1})
    check("update: стартовая version=1", await version_of(m.id) == 1)
    u1 = await service.update(m.id, metadata={"k": "v2"})
    check("update: metadata merge (k=v2, keep=1)",
          u1.metadata.get("k") == "v2" and u1.metadata.get("keep") == 1, str(u1.metadata))
    check("update: metadata-only не бампит version", await version_of(m.id) == 1)
    u2 = await service.update(m.id, content="Update merge subject revised content")
    check("update: content-изменение бампит version (триггер)", await version_of(m.id) == 2)

    await pool.close()
    print(f"\n=== ИТОГ (схема без 022): PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("ПРОВАЛЫ:", *FAIL, sep="\n  - ")
        sys.exit(1)


asyncio.run(main())
