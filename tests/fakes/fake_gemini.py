"""Script'li sahte Gemini istemcisi (ağ yok). GeminiClientProtocol'ü uygular."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from backend.agent.gemini_client import ChatTurn, FunctionCallReq, GeminiError, GeminiResponse
from backend.schemas import GeminiStatus


@dataclass
class Step:
    """Tek bir generate() yanıtı: metin, function call'lar veya fırlatılacak hata."""
    response: GeminiResponse | None = None
    error: Exception | None = None
    tokens: int = 10

    @staticmethod
    def call(name: str, args: dict[str, Any] | None = None, *more: tuple[str, dict[str, Any]]) -> Step:
        calls = [FunctionCallReq(name=name, args=args or {}), *(FunctionCallReq(name=n, args=a) for n, a in more)]
        return Step(GeminiResponse(function_calls=calls))

    @staticmethod
    def text(text: str) -> Step:
        return Step(GeminiResponse(text=text))

    @staticmethod
    def fail(error: Exception) -> Step:
        return Step(error=error)


class FakeGeminiClient:
    def __init__(self, script: Sequence[Step | Callable[[Sequence[ChatTurn]], Step]] = (),
                 status: GeminiStatus = GeminiStatus.CONNECTED) -> None:
        self.script = list(script)
        self._status = status
        self._requests = 0
        self._tokens = 0
        self.calls: list[dict[str, Any]] = []     # her generate() girdisi

    @property
    def status(self) -> GeminiStatus:
        return self._status

    @status.setter
    def status(self, v: GeminiStatus) -> None:
        self._status = v

    @property
    def requests_used(self) -> int:
        return self._requests

    @property
    def tokens_used(self) -> int:
        return self._tokens

    def generate(self, turns: Sequence[ChatTurn], *, system_prompt: str, tools: Sequence[dict[str, Any]]) -> GeminiResponse:
        self.calls.append({"turns": [t.model_copy(deep=True) for t in turns], "system_prompt": system_prompt,
                           "tool_names": [t["name"] for t in tools]})
        if self._status in (GeminiStatus.API_KEY_MISSING, GeminiStatus.DISABLED, GeminiStatus.QUOTA_EXCEEDED):
            raise GeminiError(self._status, "sahte istemci kullanılamıyor")
        if not self.script:
            raise AssertionError("FakeGeminiClient script'i bitti (beklenmeyen generate çağrısı).")
        step = self.script.pop(0)
        if callable(step):
            step = step(turns)
        self._requests += 1
        if step.error is not None:
            raise step.error
        assert step.response is not None
        self._tokens += step.tokens
        return step.response.model_copy(update={"tokens": step.tokens})
