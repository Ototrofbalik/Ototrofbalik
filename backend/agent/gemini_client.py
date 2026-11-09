"""[Gemini Integration Engineer] Gemini (google-genai) istemcisi.

- Model adı .env'den (GEMINI_MODEL); kodda sabit model adı YOK. Ücretli modele otomatik geçiş YOK.
- İstek/token sayacı, günlük bütçe, exponential backoff, timeout, yerel önbellek, kota aşımında kilit.
- API anahtarı hiçbir yere yazılmaz (log, hata mesajı, repr).
- SDK'dan bağımsız `ChatTurn`/`GeminiResponse` tipleri: orchestrator ve sahte istemci SDK'ya bağlı değildir.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from backend.agent.task_logger import mask_text
from backend.config import Settings, get_settings
from backend.schemas import GeminiStatus

logger = logging.getLogger("factoryflow.agent.gemini")

FREE_TIER_NOTE = ("Ücretsiz kota/limit bilgileri değişebilir ve garanti edilmez; geçerli limitleri Google AI "
                  "Studio / resmî API dokümantasyonundan kontrol edin. Model otomatik değiştirilmez.")


# --------------------------------------------------------------------------- SDK'dan bağımsız tipler
class FunctionCallReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class FunctionResultResp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    response: dict[str, Any]
    id: str | None = None


class ChatTurn(BaseModel):
    """role: user | model | tool. `raw` SDK'ya özgü ham içeriktir (örn. thought signature); önbellek anahtarına girmez."""
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    role: Literal["user", "model", "tool"]
    text: str = ""
    function_calls: list[FunctionCallReq] = Field(default_factory=list)
    function_results: list[FunctionResultResp] = Field(default_factory=list)
    raw: Any = Field(default=None, exclude=True)


class GeminiResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    text: str = ""
    function_calls: list[FunctionCallReq] = Field(default_factory=list)
    tokens: int = 0
    cached: bool = False
    raw: Any = Field(default=None, exclude=True)


class GeminiError(Exception):
    """Gemini çağrısı hatası. `status` kullanıcıya gösterilen bağlantı durumudur. Mesaj sır içermez."""

    def __init__(self, status: GeminiStatus, message: str) -> None:
        super().__init__(mask_text(message))
        self.status = status
        self.message = mask_text(message)


class QuotaExceededError(GeminiError):
    def __init__(self, message: str = "Gemini kotası aşıldı; AI geçici olarak kilitlendi. "
                                      "Mühendislik uygulaması çalışmaya devam eder.") -> None:
        super().__init__(GeminiStatus.QUOTA_EXCEEDED, message)


@runtime_checkable
class GeminiClientProtocol(Protocol):
    """Orchestrator'ın ihtiyaç duyduğu en küçük arayüz. FakeGeminiClient da bunu uygular."""

    @property
    def status(self) -> GeminiStatus: ...

    @property
    def requests_used(self) -> int: ...

    @property
    def tokens_used(self) -> int: ...

    def generate(self, turns: Sequence[ChatTurn], *, system_prompt: str,
                 tools: Sequence[dict[str, Any]]) -> GeminiResponse: ...


# --------------------------------------------------------------------------- hata sınıflandırma
def _error_code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return None


def classify_error(exc: BaseException) -> tuple[GeminiStatus, bool, bool]:
    """(durum, yeniden_denenebilir, kota_mı)."""
    code = _error_code(exc)
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    if code == 429 or "resource_exhausted" in msg or "quota" in msg:
        return GeminiStatus.QUOTA_EXCEEDED, True, True
    if code in (401, 403) or "api key" in msg or "api_key" in msg or "permission_denied" in msg:
        return GeminiStatus.API_KEY_MISSING, False, False
    if code == 404 or ("model" in msg and ("not found" in msg or "not supported" in msg)):
        return GeminiStatus.INVALID_MODEL, False, False
    if "timeout" in name or "timeout" in msg or "timed out" in msg or isinstance(exc, (TimeoutError, ConnectionError)):
        return GeminiStatus.CONNECTION_ERROR, True, False
    if code is not None and code >= 500:
        return GeminiStatus.CONNECTION_ERROR, True, False
    if code is not None and 400 <= code < 500:
        return GeminiStatus.CONNECTION_ERROR, False, False
    return GeminiStatus.CONNECTION_ERROR, True, False


