# Приёмка Фазы 2 — сценарии а-д на изолированном PG 5433 (схема 001..022).
# (е) кластеры и (ж) beat — отдельные скрипты.
import asyncio
import sys
from unittest.mock import MagicMock

sys.path.insert(0, r"E:\Projects\Python\selti")

from memory_server.config import Settings
from memory_server.db.pool import create_pool
from memory_server.exceptions import ConflictError, NotFoundError
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.memory.service import MemoryService

# Re-check Фазы 2 (2026-09-19): приёмочный патч GET_HISTORY УДАЛЁН —
# фикс Соны (JOIN по b.next_id/f.next_id) проверяем на чистом коде.

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
    qdrant = MagicMock(name="qdrant-mock")
    repo = MemoryRepository(pg=pg, qdrant=qdrant, ns_repo=ns_repo)
    cfg = Settings(dedup_enabled=False, hybrid_search_enabled=False)

    async def resolve_none(key):
        return None

    project_repo = MagicMock()
    project_repo.resolve_id = resolve_none
    service = MemoryService(
        repository=repo, embedding_provider=FakeEmbedding(),
        namespace_repository=ns_repo, config=cfg, project_repository=project_repo,
    )

    async def row(sql, *args):
        async with pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def val(sql, *args):
        async with pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    # повторяемость: чистим сиды прошлых прогонов (связи снимет CASCADE/SET NULL)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE user_id = 'u_acc'")

    # ══ (а) SUPERSESSION E2E ══════════════════════════════════════
    print("\n── (а) Supersession E2E ──")
    A, _ = await service.store("Postgres слушает порт 5432", "u_acc",
                               namespace="code_knowledge",
                               metadata={"src": "doc1"}, importance=4)
    B = await service.create_version(A.id, "Postgres слушит порт 5433",
                                     metadata_merge={"extra": "x"})
    ra = await row("SELECT status, superseded_by::text, valid_to, valid_from, version, confidence FROM memories WHERE id=$1", A.id)
    rb = await row("SELECT status, supersedes::text, valid_from, version, confidence, frozen, importance, metadata, user_id FROM memories WHERE id=$1", B.id)
    check("A.status=superseded", ra["status"] == "superseded")
    check("A.superseded_by=B.id", ra["superseded_by"] == B.id)
    check("A.valid_to = B.valid_from (правило Graphiti)", ra["valid_to"] == rb["valid_from"],
          f"{ra['valid_to']} vs {rb['valid_from']}")
    check("B.supersedes=A.id", rb["supersedes"] == A.id)
    check("B.version = A.version + 1", rb["version"] == ra["version"] + 1,
          f"A.v={ra['version']} B.v={rb['version']}")
    check("B.confidence = A.confidence × 0.9", abs(rb["confidence"] - ra["confidence"] * 0.9) < 1e-6,
          f"B={rb['confidence']}")
    check("B наследует importance", rb["importance"] == 4, str(rb["importance"]))
    check("B наследует user_id", rb["user_id"] == "u_acc")
    check("B metadata merge (src+extra)", rb["metadata"].get("src") == "doc1" and rb["metadata"].get("extra") == "x",
          str(rb["metadata"]))
    check("B.frozen=False", rb["frozen"] is False)
    check("B.namespace = A.namespace", B.namespace == A.namespace == "code_knowledge")
    check("Qdrant: старая помечена superseded",
          any(c.kwargs.get("payload") == {"status": "superseded"} and c.kwargs.get("point_id") == A.id
              for c in qdrant.set_payload.call_args_list))
    check("Qdrant: новая точка upsert", any(
        c.kwargs.get("point_id") == B.id for c in qdrant.upsert_vector.call_args_list))

    h = await service.get_history(A.id)
    check("get_history(A) = [A, B] от старейшей", [i.id for i in h.items] == [A.id, B.id],
          str([i.id for i in h.items]))
    check("get_history(A).current_id = B", h.current_id == B.id)
    h2 = await service.get_history(B.id)
    check("get_history(B) — та же цепочка", [i.id for i in h2.items] == [A.id, B.id])

    C = await service.create_version(B.id, "Postgres слушит порт 5434, миграция завершена")
    h3 = await service.get_history(C.id)
    check("цепочка C: [A, B, C]", [i.id for i in h3.items] == [A.id, B.id, C.id],
          str([i.id for i in h3.items]))
    check("current_id = C", h3.current_id == C.id)
    check("C.confidence = 0.9 × 0.9 = 0.81",
          abs((await val("SELECT confidence FROM memories WHERE id=$1", C.id)) - 0.81) < 1e-6)

    # ══ (б) Конфликты ═════════════════════════════════════════════
    print("\n── (б) Конфликты ──")
    try:
        await service.create_version(C.id, "Postgres слушит порт 5434, миграция завершена")
        check("идентичный контент → ConflictError", False, "исключение не поднято")
    except ConflictError as e:
        check("идентичный контент → ConflictError", "identical" in str(e), str(e))
    try:
        await service.create_version(A.id, "совсем новый контент поверх закрытой")
        check("superseded база → ConflictError", False, "исключение не поднято")
    except ConflictError as e:
        check("superseded база → ConflictError", "asserted" in str(e), str(e))
    R, _ = await service.store("Временная гранула на отзыв", "u_acc", namespace="code_knowledge")
    await service.retract(R.id, reason="test")
    try:
        await service.create_version(R.id, "версия поверх отозванной")
        check("retracted база → ConflictError", False, "исключение не поднято")
    except ConflictError as e:
        check("retracted база → ConflictError", True, str(e))
    try:
        await service.create_version("00000000-0000-0000-0000-00000000dead", "нет такой")
        check("несуществующая → NotFoundError", False, "не поднято")
    except NotFoundError:
        check("несуществующая → NotFoundError", True)

    # Краш-тест: контент новой версии == другой АКТИВНОЙ грануле того же ns.
    # Прод-условие: dedup_enabled=True всегда пишет content_hash — эмулируем SQL-ом.
    Other, _ = await service.store("Уникальный факт о сети", "u_acc", namespace="code_knowledge")
    import hashlib as _hl
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET content_hash=$2 WHERE id=$1",
            Other.id, _hl.sha256("Уникальный факт о сети".encode()).hexdigest())
    Base, _ = await service.store("База для краш-теста версии", "u_acc", namespace="code_knowledge")
    try:
        await service.create_version(Base.id, "Уникальный факт о сети")
        check("версия с контентом другой активной гранулы → конфликт-ответ",
              False, "прошло без исключения")
    except ConflictError as e:
        check("версия с контентом другой активной гранулы → конфликт-ответ", True, str(e))
    except Exception as e:
        # BUG#2: сырое asyncpg.UniqueViolationError вместо ConflictError — тул отдаст 500
        check("версия с контентом другой активной гранулы → конфликт-ответ [BUG#2]",
              False, f"сырое исключение {type(e).__name__}: {str(e)[:120]}")

    # ══ (в) Retract с reason ══════════════════════════════════════
    print("\n── (в) Retract ──")
    D, _ = await service.store("Факт для отзыва с причиной", "u_acc", namespace="code_knowledge")
    ok = await service.retract(D.id, reason="устарело после аудита")
    check("retract вернул True", ok is True)
    rd = await row("SELECT status, valid_to, metadata FROM memories WHERE id=$1", D.id)
    check("D.status=retracted", rd["status"] == "retracted")
    check("D.valid_to проставлен", rd["valid_to"] is not None)
    check("metadata.reason сохранён", rd["metadata"].get("reason") == "устарело после аудита",
          str(rd["metadata"]))
    vis = await pg.fetch_by_ids([D.id], include_historical=True)
    hid = await pg.fetch_by_ids([D.id], include_historical=False)
    check("fetch include_historical=True видит", len(vis) == 1)
    check("fetch include_historical=False скрывает", len(hid) == 0)
    again = await service.retract(D.id, reason="повтор")
    check("повторный retract → False (идемпотентность ответа)", again is False)

    # ══ (г) Freeze ════════════════════════════════════════════════
    print("\n── (г) Freeze ──")
    F, _ = await service.store("Замороженный вечный факт", "u_acc", namespace="code_knowledge")
    G, _ = await service.store("Обычная гранула рядом", "u_acc", namespace="code_knowledge")
    fr = await service.freeze(F.id, True)
    check("freeze вернул запись с frozen=True", fr.frozen is True)
    cf0 = await val("SELECT confidence FROM memories WHERE id=$1", F.id)
    cg0 = await val("SELECT confidence FROM memories WHERE id=$1", G.id)
    touched = await service.decay_confidence()
    cf1 = await val("SELECT confidence FROM memories WHERE id=$1", F.id)
    cg1 = await val("SELECT confidence FROM memories WHERE id=$1", G.id)
    check("decay не трогает frozen", abs(cf1 - cf0) < 1e-9, f"{cf0} → {cf1}")
    check("decay уменьшает обычную (rate 0.995)", abs(cg1 - cg0 * 0.995) < 1e-6,
          f"{cg0} → {cg1}")
    check("decay возвращает счёт по namespace", touched.get("code_knowledge", 0) > 0, str(touched))
    # floor: не сползает ниже 0.1
    await val("UPDATE memories SET confidence=0.1005 WHERE id=$1", G.id)
    await service.decay_confidence()
    cg2 = await val("SELECT confidence FROM memories WHERE id=$1", G.id)
    check("floor 0.1 держит (confidence>floor затронут, но не ниже floor)",
          cg2 >= 0.1 - 1e-9, f"0.1005 → {cg2}")

    # ══ (д) GC superseded ═════════════════════════════════════════
    print("\n── (д) GC superseded ──")
    # сид: 5 superseded = S1,S2,S3 старые не-frozen; S4 свежая; S5 старая frozen
    seeds = {}
    for name in ("s1", "s2", "s3", "s4", "s5"):
        base, _ = await service.store(f"GC-сид {name} базовая версия", "u_acc",
                                      namespace="code_knowledge")
        new = await service.create_version(base.id, f"GC-сид {name} новая версия")
        seeds[name] = (base.id, new.id)
    # связи до GC
    X, _ = await service.store("GC-наблюдатель X", "u_acc", namespace="code_knowledge")
    rel_named = await service.add_relation(X.id, target_id=seeds["s1"][0],
                                           link_type="references", target_name="s1 entity")
    rel_anon = await service.add_relation(X.id, target_id=seeds["s2"][0],
                                          link_type="references")
    rel_out = await service.add_relation(seeds["s3"][0], target_id=X.id, link_type="related_to")
    check("связи сида созданы", all([rel_named, rel_anon, rel_out]))

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET updated_at = now() - interval '100 days' WHERE id = ANY($1::uuid[])",
            [seeds[n][0] for n in ("s1", "s2", "s3", "s5")])
        await conn.execute("UPDATE memories SET frozen = true WHERE id=$1", seeds["s5"][0])

    # dry-run (config по умолчанию gc_dry_run=True)
    res = await service.gc_superseded()
    check("dry-run: selected=3 (s1,s2,s3)", res == {"dry_run": True, "selected": 3, "deleted": 0},
          str(res))
    alive = await val("SELECT count(*) FROM memories WHERE id = ANY($1::uuid[])",
                      [seeds[n][0] for n in seeds])
    check("dry-run: ничего не удалено (5/5 на месте)", alive == 5, str(alive))
    check("dry-run: Qdrant purge не вызван", not qdrant.delete.called)

    # real run
    service.config.gc_dry_run = False
    res = await service.gc_superseded()
    check("real: deleted=3", res == {"dry_run": False, "selected": 3, "deleted": 3}, str(res))
    left = [n for n in seeds if await val(
        "SELECT EXISTS(SELECT 1 FROM memories WHERE id=$1)", seeds[n][0])]
    check("real: удалены s1,s2,s3; живы s4(свежая),s5(frozen)",
          sorted(left) == ["s4", "s5"], str(left))
    named = await row("SELECT target_id::text IS NULL AS tnull, target_name FROM relations WHERE id=$1", rel_named)
    check("relations: named-target жива, target_id → NULL", named is not None and named["tnull"] and named["target_name"] == "s1 entity",
          str(named))
    anon_exists = await val("SELECT EXISTS(SELECT 1 FROM relations WHERE id=$1)", rel_anon)
    check("relations: анонимная target-связь удалена", not anon_exists)
    out_exists = await val("SELECT EXISTS(SELECT 1 FROM relations WHERE id=$1)", rel_out)
    check("relations: source-связь s3→X снята каскадом", not out_exists)
    check("Qdrant: purge зафиксирован (мок)", qdrant.delete.called and sorted(
        c.kwargs.get("point_ids", []) for c in qdrant.delete.call_args_list)[-1].__len__() >= 3,
          str(qdrant.delete.call_args_list[-1].kwargs.get("point_ids", []))[:120])
    # история не потерялась: get_history(s4-new) всё ещё содержит базу
    h4 = await service.get_history(seeds["s4"][1])
    check("GC не тронул живые цепочки: history(s4-new)=2", len(h4.items) == 2)
    # идемпотентность
    res2 = await service.gc_superseded()
    check("повтор real GC → selected=0 (идемпотентность)", res2["selected"] == 0 and res2["deleted"] == 0, str(res2))

    await pool.close()
    print(f"\n=== ИТОГ (сценарии а-д): PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("ПРОВАЛЫ:", *FAIL, sep="\n  - ")
        sys.exit(1)


asyncio.run(main())
