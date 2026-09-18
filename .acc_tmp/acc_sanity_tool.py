# Sanity re-check Фазы 2: memory_get_history через MCP тул-слой (eager Celery,
# mock Qdrant + fake embedding в composition root, PG — приёмочный стенд 5433).
# env выставляется ДО импорта memory_server.
import os

os.environ["DATABASE_URL"] = "postgresql+asyncpg://svc_athene_ai:acctest@127.0.0.1:5433/memory"
os.environ["QDRANT_ENABLED"] = "false"

import asyncio
import hashlib
import sys
from unittest.mock import MagicMock

sys.path.insert(0, r"E:\Projects\Python\selti")

from memory_server.celery_app import app  # noqa: E402

app.conf.task_always_eager = True

# ШИМ (только в этом скрипте): send_task в eager-конфигурации Celery всё равно
# ходит в broker/result-backend; подменяем на task.apply — тул-слой и мост
# (celery_call → run_task → send_task → ready/failed/result) работают как есть.
_orig_send_task = app.send_task


def _eager_send_task(name, kwargs=None, **kw):
    task = app.tasks.get(name)
    if task is not None:
        return task.apply(kwargs=kwargs or {}, headers=kw.get("headers"))
    return _orig_send_task(name, kwargs=kwargs, **kw)


app.send_task = _eager_send_task

# регистрируем задачи (send_task по имени требует загруженных модулей)
import memory_server.tasks.memory_tasks  # noqa: E402,F401
import memory_server.tasks.lifecycle_tasks  # noqa: E402,F401

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


class FakeEmbedding:
    async def embed(self, text: str):
        h = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for b in h[:16]]

    async def embed_many(self, texts):
        return [await self.embed(t) for t in texts]

    async def aclose(self):
        pass


async def main():
    import asyncpg

    from memory_server.state import get_state

    # повторяемость: чистим сиды прошлых прогонов
    conn = await asyncpg.connect(
        "postgresql://svc_athene_ai:acctest@127.0.0.1:5433/memory")
    await conn.execute("DELETE FROM memories WHERE user_id = 'u_tool'")
    await conn.close()

    state = get_state()
    # «mock Qdrant»: ленивые синглтоны отдают подмену, не создавая реальных клиентов
    state._qdrant = MagicMock(name="qdrant-mock")
    state._embedding = FakeEmbedding()

    # сид: цепочка A → B → C через НАСТОЯЩИЙ composition-root сервис
    def seed():
        from memory_server.tasks.async_bridge import run_async
        svc = run_async(state.get_memory_service)
        a, _ = run_async(svc.store, "Sanity-тул A: слушает 5432", "u_tool",
                         namespace="code_knowledge")
        b = run_async(svc.create_version, granule_id=a.id,
                      new_content="Sanity-тул B: слушает 5433")
        c = run_async(svc.create_version, granule_id=b.id,
                      new_content="Sanity-тул C: слушает 5434")
        return [a.id, b.id, c.id]

    a_id, b_id, c_id = await asyncio.to_thread(seed)

    # вызов через MCP тул-слой: tool → celery_call → (eager) задача → service → PG
    from memory_server.tools.memory_tools import memory_get_history

    result = await memory_get_history(granule_id=c_id)

    check("тул вернул успешный ответ (200-эквивалент)", isinstance(result, dict), type(result).__name__)
    items = result.get("items", [])
    check("история [A, B, C] от старейшей",
          [i["id"] for i in items] == [a_id, b_id, c_id],
          str([i["id"] for i in items]))
    check("current_id = C", result.get("current_id") == c_id, str(result.get("current_id")))
    check("content в цепочке A→B→C по порядку",
          [i["content"].split(":")[0].strip() for i in items]
          == ["Sanity-тул A", "Sanity-тул B", "Sanity-тул C"],
          str([i["content"][:20] for i in items]))
    check("Qdrant-мок получил точки (upsert) и supersession-payload",
          state._qdrant.upsert.called and state._qdrant.set_payload.called,
          f"upsert={len(state._qdrant.upsert.call_args_list)} "
          f"set_payload={len(state._qdrant.set_payload.call_args_list)}")

    print(f"\n=== ИТОГ (тул-слой sanity): PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("ПРОВAЛЫ:", *FAIL, sep="\n  - ")
        sys.exit(1)


asyncio.run(main())
