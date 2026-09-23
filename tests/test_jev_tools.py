"""Tests for jev tools (System One bridge, Open-Jev-2B).

Сеть НЕ трогаем: транспорт — AsyncMock на httpx-клиенте (подмена
_get_client). Валидационные ошибки обязаны отрабатываться ДО похода
в сеть — проверяем assert_not_awaited на post.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from memory_server.exceptions import JevServiceError
from memory_server.tools import jev_tools
from memory_server.tools.jev_tools import (
    JevClient,
    jev_decide,
    jev_judge,
    jev_rate,
)


def _response(payload: dict[str, Any], status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    return resp


def _transport(response: MagicMock | Exception) -> MagicMock:
    """Мок httpx-клиента: post — AsyncMock с ответом/ошибкой."""
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=None if isinstance(response, Exception) else response,
    )
    return client


def _patched_jev(transport: MagicMock, api_key: str = "test-key") -> JevClient:
    """JevClient с замоканным транспортом (без реальной сети)."""
    jev = JevClient(base_url="http://jev.test", api_key=api_key)

    async def _fake_get_client() -> MagicMock:
        return transport

    jev._get_client = _fake_get_client  # type: ignore[method-assign]
    return jev


def _patch_tool_client(monkeypatch: pytest.MonkeyPatch, jev: JevClient) -> None:
    """Тулы берут клиент из процессного синглтона — подменяем его."""
    monkeypatch.setattr(jev_tools, "_jev_client", jev)


# ---------------------------------------------------------------------------
# JevClient (транспортный слой)
# ---------------------------------------------------------------------------


class TestJevClient:
    @pytest.mark.asyncio
    async def test_ask_posts_payload_and_bearer(self):
        """POST /v1/systemone: body {state, questions}, Bearer при заданном ключе."""
        transport = _transport(
            _response({"answers": {"decision": {"type": "choice", "choice": "a"}}})
        )
        jev = _patched_jev(transport, api_key="s3cret")

        block = await jev.ask("state text", "decision", {"type": "choice"})

        assert block == {"type": "choice", "choice": "a"}
        transport.post.assert_awaited_once()
        args, kwargs = transport.post.call_args
        assert args[0] == "/v1/systemone"
        assert kwargs["json"] == {
            "state": "state text",
            "questions": {"decision": {"type": "choice"}},
        }
        assert kwargs["headers"]["Authorization"] == "Bearer s3cret"

    @pytest.mark.asyncio
    async def test_ask_no_bearer_without_key(self):
        """Пустой ключ → заголовок Authorization отсутствует (локальный инстанс)."""
        transport = _transport(_response({"answers": {"gate": {"type": "noul", "noul": 1.0}}}))
        jev = _patched_jev(transport, api_key="")

        await jev.ask("s", "gate", {"type": "noul"})

        _, kwargs = transport.post.call_args
        assert "Authorization" not in kwargs["headers"]

    @pytest.mark.asyncio
    async def test_ask_http_error_raises(self):
        """HTTP 503 сервиса → JevServiceError (не молчаливый дефолт)."""
        transport = _transport(_response({"detail": "loading"}, status_code=503))
        jev = _patched_jev(transport)

        with pytest.raises(JevServiceError, match="HTTP 503"):
            await jev.ask("s", "decision", {"type": "choice"})

    @pytest.mark.asyncio
    async def test_ask_transport_error_raises(self):
        """Таймаут/обрыв соединения → JevServiceError (unreachable)."""
        transport = _transport(httpx.ConnectTimeout("timed out"))
        jev = _patched_jev(transport)

        with pytest.raises(JevServiceError, match="unreachable"):
            await jev.ask("s", "decision", {"type": "choice"})

    @pytest.mark.asyncio
    async def test_ask_missing_answer_block_raises(self):
        """Ответ без answers[qid] → JevServiceError (wire-формат нарушен)."""
        transport = _transport(_response({"answers": {}}))
        jev = _patched_jev(transport)

        with pytest.raises(JevServiceError, match="no answer for 'decision'"):
            await jev.ask("s", "decision", {"type": "choice"})


# ---------------------------------------------------------------------------
# jev_decide (choice)
# ---------------------------------------------------------------------------


class TestJevDecide:
    @pytest.mark.asyncio
    async def test_happy_path(self, monkeypatch):
        """Choice-примитив: {choice, probabilities, confidence} из ответ-блока."""
        transport = _transport(
            _response(
                {
                    "answers": {
                        "decision": {
                            "type": "choice",
                            "choice": "infra",
                            "probabilities": {"infra": 0.96, "billing": 0.04},
                            "confidence": 0.92,
                        }
                    },
                    "model": "open-jev-2b",
                    "usage": {"total_tokens": 128},
                }
            )
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        result = await jev_decide(
            state="replication lag on primary",
            instructions="Which team owns this incident?",
            choices={"infra": "infrastructure team", "billing": "billing team"},
        )

        assert result == {
            "choice": "infra",
            "probabilities": {"infra": 0.96, "billing": 0.04},
            "confidence": 0.92,
        }
        _, kwargs = transport.post.call_args
        assert kwargs["json"]["questions"]["decision"]["type"] == "choice"

    @pytest.mark.asyncio
    async def test_empty_choice_description_falls_back_to_name(self, monkeypatch):
        """Пустой desc выбора = имя (паттерн CLI) — в criteria уходит имя."""
        transport = _transport(
            _response({"answers": {"decision": {"type": "choice", "choice": "a"}}})
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        await jev_decide("s", "q?", choices={"a": "", "b": "bees"})

        _, kwargs = transport.post.call_args
        assert kwargs["json"]["questions"]["decision"]["criteria"] == {"a": "a", "b": "bees"}

    @pytest.mark.asyncio
    async def test_invalid_choices_count_no_network(self, monkeypatch):
        """1 выбор → ошибка валидации ДО сети (post не вызван)."""
        transport = _transport(_response({}))
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="choices must be"):
            await jev_decide("s", "q?", choices={"only": "one"})

        transport.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_choice_name_no_network(self, monkeypatch):
        """Пустое имя выбора → ошибка валидации ДО сети."""
        transport = _transport(_response({}))
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="non-empty"):
            await jev_decide("s", "q?", choices={"": "desc", "b": "desc"})

        transport.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_service_503_raises_to_agent(self, monkeypatch):
        """503 сервиса → тул падает ошибкой (агент видит отказ рефлекс-слоя)."""
        transport = _transport(_response({"detail": "down"}, status_code=503))
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="jev_decide.*HTTP 503"):
            await jev_decide("s", "q?", choices={"a": "A", "b": "B"})


# ---------------------------------------------------------------------------
# jev_judge (noul)
# ---------------------------------------------------------------------------


class TestJevJudge:
    @pytest.mark.asyncio
    async def test_happy_path_true_side(self, monkeypatch):
        """noul 0.87 > 0.5 → meaning = true_label (кастомный)."""
        transport = _transport(
            _response({"answers": {"judgement": {"type": "noul", "noul": 0.87}}})
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        result = await jev_judge(
            state="CI green, tests passed",
            instructions="Is it safe to deploy?",
            true_label="ship it",
            false_label="hold",
        )

        assert result == {"probability": 0.87, "meaning": "ship it"}
        _, kwargs = transport.post.call_args
        # Wire-ключи критериев noul — строго true/false
        assert kwargs["json"]["questions"]["judgement"]["criteria"] == {
            "true": "condition holds true",
            "false": "condition does not hold",
        }

    @pytest.mark.asyncio
    async def test_false_side_below_threshold(self, monkeypatch):
        """noul 0.42 ≤ 0.5 → meaning = false_label."""
        transport = _transport(
            _response({"answers": {"judgement": {"type": "noul", "noul": 0.42}}})
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        result = await jev_judge("s", "q?", true_label="yes", false_label="no")

        assert result == {"probability": 0.42, "meaning": "no"}

    @pytest.mark.asyncio
    async def test_default_labels(self, monkeypatch):
        """Без кастомных лейблов meaning — строковые true/false."""
        transport = _transport(
            _response({"answers": {"judgement": {"type": "noul", "noul": 0.9}}})
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        result = await jev_judge("s", "q?")

        assert result == {"probability": 0.9, "meaning": "true"}

    @pytest.mark.asyncio
    async def test_blank_label_no_network(self, monkeypatch):
        """Пустой true_label → ошибка валидации ДО сети."""
        transport = _transport(_response({}))
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="true_label and false_label"):
            await jev_judge("s", "q?", true_label=" ")

        transport.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bool_noul_rejected(self, monkeypatch):
        """noul=true (bool) — не вероятность: честная ошибка, не 1.0."""
        transport = _transport(
            _response({"answers": {"judgement": {"type": "noul", "noul": True}}})
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="no noul value"):
            await jev_judge("s", "q?")


# ---------------------------------------------------------------------------
# jev_rate (score)
# ---------------------------------------------------------------------------


class TestJevRate:
    @pytest.mark.asyncio
    async def test_happy_path(self, monkeypatch):
        """score 1.6, 3 уровня → level = levels[2]; probabilities на месте."""
        transport = _transport(
            _response(
                {
                    "answers": {
                        "rating": {
                            "type": "score",
                            "score": 1.6,
                            "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
                            "confidence": 0.3,
                        }
                    }
                }
            )
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        result = await jev_rate(
            state="p99 latency 3s",
            instructions="How severe is this alert?",
            levels=["ticket", "wake athena", "wake master"],
        )

        assert result == {
            "score": 1.6,
            "level": "wake master",
            "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
        }
        _, kwargs = transport.post.call_args
        assert kwargs["json"]["questions"]["rating"]["criteria"] == [
            "ticket",
            "wake athena",
            "wake master",
        ]

    @pytest.mark.asyncio
    async def test_score_clamped_to_scale(self, monkeypatch):
        """score 2.6 при 3 уровнях → round=3 выходит за шкалу, clamp → levels[2]."""
        transport = _transport(
            _response({"answers": {"rating": {"type": "score", "score": 2.6}}})
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        result = await jev_rate("s", "q?", levels=["low", "mid", "high"])

        assert result["level"] == "high"

    @pytest.mark.asyncio
    async def test_invalid_levels_no_network(self, monkeypatch):
        """1 уровень → ошибка валидации ДО сети."""
        transport = _transport(_response({}))
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="levels must be"):
            await jev_rate("s", "q?", levels=["only"])

        transport.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_level_no_network(self, monkeypatch):
        """Пустой элемент levels → ошибка валидации ДО сети."""
        transport = _transport(_response({}))
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="non-empty"):
            await jev_rate("s", "q?", levels=["low", "  "])

        transport.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# Общая валидация промпта (state / instructions)
# ---------------------------------------------------------------------------


class TestPromptValidation:
    @pytest.mark.parametrize(
        "state",
        ["", "   ", "x" * 4001],
        ids=["empty", "whitespace", "too-long"],
    )
    @pytest.mark.asyncio
    async def test_bad_state_no_network(self, monkeypatch, state):
        transport = _transport(_response({}))
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="state must be"):
            await jev_decide(state, "q?", choices={"a": "A", "b": "B"})

        transport.post.assert_not_awaited()

    @pytest.mark.parametrize(
        "instructions",
        ["", "x" * 1001],
        ids=["empty", "too-long"],
    )
    @pytest.mark.asyncio
    async def test_bad_instructions_no_network(self, monkeypatch, instructions):
        transport = _transport(_response({}))
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        with pytest.raises(RuntimeError, match="instructions must be"):
            await jev_judge("s", instructions)

        transport.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_boundary_lengths_pass_validation(self, monkeypatch):
        """state=4000 и instructions=1000 символов — граница валидна, сеть зовётся."""
        transport = _transport(
            _response({"answers": {"judgement": {"type": "noul", "noul": 0.5}}})
        )
        _patch_tool_client(monkeypatch, _patched_jev(transport))

        await jev_judge("x" * 4000, "y" * 1000)

        transport.post.assert_awaited_once()
