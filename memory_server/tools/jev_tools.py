"""MCP tools for System One decisions (Open-Jev-2B, приказ Мастера 23.09).

Мост к decision-сервису — три примитива решений с калиброванными
вероятностями для агентов (choice / noul / score). Сервис stateless и
детерминирован на фиксированных потоках: латентность 7-8с на CPU →
таймаут 60с, БЕЗ ретраев (повтор не меняет ответ, а меняет FP-порядок).
Ошибка сервиса — JevServiceError наружу: агент обязан видеть отказ
рефлекс-слоя, а не молчаливый дефолт. Валидация входов (лимиты
серверного validator'а) — ДО похода в сеть.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastmcp import Context

from memory_server.config import settings
from memory_server.exceptions import JevServiceError
from memory_server.logger import get_logger
from memory_server.server import mcp
from memory_server.utils.metrics_decorator import tool_handler

logger = get_logger(__name__)

# Транспортный таймаут одного запроса: инференс на CPU 7-8с, запас на
# загрузку; ретраев нет (детерминизм — повтор меняет FP-порядок потоков).
JEV_TIMEOUT_SECONDS = 60.0

# Лимиты серверного validator'а System One — отсекаем мусор до сети
_STATE_MAX_CHARS = 4000
_INSTRUCTIONS_MAX_CHARS = 1000

# qid в wire-формате: один тул = один вопрос; осмысленные имена для логов
_QID_DECIDE = "decision"
_QID_JUDGE = "judgement"
_QID_RATE = "rating"

# Канонические критерии noul (референс CLI): wire-ключи строго true/false,
# кастомные лейблы тулa — только интерпретация ответа, не критерии
_NOUL_CRITERIA = {
    "true": "condition holds true",
    "false": "condition does not hold",
}


class JevClient:
    """Ленивый httpx-клиент POST /v1/systemone (по образцу LinkerLLMClient).

    Привязка к event loop: воркеры держат persistent loop, при смене
    (тесты) клиент пересоздаётся — старый закрывается с WARNING.
    """

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        # Fast path: клиент создан и loop тот же
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
                        "Jev client close failed on loop switch",
                        extra={"error": str(exc), "error_type": type(exc).__name__},
                    )
                self._client = None
            self._client_loop = loop
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(JEV_TIMEOUT_SECONDS),
            )
            return self._client

    async def ask(self, state: str, qid: str, question: dict[str, Any]) -> dict[str, Any]:
        """Один вопрос по state → ответ-блок answers[qid]. Отказ — JevServiceError."""
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        client = await self._get_client()
        try:
            response = await client.post(
                "/v1/systemone",
                json={"state": state, "questions": {qid: question}},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise JevServiceError(
                f"decision service unreachable at {self.base_url}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise JevServiceError(
                f"decision service HTTP {response.status_code}: {response.text[:200]}"
            )
        answers = response.json().get("answers")
        block = answers.get(qid) if isinstance(answers, dict) else None
        if not isinstance(block, dict):
            raise JevServiceError(
                f"decision service returned no answer for '{qid}': {response.text[:200]}"
            )
        return block

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None


_jev_client: JevClient | None = None


def get_jev_client() -> JevClient:
    """Процессный синглтон из фундаментных настроек env-only."""
    global _jev_client
    if _jev_client is None:
        _jev_client = JevClient(
            base_url=settings.jev_base_url, api_key=settings.jev_access_key
        )
    return _jev_client


async def close_jev_client() -> None:
    """Закрытие httpx-клиента на shutdown (server.py lifespan); no-op без клиента."""
    global _jev_client
    if _jev_client is not None:
        try:
            await _jev_client.aclose()
        except Exception as exc:
            logger.warning(
                "Jev client close failed",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
        _jev_client = None


# ══════════════════════════════════════════════════════════════════
# Валидация (лимиты серверного validator'а System One)
# ══════════════════════════════════════════════════════════════════


def _validate_prompt(state: str, instructions: str) -> None:
    if not isinstance(state, str) or not state.strip() or len(state) > _STATE_MAX_CHARS:
        raise ValueError(f"state must be a non-empty string of 1..{_STATE_MAX_CHARS} chars")
    if (
        not isinstance(instructions, str)
        or not instructions.strip()
        or len(instructions) > _INSTRUCTIONS_MAX_CHARS
    ):
        raise ValueError(
            f"instructions must be a non-empty string of 1..{_INSTRUCTIONS_MAX_CHARS} chars"
        )


def _validate_choices(choices: dict[str, str]) -> dict[str, str]:
    """choices тула → wire-criteria; пустой desc = имя (паттерн CLI)."""
    if not isinstance(choices, dict) or not 2 <= len(choices) <= 8:
        got = len(choices) if isinstance(choices, dict) else type(choices).__name__
        raise ValueError(f"choices must be an object of 2..8 options, got {got}")
    criteria: dict[str, str] = {}
    for name, desc in choices.items():
        key = (name or "").strip()
        if not key:
            raise ValueError("choice names must be non-empty strings")
        criteria[key] = (desc or "").strip() or key
    if len(criteria) != len(choices):
        raise ValueError("choice names must be unique after trimming")
    return criteria


def _validate_levels(levels: list[str]) -> list[str]:
    if not isinstance(levels, list) or not 2 <= len(levels) <= 10:
        got = len(levels) if isinstance(levels, list) else type(levels).__name__
        raise ValueError(f"levels must be an array of 2..10 descriptions, got {got}")
    cleaned = [(level or "").strip() for level in levels]
    if not all(cleaned):
        raise ValueError("level descriptions must be non-empty strings")
    return cleaned


# ══════════════════════════════════════════════════════════════════
# System One tools
# ══════════════════════════════════════════════════════════════════


@mcp.tool()
@tool_handler("jev_decide")
async def jev_decide(
    state: str,
    instructions: str,
    choices: dict[str, str],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Choose one option for the situation with calibrated probabilities.

    System One reflex (Open-Jev-2B): fast decision layer for agents.
    state: the situation to decide on (1..4000 chars).
    instructions: the question to answer (1..1000 chars).
    choices: {name: description} of 2..8 options; empty description = name.
    Returns {choice, probabilities, confidence} — confidence is top1-top2.
    """
    _validate_prompt(state, instructions)
    criteria = _validate_choices(choices)
    block = await get_jev_client().ask(
        state,
        _QID_DECIDE,
        {"type": "choice", "instructions": instructions, "criteria": criteria},
    )
    choice = block.get("choice")
    if not isinstance(choice, str):
        raise JevServiceError(f"decision service returned no choice: {block!r:.200}")
    return {
        "choice": choice,
        "probabilities": block.get("probabilities", {}),
        "confidence": block.get("confidence"),
    }