# --------------------------------------------------------------------------- gerçek istemci
class GeminiClient:
    """google-genai üzerinden Gemini. Anahtar yoksa/kapalıysa hiçbir ağ çağrısı yapılmaz."""

    def __init__(self, settings: Settings | None = None, *, sdk_client: Any = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 3, backoff_base_s: float = 1.0,
                 cache_size: int = 64, clock: Callable[[], datetime] | None = None) -> None:
        self._settings = settings or get_settings()
        self._sdk = sdk_client
        self._sleep = sleep
        self._max_retries = max_retries
        self._backoff_base = backoff_base_s
        self._cache: OrderedDict[str, GeminiResponse] = OrderedDict()
        self._cache_size = cache_size
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self.model: str = self._settings.gemini_model.strip()
        self._requests = 0
        self._tokens = 0
        self._day: date = self._clock().date()
        self._daily_requests = 0
        self._quota_locked = False
        self._model_invalid = False
        self._connection_error = False
        self._verified = False

    def __repr__(self) -> str:  # anahtar asla yazılmaz
        return f"GeminiClient(model={self.model!r}, status={self.status.value!r})"

    # -- durum -------------------------------------------------------------------------------
    @property
    def status(self) -> GeminiStatus:
        s = self._settings
        if not s.gemini_enabled:
            return GeminiStatus.DISABLED
        if not s.gemini_configured:
            return GeminiStatus.API_KEY_MISSING
        if not self.model or self._model_invalid:
            return GeminiStatus.INVALID_MODEL
        if self._quota_locked:
            return GeminiStatus.QUOTA_EXCEEDED
        if self._connection_error:
            return GeminiStatus.CONNECTION_ERROR
        return GeminiStatus.CONNECTED

    @property
    def verified(self) -> bool:
        """Gerçek bir başarılı çağrı/doğrulama yapıldı mı (CONNECTED yalnızca 'yapılandırıldı' da olabilir)."""
        return self._verified

    @property
    def requests_used(self) -> int:
        return self._requests

    @property
    def tokens_used(self) -> int:
        return self._tokens

    @property
    def daily_requests_used(self) -> int:
        self._roll_day()
        return self._daily_requests

    @property
    def daily_budget(self) -> int:
        return self._settings.gemini_daily_request_budget

    def unlock_quota(self) -> None:
        """Kullanıcı kilidi elle açar (örn. kota yenilendikten sonra)."""
        with self._lock:
            self._quota_locked = False

    def _roll_day(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day, self._daily_requests = today, 0
            self._quota_locked = False   # günlük kilit yeni günde kalkar

    # -- SDK ---------------------------------------------------------------------------------
    def _sdk_client(self) -> Any:
        if self._sdk is None:
            from google import genai
            from google.genai import types

            key = self._settings.gemini_api_key.get_secret_value()  # type: ignore[union-attr]
            self._sdk = genai.Client(
                api_key=key, http_options=types.HttpOptions(timeout=int(self._settings.gemini_timeout_s * 1000)))
        return self._sdk

    def _ensure_usable(self) -> None:
        st = self.status
        if st in (GeminiStatus.DISABLED, GeminiStatus.API_KEY_MISSING, GeminiStatus.INVALID_MODEL):
            raise GeminiError(st, {GeminiStatus.DISABLED: "Gemini devre dışı (GEMINI_ENABLED=false).",
                                   GeminiStatus.API_KEY_MISSING: "Gemini API anahtarı tanımlı değil (.env).",
                                   GeminiStatus.INVALID_MODEL: "Geçerli bir GEMINI_MODEL tanımlı değil."}[st])
        self._roll_day()
        if self._quota_locked:
            raise QuotaExceededError()
        if self._daily_requests >= self.daily_budget:
            self._quota_locked = True
            raise QuotaExceededError(f"Günlük istek bütçesi ({self.daily_budget}) doldu; AI kilitlendi.")

    # -- istek -------------------------------------------------------------------------------
    def generate(self, turns: Sequence[ChatTurn], *, system_prompt: str,
                 tools: Sequence[dict[str, Any]]) -> GeminiResponse:
        key = self._cache_key(turns, system_prompt, tools)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit.model_copy(update={"cached": True, "tokens": 0})
        self._ensure_usable()
        resp = self._call_with_backoff(lambda: self._generate_once(turns, system_prompt, tools))
        with self._lock:
            self._cache[key] = resp
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return resp

    def _cache_key(self, turns: Sequence[ChatTurn], system_prompt: str, tools: Sequence[dict[str, Any]]) -> str:
        payload = json.dumps(
            {"m": self.model, "s": system_prompt, "t": [t.model_dump(mode="json") for t in turns],
             "tools": [d.get("name") for d in tools]}, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _call_with_backoff(self, fn: Callable[[], GeminiResponse]) -> GeminiResponse:
        last: GeminiError | None = None
        for attempt in range(self._max_retries + 1):
            self._ensure_usable()
            with self._lock:
                self._requests += 1
                self._daily_requests += 1
            try:
                resp = fn()
            except GeminiError:
                raise
            except Exception as exc:  # noqa: BLE001 - SDK hataları sınıflandırılır
                status, retryable, is_quota = classify_error(exc)
                msg = mask_text(f"{type(exc).__name__}: {exc}")
                logger.warning("Gemini çağrısı başarısız (deneme %d): %s", attempt + 1, msg)
                last = QuotaExceededError() if is_quota else GeminiError(status, msg)
                if status is GeminiStatus.INVALID_MODEL:
                    self._model_invalid = True
                if not retryable or attempt >= self._max_retries:
                    break
                self._sleep(self._backoff_base * (2 ** attempt))
                continue
            with self._lock:
                self._tokens += resp.tokens
                self._connection_error = False
                self._verified = True
            return resp
        assert last is not None
        if isinstance(last, QuotaExceededError):
            self._quota_locked = True    # kota aşımı: işlemleri kilitle (tekrar denemeyi durdur)
        elif last.status is GeminiStatus.CONNECTION_ERROR:
            self._connection_error = True
        raise last

    def _generate_once(self, turns: Sequence[ChatTurn], system_prompt: str,
                       tools: Sequence[dict[str, Any]]) -> GeminiResponse:
        from google.genai import types

        contents = [self._to_content(t, types) for t in turns]
        cfg = types.GenerateContentConfig(
            system_instruction=system_prompt, temperature=0.2,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=d["name"], description=d.get("description", ""),
                                          parameters_json_schema=d.get("parameters_json_schema"))
                for d in tools])] if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        raw = self._sdk_client().models.generate_content(model=self.model, contents=contents, config=cfg)
        calls: list[FunctionCallReq] = []
        texts: list[str] = []
        cand_content = None
        for cand in getattr(raw, "candidates", None) or []:
            cand_content = getattr(cand, "content", None)
            for part in getattr(cand_content, "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc is not None and getattr(fc, "name", None):
                    calls.append(FunctionCallReq(name=fc.name, args=dict(fc.args or {}), id=getattr(fc, "id", None)))
                elif getattr(part, "text", None) and not getattr(part, "thought", False):
                    texts.append(part.text)
            break
        usage = getattr(raw, "usage_metadata", None)
        tokens = int(getattr(usage, "total_token_count", 0) or 0)
        return GeminiResponse(text="".join(texts), function_calls=calls, tokens=tokens, raw=cand_content)

    @staticmethod
    def _to_content(turn: ChatTurn, types: Any) -> Any:
        if turn.raw is not None and turn.role == "model":
            return turn.raw     # thought signature vb. korunur
        if turn.role == "tool":
            return types.Content(role="user", parts=[
                types.Part.from_function_response(name=r.name, response=r.response) for r in turn.function_results])
        parts: list[Any] = []
        if turn.text:
            parts.append(types.Part(text=turn.text))
        for c in turn.function_calls:
            parts.append(types.Part(function_call=types.FunctionCall(name=c.name, args=c.args)))
        return types.Content(role="model" if turn.role == "model" else "user", parts=parts)

    # -- model doğrulama / listeleme ------------------------------------------------------------
    def verify_model(self, model: str | None = None) -> tuple[bool, str]:
        """Seçili modelin hesapta erişilebilir olduğunu doğrular (models.get). Kota harcamaz."""
        name = (model or self.model).strip()
        if not self._settings.gemini_active:
            return False, "Gemini devre dışı veya API anahtarı yok."
        if not name:
            return False, "Model adı boş (.env GEMINI_MODEL)."
        try:
            self._sdk_client().models.get(model=name)
        except Exception as exc:  # noqa: BLE001
            status, _, _ = classify_error(exc)
            if status is GeminiStatus.INVALID_MODEL:
                self._model_invalid = name == self.model
            return False, mask_text(f"Model doğrulanamadı ({status.value}): {exc}")
        if name == self.model:
            self._model_invalid = False
            self._verified = True
        return True, f"Model erişilebilir: {name}"

    def list_available_models(self) -> list[dict[str, Any]]:
        """Hesabın generateContent destekleyen modelleri. Ücretsiz olup olmadığı API'den ÖĞRENİLEMEZ."""
        if not self._settings.gemini_active:
            raise GeminiError(self.status, "Gemini devre dışı veya API anahtarı yok.")
        try:
            out = []
            for m in self._sdk_client().models.list():
                actions = list(getattr(m, "supported_actions", None) or [])
                if actions and "generateContent" not in actions:
                    continue
                out.append({"name": str(m.name).removeprefix("models/"),
                            "display_name": getattr(m, "display_name", None), "note": FREE_TIER_NOTE})
            return out
        except Exception as exc:  # noqa: BLE001
            status, _, _ = classify_error(exc)
            raise GeminiError(status, f"Model listesi alınamadı: {exc}") from exc

    def select_model(self, model: str) -> tuple[bool, str]:
        """Manuel model değiştirme: yalnız doğrulanırsa uygulanır (kalıcı yazmaz, otomatik geçiş yok)."""
        ok, msg = self.verify_model(model)
        if ok:
            with self._lock:
                self.model = model.strip()
                self._cache.clear()
        return ok, msg

    def test_connection(self) -> GeminiStatus:
        """İlk bağlantı testi: yalnız model doğrulaması (içerik göndermez)."""
        self.verify_model()
        return self.status
