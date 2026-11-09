"""[Gemini Integration Engineer] FactoryFlow Engineer orchestrator.

Kullanıcı mesajı → Gemini function calling döngüsü → ToolExecutor → ToolResult → açıklama (AgentReply).

Gemini'ye hiçbir tesis verisi şu durumlarda GİTMEZ ve `offline` mod kullanılır:
- `external_data_consent` verilmemiş,
- istemci yok / anahtar yok / devre dışı / kota aşıldı / model geçersiz.
Offline modda "Bu tesisi analiz et" gibi komutlar deterministik `backend.pipeline.analyze_facility` ile çalışır.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend import services
from backend.agent.gemini_client import (
    ChatTurn,
    FunctionResultResp,
    GeminiClientProtocol,
    GeminiError,
)
from backend.agent.offline import is_analyze_request, run_offline_analysis, summarize_analysis
from backend.agent.system_prompt import build_system_prompt
from backend.agent.task_logger import mask_text
from backend.agent.tool_executor import ToolExecutor, model_payload
from backend.agent.tool_registry import function_declaration_dicts
from backend.config import Settings, get_settings
from backend.schemas import AgentReply, GeminiStatus, PendingChange, ToolResult, ToolStatus

logger = logging.getLogger("factoryflow.agent.orchestrator")

_OFFLINE_STATUSES = {GeminiStatus.API_KEY_MISSING, GeminiStatus.DISABLED, GeminiStatus.QUOTA_EXCEEDED,
                     GeminiStatus.INVALID_MODEL}
_STATUS_MESSAGES = {
    GeminiStatus.API_KEY_MISSING: "Gemini API anahtarı tanımlı değil",
    GeminiStatus.DISABLED: "Gemini devre dışı",
    GeminiStatus.QUOTA_EXCEEDED: "Gemini kotası aşıldı (AI kilitli)",
    GeminiStatus.INVALID_MODEL: "Gemini modeli geçersiz/erişilemez",
    GeminiStatus.CONNECTION_ERROR: "Gemini bağlantı hatası",
}
MAX_HISTORY_TURNS = 20


class FactoryFlowAgent:
    def __init__(self, client: GeminiClientProtocol | None = None, executor: ToolExecutor | None = None, *,
                 settings: Settings | None = None, max_requests_per_task: int | None = None,
                 analyzer: Callable[..., Any] | None = None) -> None:
        self.client = client
        self.executor = executor or ToolExecutor()
        self._settings = settings
        self._max_requests = max_requests_per_task
        self._analyzer = analyzer
        self._history: list[ChatTurn] = []   # önceki görevlerin yalnız (kullanıcı, nihai yanıt) metinleri

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    @property
    def gemini_status(self) -> GeminiStatus:
        return self.client.status if self.client is not None else GeminiStatus.DISABLED

    # -- onay (UI bunları çağırır; Gemini'nin erişimi yok) ---------------------------------------
    def approve_change(self, change_id: str) -> ToolResult:
        return self.executor.approve_change(change_id)

    def reject_change(self, change_id: str) -> ToolResult:
        return self.executor.reject_change(change_id)

    # -- ana giriş -----------------------------------------------------------------------------
    def handle(self, user_text: str, project_id: str | None = None) -> AgentReply:
        task_id = self.executor.start_task(user_text, project_id)
        pid = self.executor.ctx.project_id
        st = self.gemini_status
        offline_reason: str | None = None
        if self.client is None or st in _OFFLINE_STATUSES:
            offline_reason = _STATUS_MESSAGES.get(st, "Gemini kullanılamıyor") + "."
        elif not self.settings.external_data_consent:
            offline_reason = ("Tesis verilerinin harici API'ye (Gemini) gönderilmesi için onayınız yok; veriler "
                              "yerelde kalıyor. Onay vermek için ayarlardaki harici veri onayını açın.")
        if offline_reason is not None:
            return self._offline_reply(task_id, user_text, pid, offline_reason)
        assert self.client is not None
        req0, tok0 = self.client.requests_used, self.client.tokens_used
        try:
            text, results = self._loop(task_id, user_text, pid)
        except GeminiError as exc:
            logger.warning("Gemini hatası task=%s status=%s", task_id, exc.status.value)
            return self._offline_reply(task_id, user_text, pid, f"{exc.message}", status=exc.status)
        pending = self.executor.pending_changes(task_id)
        if pending:
            text += "\n\nOnay bekleyen değişiklikler (henüz UYGULANMADI):\n" + "\n".join(
                f"- [{p.id}] {p.description}" for p in pending)
        return self._finish(task_id, text, results, pending, self.client.status, "done",
                            self.client.requests_used - req0, self.client.tokens_used - tok0)

    # -- Gemini döngüsü ------------------------------------------------------------------------
    def _loop(self, task_id: str, user_text: str, pid: str | None) -> tuple[str, list[ToolResult]]:
        assert self.client is not None
        max_req = max(1, self._max_requests or self.settings.gemini_max_requests_per_task)
        decls = function_declaration_dicts()
        prompt = build_system_prompt(pid)
        head = f"[Aktif proje: {pid}]\n" if pid else ""
        turns = [*self._history, ChatTurn(role="user", text=head + user_text)]
        results: list[ToolResult] = []
        final = ""
        for _ in range(max_req):
            resp = self.client.generate(turns, system_prompt=prompt, tools=decls)
            if not resp.function_calls:
                final = resp.text.strip() or "Yanıt üretilemedi."
                break
            turns.append(ChatTurn(role="model", text=resp.text, function_calls=resp.function_calls, raw=resp.raw))
            outs = []
            for call in resp.function_calls:
                r = self.executor.execute(task_id, call.name, call.args)
                results.append(r)
                outs.append(FunctionResultResp(name=call.name, response=model_payload(r), id=call.id))
            turns.append(ChatTurn(role="tool", function_results=outs))
        else:
            final = (f"Bu görev için istek sınırına ({max_req}) ulaşıldı; işlem yarım kalmış olabilir. "
                     "Yapılan araç çağrıları aşağıda listelenmiştir, devam etmek için yeni bir mesaj verin.")
        self._remember(user_text, final)
        return final, results

    def _remember(self, user_text: str, reply: str) -> None:
        self._history += [ChatTurn(role="user", text=user_text), ChatTurn(role="model", text=reply)]
        self._history = self._history[-MAX_HISTORY_TURNS:]

    # -- offline -------------------------------------------------------------------------------
    def _offline_reply(self, task_id: str, user_text: str, pid: str | None, reason: str,
                       status: GeminiStatus | None = None) -> AgentReply:
        st = status or self.gemini_status
        head = f"AI asistanı devre dışı: {reason}\nMühendislik uygulaması çalışmaya devam eder."
        artifacts: list[str] = []
        if not is_analyze_request(user_text):
            body = ("Offline modda yalnızca “Bu tesisi analiz et” komutu çalışır (yerel hesaplama modülleriyle). "
                    "Diğer işlemler için arayüzdeki mühendislik sayfalarını kullanabilir veya AI'yı yapılandırabilirsiniz.")
        elif not pid:
            body = "Analiz için aktif bir proje seçilmedi."
        else:
            body, artifacts = self._offline_analysis(task_id, pid)
        return self._finish(task_id, f"{head}\n\n{body}", [], [], st, "offline", 0, 0, artifacts)

    def _offline_analysis(self, task_id: str, pid: str) -> tuple[str, list[str]]:
        import time

        t0 = time.perf_counter()
        try:
            project = services.get_project(pid)
            out_dir = self.executor.ctx.artifact_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            kw: dict[str, Any] = {} if self._analyzer is None else {"analyzer": self._analyzer}
            analysis, notes = run_offline_analysis(project, Path(out_dir), **kw)
        except Exception as exc:  # noqa: BLE001
            msg = mask_text(f"Analiz çalıştırılamadı ({type(exc).__name__}): {exc}")
            self.executor.log.log_call(task_id, "analyze_facility", {"project_id": pid}, ToolStatus.ERROR,
                                       (time.perf_counter() - t0) * 1000, error=msg)
            return msg, []
        self.executor.log.log_call(task_id, "analyze_facility", {"project_id": pid}, ToolStatus.OK,
                                   (time.perf_counter() - t0) * 1000,
                                   output={"valid": analysis.validation.valid,
                                           "steps": [s.step for s in analysis.steps if s.ok]})
        return summarize_analysis(analysis, project, notes), list(analysis.artifacts)

    # -- sonuç ---------------------------------------------------------------------------------
    def _finish(self, task_id: str, text: str, results: list[ToolResult], pending: list[PendingChange],
                status: GeminiStatus, task_status: str, requests: int, tokens: int,
                extra_artifacts: list[str] | None = None) -> AgentReply:
        text = mask_text(text)
        artifacts = list(dict.fromkeys([*(extra_artifacts or []), *(a for r in results for a in r.artifacts)]))
        self.executor.log.finish_task(task_id, text, task_status, requests, tokens)
        return AgentReply(task_id=task_id, text=text, tool_calls=self.executor.records(task_id),
                          pending_changes=pending, artifacts=artifacts, gemini_status=status,
                          requests_used=requests, tokens_used=tokens)
