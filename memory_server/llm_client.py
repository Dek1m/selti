"""Linker L2 LLM-клиент (ADR-019 C, V3.3) — OpenAI-совместимый вердикт.

Тот же класс внешнего API, что embedding-провайдер (клиент по образцу
EmbeddingClient: ленивый httpx.AsyncClient, привязка к event loop, aclose),
но чат-эндпоинт: один вызов на гранулу решает судьбу всех кандидатов
серой зоны 0.85–dedup (паттерн Graphiti: дубликат+противоречие+тип за
один прогон). Строгий JSON на выходе; битый JSON — VerdictParseError,
вызывающая сторона деградирует в none (LLM-отказ не роняет очередь).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, get_args

import httpx

from memory_server.logger import get_logger
from memory_server.metrics import LINKER_LLM_LATENCY_SECONDS
from memory_server.models import LinkType

logger = get_logger(__name__)

# Вердикты L2 (ADR-019 C): link — конкретный тип ребра; duplicate — сигнал
# слияния (авто-supersede ЗАПРЕЩЁН без человека — WARN и метка в отчёте);
# contradiction — ребро contradicts; none — не связано.
VerdictKind = Literal["link", "duplicate", "contradiction", "none"]

VERDICT_KINDS = frozenset(get_args(VerdictKind))
_LINK_TYPES = frozenset(get_args(LinkType))

# Вырезка первого сбалансированного {...}-блока: модели любят оборачивать
# JSON в ```json-заборы и сопроводительный текст.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_VERDICT_PROMPT_SYSTEM = (
    "You are a knowledge-graph linker. For the SOURCE granule and each CANDIDATE "
    "granule decide the relation. Respond with STRICT JSON only, no markdown:\n"
    '{"verdicts": [{"id": "<candidate id>", "verdict": "link|duplicate|contradiction|none", '
    '"link_type": "<one of the allowed types or null>", "confidence": <0.0-1.0>, '
    '"rationale": "<short reason>"}]}\n'
    "Rules:\n"
    "- duplicate: candidate restates the same fact as the source (different wording);\n"
    "- contradiction: candidate asserts the opposite of the source;\n"
    "- link: candidate is genuinely related; pick the most specific link_type "
    "from: " + ", ".join(sorted(_LINK_TYPES)) + "; use null only with verdict none;\n"
    "- none: no meaningful relation. One verdict object per candidate id, no extras."
)


class VerdictParseError(Exception):
    """LLM-ответ не парсится в вердикты (битый JSON / пустой / не по формату)."""


@dataclass(frozen=True)
class GranuleText:
    """Участник вердикта: текст с заголовком (entity_name → голова контента)."""

    granule_id: str
    title: str
    content: str
    namespace: str
    content_hash: str | None = None


@dataclass(frozen=True)
class Verdict:
    """Вердикт по одной паре (источник, кандидат)."""

    verdict: VerdictKind
    link_type: str | None = None
    confidence: float = 0.0
    rationale: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Сериализация для verdict-cache (Redis)."""
        return {
            "verdict": self.verdict,
            "link_type": self.link_type,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "Verdict | None":
        """Десериализация из кеша; битая запись = промах (None)."""
        if not isinstance(raw, dict) or raw.get("verdict") not in VERDICT_KINDS:
            return None
        link_type = raw.get("link_type")
        if link_type is not None and link_type not in _LINK_TYPES:
            link_type = None
        try:
            confidence = min(max(float(raw.get("confidence", 0.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            verdict=raw["verdict"],
            link_type=link_type,
            confidence=confidence,
            rationale=raw.get("rationale"),
        )


def _clip(text: str, max_chars: int = 2000) -> str:
    """Усечение выжимки пары (токен-бюджет промпта, ~500 токенов на сторону)."""
    flattened = " ".join((text or "").split())
    return flattened[:max_chars] if len(flattened) > max_chars else flattened


class LinkerLLMClient:
    """OpenAI-совместимый chat-клиент одного L2-вердикт-вызова на гранулу."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "glm-4.7-flash",
        timeout: float = 10.0,
        max_retries: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        # Fast path: клиент создан и loop тот же (воркеры Celery держат
        # persistent loop — паттерн EmbeddingClient)
        if self._client is not None and self._client_loop is asyncio.get_running_loop():
            return self._client
        async with self._client_lock:
            loop = asyncio.get_running_loop()
            if self._client is not None and self._client_loop is loop:
                return self._client
            if self._client is not None:
                try:
                    await self._client.aclose()
                except Exception as exc:
                    # Старый клиент не закрылся (мёртвый loop) — не блокируем замену
                    logger.warning(
                        "Linker LLM client close failed on loop switch",
                        extra={"error": str(exc), "error_type": type(exc).__name__},
                    )
                self._client = None
            self._client_loop = loop
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
            )
            return self._client

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    def _build_user_prompt(self, source: GranuleText, candidates: list[GranuleText]) -> str:
        parts = [
            f"SOURCE [{source.title or source.granule_id}] (namespace {source.namespace}):\n"
            f"{_clip(source.content)}"
        ]
        for cand in candidates:
            parts.append(
                f"CANDIDATE id={cand.granule_id} "
                f"[{cand.title or cand.granule_id}] (namespace {cand.namespace}):\n"
                f"{_clip(cand.content)}"
            )
        parts.append("Return the strict JSON verdicts now.")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> dict[str, Verdict]:
        """Тело ответа → {candidate_id: Verdict}. Битое — VerdictParseError."""
        raw_verdicts = payload.get("verdicts")
        if not isinstance(raw_verdicts, list) or not raw_verdicts:
            raise VerdictParseError(f"no verdicts array in response: {payload!r:.200}")
        verdicts: dict[str, Verdict] = {}
        for item in raw_verdicts:
            if not isinstance(item, dict):
                continue
            cand_id = str(item.get("id") or "")
            kind = item.get("verdict")
            if not cand_id or kind not in VERDICT_KINDS:
                continue
            link_type = item.get("link_type")
            if link_type is not None and link_type not in _LINK_TYPES:
                # Незнакомый тип — не отбрасываем пару, деградируем в нейтральный
                link_type = None
            try:
                confidence = min(max(float(item.get("confidence", 0.0)), 0.0), 1.0)
            except (TypeError, ValueError):
                confidence = 0.0
            verdicts[cand_id] = Verdict(
                verdict=kind,
                link_type=link_type if kind == "link" else None,
                confidence=confidence,
                rationale=str(item["rationale"]) if item.get("rationale") else None,
            )
        if not verdicts:
            raise VerdictParseError(f"no valid verdict objects in response: {payload!r:.200}")
        return verdicts

    async def verdict_batch(
        self, source: GranuleText, candidates: list[GranuleText]
    ) -> dict[str, Verdict]:
        """ОДИН вызов на гранулу: вердикты по всем кандидатам (≤ linker_top_k).

        Транспортные сбои (timeout, соединение) ретраятся max_retries раз,
        затем исключение наружу — элемент вернётся в очередь. Битый JSON —
        VerdictParseError (без ретрая: детерминированный мусор).
        """
        client = await self._get_client()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _VERDICT_PROMPT_SYSTEM},
                {"role": "user", "content": self._build_user_prompt(source, candidates)},
            ],
            "temperature": 0.0,
        }
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                response = await client.post("/chat/completions", json=body)
                LINKER_LLM_LATENCY_SECONDS.observe(time.monotonic() - started)
                if response.status_code != 200:
                    # 4xx/5xx — retryable на всякий случай (rate limit — 429)
                    last_exc = httpx.HTTPStatusError(
                        f"llm verdict HTTP {response.status_code}: {response.text[:200]}",
                        request=response.request,
                        response=response,
                    )
                    continue
                content = response.json()["choices"][0]["message"]["content"]
                match = _JSON_OBJECT_RE.search(content or "")
                if match is None:
                    raise VerdictParseError(f"no JSON object in LLM output: {(content or '')!r:.200}")
                return self._parse_response(json.loads(match.group(0)))
            except VerdictParseError:
                raise
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise VerdictParseError(f"malformed LLM response: {exc}") from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                LINKER_LLM_LATENCY_SECONDS.observe(time.monotonic() - started)
                last_exc = exc
        raise last_exc if last_exc else VerdictParseError("llm verdict failed without exception")