@mcp.tool()
@tool_handler("jev_judge")
async def jev_judge(
    state: str,
    instructions: str,
    true_label: str = "true",
    false_label: str = "false",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Judge a yes/no question with a calibrated probability.

    System One reflex (Open-Jev-2B): binary gate for agents.
    state: the situation to judge (1..4000 chars).
    instructions: a yes/no question about the state (1..1000 chars).
    true_label / false_label: agent-facing labels of the verdict.
    Returns {probability, meaning} — meaning is the label whose side
    holds (probability > 0.5 → true_label).
    """
    _validate_prompt(state, instructions)
    if not (true_label or "").strip() or not (false_label or "").strip():
        raise ValueError("true_label and false_label must be non-empty strings")
    block = await get_jev_client().ask(
        state,
        _QID_JUDGE,
        {"type": "noul", "instructions": instructions, "criteria": dict(_NOUL_CRITERIA)},
    )
    raw_noul = block.get("noul")
    if isinstance(raw_noul, bool) or not isinstance(raw_noul, (int, float)):
        raise JevServiceError(f"decision service returned no noul value: {block!r:.200}")
    probability = float(raw_noul)
    return {
        "probability": probability,
        "meaning": true_label if probability > 0.5 else false_label,
    }


@mcp.tool()
@tool_handler("jev_rate")
async def jev_rate(
    state: str,
    instructions: str,
    levels: list[str],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Rate the situation on an ordered scale of levels.

    System One reflex (Open-Jev-2B): severity / grade for agents.
    state: the situation to rate (1..4000 chars).
    instructions: the rating question (1..1000 chars).
    levels: ordered level descriptions, 2..10 (index 0 = lowest).
    Returns {score, level, probabilities} — score is a float index into
    levels (0..len-1), level is levels[round(score)].
    """
    _validate_prompt(state, instructions)
    cleaned = _validate_levels(levels)
    block = await get_jev_client().ask(
        state,
        _QID_RATE,
        {"type": "score", "instructions": instructions, "criteria": cleaned},
    )
    raw_score = block.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise JevServiceError(f"decision service returned no score value: {block!r:.200}")
    score = float(raw_score)
    # round + clamp: guard от FP-выхода за границы шкалы
    index = min(max(round(score), 0), len(cleaned) - 1)
    return {
        "score": score,
        "level": cleaned[index],
        "probabilities": block.get("probabilities", {}),
    }
