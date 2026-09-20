"""Линкер V3 (ADR-019 C) — автолинкинг графа знаний.

Трёхслойная пирамида с непересекающимися зонами cosine:
  * L1a synonym  [0.80, 0.85)      — related_to weight=score, БЕЗ LLM (HippoRAG 2);
  * L2 verdict   [0.85, dedup[ns]) — ОДИН LLM-вызов на гранулу, ≤5 кандидатов
                                      (link / duplicate / contradiction / none);
  * dedup-зона   ≥ dedup[ns]       — территория DedupEngine, линкер не ходит.
  * L1c co-occurrence — без cosine: один project+namespace+session_id
    → related_to 0.5, кап linker_cooccurrence_cap на гранулу.

Резолв имён (V3.2): lateral в sync-путях (queries.py) + beat-кампания
name_reconciler для отложенных висяков. Владение рёбрами — metadata
source="linker_v3" (третий владелец наряду с Тишью и ручными; НЕ
metadata.links и НЕ synced_from — их сотрёт следующий sync).

Store не дорожает: link_new_granule выполняется асинхронной Celery-задачей
после commit; L2-кандидаты складываются в Redis-очередь, beat-воркер
l2_verdicts забирает пачками. Verdict-cache — Redis, ключ канонической
пары + хэши контентов (ADR-019 C: TTL 30 дней; правка гранулы = другой
хэш = инвалидация автоматически).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from memory_server.config import Settings
from memory_server.db import queries as q
from memory_server.llm_client import (
    GranuleText,
    LinkerLLMClient,
    Verdict,
    VerdictParseError,
)
from memory_server.logger import get_logger
from memory_server.memory.qdrant_store import QdrantStore
from memory_server.metrics import (
    LINKER_L2_QUEUE_SIZE,
    LINKER_LINKS_CREATED_TOTAL,
    LINKER_LLM_VERDICTS_TOTAL,
    LINKER_NAMES_RESOLVED_TOTAL,
)

logger = get_logger(__name__)

# ── CNLM-матрица (CHANGELOG-auto-linker-cnlm.md v1.1.0): валидные типы
# рёбер для cross-namespace пар. Intra-ns — любой тип; пары вне матрицы —
# только нейтральный related_to (прецедент не задал семантику — не выдумываем).
CROSS_NS_ALLOWED: dict[tuple[str, str], frozenset[str]] = {
    ("project_meta", "code_knowledge"): frozenset(
        {"implements_adr", "solves", "motivates"}
    ),
    ("dialogue_insights", "code_knowledge"): frozenset(
        {"solves", "derived_from", "motivates"}
    ),
    ("user_facts", "code_knowledge"): frozenset({"motivates", "derived_from"}),
    ("project_meta", "dialogue_insights"): frozenset({"informed_by"}),
    ("dialogue_insights", "project_meta"): frozenset({"motivates", "informed_by"}),
    ("user_facts", "project_meta"): frozenset({"motivates", "derived_from"}),
}


def validate_link_type(src_ns: str, dst_ns: str, link_type: str | None) -> str | None:
    """CNLM-фильтр: допустим ли link_type для пары namespace'ов.

    None → пара не допускает этот тип; вызывающий деградирует в related_to.
    related_to нейтрален и допускается всегда (fallback v1.1.0).
    """
    if link_type is None:
        return None
    if link_type == "related_to":
        return link_type
    if src_ns == dst_ns:
        return link_type
    return link_type if link_type in CROSS_NS_ALLOWED.get((src_ns, dst_ns), frozenset()) else None


# Redis-ключи линкера: очередь L2, verdict-cache, счётчики вердиктов/кеша.
_L2_QUEUE_KEY = "linker:l2q"
_VERDICT_CACHE_PREFIX = "linker:vdc"
_COUNTER_VERDICT_PREFIX = "linker:c:verdict"
_COUNTER_CACHE_HIT = "linker:c:cache:hit"
_COUNTER_CACHE_MISS = "linker:c:cache:miss"


def verdict_cache_key(a_id: str, b_id: str, a_hash: str | None, b_hash: str | None) -> str:
    """Ключ кеша вердикта: каноническая пара (min/max) + хэши контентов.

    Хэши в ключе = автоматическая инвалидация при изменении любой стороны
    (ADR-019 C: (a_id, b_id, hash(a), hash(b))), канонизация — встречные
    пары a→b / b→a делят одну запись: id И хэши сортируются согласованно
    (баг приёмки В3: иначе b→a давал промах и второй LLM-вызов).
    """
    (lo, lo_hash), (hi, hi_hash) = sorted(
        ((a_id, a_hash or "-"), (b_id, b_hash or "-")), key=lambda t: t[0]
    )
    return f"{_VERDICT_CACHE_PREFIX}:{lo}:{hi}:{lo_hash}:{hi_hash}"


class Linker:
    """Ядро Линкера V3: ANN-слои, резолв имён, LLM-вердикты, статистика."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        qdrant: QdrantStore | None,
        redis_provider: Callable[[], Awaitable[Any]] | None,
        config: Settings | None = None,
        llm: LinkerLLMClient | None = None,
    ) -> None:
        self.pool = pool
        self.qdrant = qdrant
        self.redis_provider = redis_provider
        self.config = config or Settings()
        self.llm = llm
        if self.config.linker_verdict_threshold <= self.config.linker_synonym_threshold:
            logger.warning(
                "linker: zone thresholds misconfigured (verdict <= synonym), "
                "L1a auto zone is empty (only L2 verdict zone would remain)",
                extra={
                    "synonym": self.config.linker_synonym_threshold,
                    "verdict": self.config.linker_verdict_threshold,
                },
            )

    # ── Общие помощники ──

    def l2_enabled(self) -> bool:
        """L2 включён ⇔ LLM-провайдер настроен (непустой base_url)."""
        return self.llm is not None

    def _upper_bound(self, namespace: str) -> float:
        """Верхняя граница зоны линкера = порог дедупа namespace.

        Выше — территория DedupEngine: слои не пересекаются ни в одном ns
        (dialogue_insights дедупит с 0.85 — там серая зона L2 пуста by design).
        """
        return self.config.dedup_thresholds.get(namespace, self.config.dedup_threshold)

    async def _get_redis(self) -> Any | None:
        """Redis-клиент или None (деградация: без кеша, без очереди)."""
        if self.redis_provider is None:
            return None
        try:
            return await self.redis_provider()
        except Exception:
            logger.warning("linker: redis unavailable, degrading")
            return None

    async def _fetch_granules(self, conn: asyncpg.Connection, ids: list[str]) -> dict[str, GranuleText]:
        """Батч-загрузка участников вердикта (только актуальные)."""
        rows = await conn.fetch(q.SELECT_GRANULES_FOR_LINKER, ids)
        return {
            str(row["id"]): GranuleText(
                granule_id=str(row["id"]),
                title=row["entity_name"] or str(row["id"])[:8],
                content=row["content"],
                namespace=row["namespace"],
                content_hash=row["content_hash"],
            )
            for row in rows
        }

    async def _insert_link(
        self,
        conn: asyncpg.Connection,
        source_id: str,
        target_id: str,
        link_type: str,
        weight: float,
        metadata: dict[str, Any],
    ) -> bool:
        """INSERT линкер-ребра; False = ON CONFLICT погасил дубль."""
        row = await conn.fetchrow(
            q.INSERT_LINKER_RELATION,
            source_id,
            target_id,
            link_type,
            metadata.get("description"),
            weight,
            metadata,
        )
        return row is not None

    async def _bump_l2_queue_metric(self, redis: Any) -> None:
        """Gauge размера очереди L2 (best-effort)."""
        try:
            LINKER_L2_QUEUE_SIZE.set(await redis.llen(_L2_QUEUE_KEY))
        except Exception:
            pass

    # ── L1a + L1c + постановка в L2: новая гранула ──

    async def link_new_granule(self, granule_id: str) -> dict[str, Any]:
        """Автолинкинг новой гранулы (асинхронно после store, store НЕ дорожает).

        L1a: ANN соседей [synonym, verdict) → related_to weight=score.
        L2: соседи [verdict, dedup[ns]) → Redis-очередь вердиктов
        (не наполняется при выключенном LLM — сирот подберёт V3.4).
        L1c: соседи той же сессии → related_to 0.5.
        """
        report: dict[str, Any] = {"granule_id": granule_id, "l1a_created": 0, "l1c_created": 0, "l2_enqueued": 0}
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.SELECT_NEW_GRANULE_FOR_LINKER, granule_id)
            if row is None:
                # не asserted / удалена между store и задачей — не ошибка
                report["skipped"] = "not_asserted"
                return report

            await self._link_cooccurrence(conn, report, granule_id, row)

            if not (self.config.linker_l1a_enabled and self.qdrant is not None):
                return report
            vectors = self.qdrant.retrieve_vectors([granule_id])
            vector = vectors.get(granule_id)
            if vector is None:
                # рассинхрон PG ↔ Qdrant: вектора нет, ANN невозможен
                logger.warning("linker: no vector for granule, ANN skipped", extra={"id": granule_id})
                return report

            upper = self._upper_bound(row["ns_uid"])
            hits = self.qdrant.search_batch(
                query_vectors=[vector],
                limit=self.config.linker_ann_limit + 1,  # +1: HNSW вернёт саму точку
                score_threshold=self.config.linker_synonym_threshold,
                query_filter=QdrantStore.build_filter(namespace_id=row["ns_id"], active_only=True),
            )
            l2_candidates: list[dict[str, Any]] = []
            for hit in hits[0] if hits else []:
                cand_id = str(hit["id"])
                score = float(hit["score"])
                if cand_id == granule_id or score < self.config.linker_synonym_threshold:
                    continue
                if score >= upper:
                    continue  # dedup-зона: территория DedupEngine, не наша
                if score < self.config.linker_verdict_threshold:
                    created = await self._insert_link(
                        conn,
                        granule_id,
                        cand_id,
                        "related_to",
                        round(score, 6),
                        {"source": "linker_v3", "layer": "l1a"},
                    )
                    if created:
                        report["l1a_created"] += 1
                        LINKER_LINKS_CREATED_TOTAL.labels(layer="l1a").inc()
                elif len(l2_candidates) < self.config.linker_top_k:
                    l2_candidates.append({"id": cand_id, "score": round(score, 6)})

            if l2_candidates and self.l2_enabled():
                redis = await self._get_redis()
                if redis is not None:
                    try:
                        await redis.lpush(
                            _L2_QUEUE_KEY,
                            json.dumps({"granule_id": granule_id, "candidates": l2_candidates, "attempts": 0}),
                        )
                        report["l2_enqueued"] = len(l2_candidates)
                        await self._bump_l2_queue_metric(redis)
                    except Exception:
                        logger.warning("linker: l2 enqueue failed (non-fatal)", extra={"id": granule_id})
        return report

    async def _link_cooccurrence(
        self,
        conn: asyncpg.Connection,
        report: dict[str, Any],
        granule_id: str,
        row: asyncpg.Record,
    ) -> None:
        """L1c: соседи той же сессии → related_to 0.5 (кап config, свежие)."""
        if not self.config.linker_l1c_enabled or not row["sid"]:
            return
        created = await conn.fetch(
            q.INSERT_COOCCURRENCE_LINKS,
            granule_id,
            row["project_id"],
            row["ns_id"],
            row["sid"],
            self.config.linker_cooccurrence_cap,
        )
        report["l1c_created"] = len(created)
        if created:
            LINKER_LINKS_CREATED_TOTAL.labels(layer="l1c").inc(len(created))

    # ── Кампания name_reconciler (V3.2, ADR-019 C L3) ──

    async def run_name_reconciler(self, dry_run: bool | None = None) -> dict[str, Any]:
        """Батчевый резолв висячих target_name (приоритет ADR-017 A.1).

        Идемпотентность: UPDATE только WHERE target_id IS NULL — повторный
        прогон не создаёт дублей; уникальность каноничного ребра — NOT EXISTS
        в CTE. dry_run (дефолт True до ручной первой кампании) считает, не
        пишет. Отчёт: сколько разрешено / осталось висячих.
        """
        if dry_run is None:
            dry_run = self.config.linker_reconciler_dry_run
        async with self.pool.acquire() as conn:
            pending_total = await conn.fetchval(q.COUNT_PENDING_TARGET_NAMES)
            if dry_run:
                would_resolve = await conn.fetchval(
                    q.RESOLVE_PENDING_TARGET_NAMES_DRY, self.config.linker_reconciler_batch
                )
                report = {
                    "dry_run": True,
                    "resolved": 0,
                    "would_resolve": would_resolve or 0,
                    "pending": pending_total or 0,
                    "batch": self.config.linker_reconciler_batch,
                }
                logger.info("name_reconciler: dry-run report", extra=report)
                return report

            resolved_total = 0
            while True:
                rows = await conn.fetch(
                    q.RESOLVE_PENDING_TARGET_NAMES, self.config.linker_reconciler_batch
                )
                if not rows:
                    break
                resolved_total += len(rows)
            remaining = await conn.fetchval(q.COUNT_PENDING_TARGET_NAMES)
            report = {
                "dry_run": False,
                "resolved": resolved_total,
                "pending": remaining or 0,
                "batch": self.config.linker_reconciler_batch,
            }
            if resolved_total:
                LINKER_NAMES_RESOLVED_TOTAL.labels(path="reconciler").inc(resolved_total)
            logger.info("name_reconciler: done", extra=report)
            return report

    # ── Кампания co-occurrence (L1c для исторического корпуса) ──

    async def run_co_occurrence(self, batch: int | None = None) -> dict[str, Any]:
        """Beat-кампания L1c: гранулы с session_id без l1c-рёбер, батчами.

        Выборка идемпотентна: после первого прогона гранула получает l1c-ребро
        (есть соседи ⇒ ребро будет) и выпадает из пула кандидатов.
        """
        limit = batch or self.config.linker_reconciler_batch
        created_total = 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SELECT_COOCCURRENCE_CANDIDATES, limit)
            for row in rows:
                created = await conn.fetch(
                    q.INSERT_COOCCURRENCE_LINKS,
                    row["id"],
                    row["project_id"],
                    row["namespace_id"],
                    row["session_id"],
                    self.config.linker_cooccurrence_cap,
                )
                created_total += len(created)
            if created_total:
                LINKER_LINKS_CREATED_TOTAL.labels(layer="l1c").inc(created_total)
        report = {"candidates": len(rows), "links_created": created_total}
        logger.info("co_occurrence: done", extra=report)
        return report

    # ── L2: воркер вердиктов ──

    async def run_l2_verdicts(self) -> dict[str, Any]:
        """Забрать батч из Redis-очереди → вердикты → рёбра/кеш.

        LLM выключен ⇒ очередь не разбирается (кандидаты ждут включения,
        сирот дорезолвит V3.4 orphan_linker). Сетевой сбой LLM ⇒ элемент
        возвращается в очередь (до linker_l2_max_attempts). Битый JSON ⇒
        none для всех кандидатов вызова (метрика error) — не зацикливаем.
        """
        if not self.l2_enabled():
            return {"ok": False, "reason": "llm_disabled", "processed": 0}
        redis = await self._get_redis()
        if redis is None:
            return {"ok": False, "reason": "redis_unavailable", "processed": 0}

        report: dict[str, Any] = {"ok": True, "processed": 0, "links_created": 0}
        verdict_counts: dict[str, int] = {}
        requeued_items: list[str] = []
        dropped = 0
        for _ in range(self.config.linker_l2_batch):
            raw = None
            try:
                raw = await redis.rpop(_L2_QUEUE_KEY)
            except Exception:
                logger.warning("linker: queue read failed, stopping batch")
                break
            if raw is None:
                break
            item = json.loads(raw)
            item["attempts"] = item.get("attempts", 0) + 1
            outcome = await self._verdict_one(redis, item)
            if outcome == "requeue":
                # Возврат в очередь ПОСЛЕ цикла: сбойный элемент не съедает
                # весь батч повторами в том же прогоне
                if item["attempts"] < self.config.linker_l2_max_attempts:
                    requeued_items.append(json.dumps(item))
                else:
                    dropped += 1
                    logger.warning(
                        "linker: l2 item dropped after max attempts",
                        extra={"granule_id": item.get("granule_id")},
                    )
                continue
            report["processed"] += 1
            for kind, count in (outcome or {}).items():
                verdict_counts[kind] = verdict_counts.get(kind, 0) + count
            report["links_created"] += (outcome or {}).get("link", 0) + (outcome or {}).get(
                "contradiction", 0
            )

        for payload in requeued_items:
            await redis.lpush(_L2_QUEUE_KEY, payload)
        await self._bump_l2_queue_metric(redis)
        report["verdicts"] = verdict_counts
        report["requeued"] = len(requeued_items)
        report["dropped"] = dropped
        if report["processed"]:
            logger.info("l2_verdicts: done", extra=report)
        return report

    async def _verdict_one(self, redis: Any, item: dict[str, Any]) -> dict[str, int] | str:
        """Один элемент очереди: кеш → LLM → применение. Счёт вердиктов.

        'requeue' — транспортный сбой LLM (элемент вернётся в очередь).
        """
        granule_id: str = item["granule_id"]
        candidate_specs: list[dict[str, Any]] = item.get("candidates", [])
        counts: dict[str, int] = {}
        async with self.pool.acquire() as conn:
            granules = await self._fetch_granules(
                conn, [granule_id] + [str(c["id"]) for c in candidate_specs]
            )
            source = granules.get(granule_id)
            if source is None:
                return counts  # гранула закрылась пока ждала — не ошибка
            candidates = [
                granules[str(c["id"])] for c in candidate_specs if str(c["id"]) in granules
            ]
            if not candidates:
                return counts

            # Verdict-cache: хэши контентов в ключе → правка = инвалидация
            keys = [
                verdict_cache_key(source.granule_id, c.granule_id, source.content_hash, c.content_hash)
                for c in candidates
            ]
            cached: dict[str, Verdict | None] = {}
            try:
                raw_cache = await redis.mget(keys)
            except Exception:
                raw_cache = [None] * len(keys)
            to_ask: list[GranuleText] = []
            for cand, key, raw in zip(candidates, keys, raw_cache):
                verdict = Verdict.from_json(_load_json(raw)) if raw else None
                if verdict is not None:
                    cached[cand.granule_id] = verdict
                    await self._incr(redis, _COUNTER_CACHE_HIT)
                else:
                    to_ask.append(cand)
                    await self._incr(redis, _COUNTER_CACHE_MISS)

            cache_fresh = True
            if to_ask:
                try:
                    fresh = await self.llm.verdict_batch(source, to_ask)
                except VerdictParseError as exc:
                    # Битый JSON: none для всех + лог; ретрай бессмыслен.
                    # В кеш деградацию НЕ пишем (баг приёмки В2): мусорный
                    # ответ LLM не должен месяц закрывать линк пары.
                    logger.warning(
                        "linker: llm verdict unparsable, degrading to none",
                        extra={"granule_id": granule_id, "error": str(exc)},
                    )
                    LINKER_LLM_VERDICTS_TOTAL.labels(verdict="error").inc()
                    await self._incr(redis, f"{_COUNTER_VERDICT_PREFIX}:error")
                    fresh = {}
                    cache_fresh = False
                except Exception as exc:
                    # Таймаут/транспорт: элемент вернётся в очередь
                    logger.warning(
                        "linker: llm verdict transport failure, requeue",
                        extra={"granule_id": granule_id, "error": str(exc)},
                    )
                    return "requeue"
                for cand in to_ask:
                    verdict = fresh.get(cand.granule_id)
                    if verdict is None:
                        verdict = Verdict(verdict="none")
                    cached[cand.granule_id] = verdict
                    if not cache_fresh:
                        continue
                    try:
                        await redis.setex(
                            verdict_cache_key(
                                source.granule_id, cand.granule_id,
                                source.content_hash, cand.content_hash,
                            ),
                            self.config.linker_verdict_cache_ttl,
                            json.dumps(verdict.to_json()),
                        )
                    except Exception:
                        pass  # кеш опционален

            for cand in candidates:
                verdict = cached.get(cand.granule_id) or Verdict(verdict="none")
                counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
                LINKER_LLM_VERDICTS_TOTAL.labels(verdict=verdict.verdict).inc()
                await self._incr(redis, f"{_COUNTER_VERDICT_PREFIX}:{verdict.verdict}")
                created = await self._apply_verdict(conn, source, cand, verdict)
                if created:
                    LINKER_LINKS_CREATED_TOTAL.labels(layer="l2").inc()
            return counts

    async def _apply_verdict(
        self,
        conn: asyncpg.Connection,
        source: GranuleText,
        candidate: GranuleText,
        verdict: Verdict,
    ) -> bool:
        """Вердикт → ребро. duplicate: авто-supersede ЗАПРЕЩЁН без человека (WARN)."""
        if verdict.verdict == "duplicate":
            logger.warning(
                "linker: LLM duplicate verdict (manual merge candidate, "
                "auto-supersede NOT started)",
                extra={"source": source.granule_id, "duplicate": candidate.granule_id},
            )
            return False
        if verdict.verdict == "none":
            return False
        if verdict.verdict == "link":
            link_type = (
                validate_link_type(source.namespace, candidate.namespace, verdict.link_type)
                or "related_to"
            )
        else:  # contradiction
            link_type = "contradicts"
        return await self._insert_link(
            conn,
            source.granule_id,
            candidate.granule_id,
            link_type,
            round(verdict.confidence, 6),
            {
                "source": "linker_v3",
                "layer": "l2",
                "confidence": round(verdict.confidence, 6),
                "rationale": verdict.rationale,
            },
        )

    @staticmethod
    async def _incr(redis: Any, key: str) -> None:
        """Redis-счётчик для memory_linker_stats (переживает рестарт воркера)."""
        try:
            await redis.incr(key)
        except Exception:
            pass

    # ── Статистика (ADR-019 G, memory_linker_stats) ──

    async def stats(self) -> dict[str, Any]:
        """Read-only срез линкера: рёбра по слоям, имена, очередь, вердикты."""
        async with self.pool.acquire() as conn:
            layer_rows = await conn.fetch(q.SELECT_LINKER_LINK_STATS)
            name_row = await conn.fetchrow(q.SELECT_LINKER_NAME_STATS)
        links_by_layer = {row["layer"]: row["count"] for row in layer_rows}
        result: dict[str, Any] = {
            "ok": True,
            "links_by_layer": links_by_layer,
            "links_total": sum(links_by_layer.values()),
            "names_resolved": name_row["resolved"] if name_row else 0,
            "names_pending": name_row["pending"] if name_row else 0,
            "l2_enabled": self.l2_enabled(),
            "zones": {
                "l1a": [self.config.linker_synonym_threshold, self.config.linker_verdict_threshold],
                # Верхняя граница per-namespace = dedup_thresholds[ns]; для
                # обзора показываем самый консервативный (минимальный) порог
                # (баг приёмки М5: вместо строкового плейсхолдера — число).
                "l2": [
                    self.config.linker_verdict_threshold,
                    min(self.config.dedup_thresholds.values())
                    if self.config.dedup_thresholds
                    else self.config.dedup_threshold,
                ],
            },
        }
        redis = await self._get_redis()
        if redis is not None:
            try:
                verdict_names = ("link", "duplicate", "contradiction", "none", "error")
                verdict_keys = [f"{_COUNTER_VERDICT_PREFIX}:{v}" for v in verdict_names]
                values = await redis.mget(verdict_keys + [_COUNTER_CACHE_HIT, _COUNTER_CACHE_MISS])
                result["verdicts"] = {
                    v: int(n or 0) for v, n in zip(verdict_names, values[:5])
                }
                result["verdict_cache"] = {
                    "hits": int(values[5] or 0),
                    "misses": int(values[6] or 0),
                }
                result["l2_queue_size"] = await redis.llen(_L2_QUEUE_KEY)
            except Exception:
                result["verdicts"] = {}
                result["verdict_cache"] = {"hits": 0, "misses": 0}
                result["l2_queue_size"] = None
        return result


def _load_json(raw: Any) -> Any:
    """Redis-значение кеша → объект; битое → None (промах)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
