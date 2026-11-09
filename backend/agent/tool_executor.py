"""[Gemini Integration Engineer] Tool yürütücü: whitelist, Pydantic arg doğrulama, görev başına çağrı sınırı,
task_id ile kayıt (input/output/süre/durum; sırlar maskeli), hata yakalama ve ONAY AKIŞI.

Layout değiştiren tool'lar `Proposal` döner → burada NEEDS_APPROVAL + PendingChange'e çevrilir.
Kayıtlı layout YALNIZ `approve_change` ile değişir.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from backend import services
from backend.agent.task_logger import TaskLogger, mask_text
from backend.agent.tool_registry import (
    TOOLS,
    HandlerOutput,
    Proposal,
    ToolContext,
    ToolError,
    ToolRejected,
    ToolSpec,
)
from backend.schemas import PendingChange, ToolCallRecord, ToolResult, ToolStatus

DEFAULT_MAX_TOOL_CALLS = 15


@dataclass
class _PendingEntry:
    change: PendingChange
    proposal: Proposal


def _short_validation_error(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors()[:6]:
        loc = ".".join(str(x) for x in e["loc"]) or "(kök)"
        parts.append(f"{loc}: {e['msg']}")
    return "; ".join(parts)


class ToolExecutor:
    def __init__(self, ctx: ToolContext | None = None, *, max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
                 allowed_tools: set[str] | None = None, task_logger: TaskLogger | None = None,
                 registry: dict[str, ToolSpec] | None = None) -> None:
        self.ctx = ctx or ToolContext()
        self.max_tool_calls = max_tool_calls
        self.registry = registry if registry is not None else TOOLS
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else set(self.registry)
        self.log = task_logger or TaskLogger()
        self._pending: dict[str, _PendingEntry] = {}

    # -- görev ---------------------------------------------------------------------------------
    def start_task(self, user_text: str = "", project_id: str | None = None) -> str:
        if project_id:
            self.ctx.project_id = project_id
        return self.log.start_task(user_text, self.ctx.project_id)

    def records(self, task_id: str) -> list[ToolCallRecord]:
        return self.log.records(task_id)

    # -- yürütme -------------------------------------------------------------------------------
    def execute(self, task_id: str, name: str, raw_args: dict[str, Any] | None) -> ToolResult:
        args_in = dict(raw_args or {})
        t0 = time.perf_counter()
        result = self._run(task_id, name, args_in)
        self.log.log_call(task_id, name, args_in, result.status, (time.perf_counter() - t0) * 1000.0,
                          output={"data": result.data, "error_code": result.error_code,
                                  "pending_change_id": result.pending_change_id},
                          error=result.error)
        return result

    def _run(self, task_id: str, name: str, args_in: dict[str, Any]) -> ToolResult:
        spec = self.registry.get(name)
        if spec is None or name not in self.allowed_tools:
            return ToolResult(tool=name, status=ToolStatus.REJECTED, error_code="TOOL_NOT_ALLOWED",
                              error=f"'{name}' izin verilen araçlar arasında değil.")
        if self.log.count(task_id) >= self.max_tool_calls:
            return ToolResult(tool=name, status=ToolStatus.ERROR, error_code="TOOL_CALL_LIMIT",
                              error=f"Bu görev için en fazla {self.max_tool_calls} araç çağrısına izin verilir.")
        try:
            parsed = spec.args_model.model_validate(args_in)
        except ValidationError as exc:
            return ToolResult(tool=name, status=ToolStatus.ERROR, error_code="INVALID_ARGS",
                              error=mask_text(f"Parametreler geçersiz: {_short_validation_error(exc)}"))
        try:
            out = spec.handler(self.ctx, parsed)
        except ToolRejected as exc:
            return ToolResult(tool=name, status=ToolStatus.REJECTED, error_code=exc.code, error=mask_text(exc.message),
                              data=to_jsonable_python(exc.data))
        except ToolError as exc:
            return ToolResult(tool=name, status=ToolStatus.ERROR, error_code=exc.code, error=mask_text(exc.message))
        except (NotImplementedError, services.ModuleNotReady) as exc:
            return ToolResult(tool=name, status=ToolStatus.ERROR, error_code="MODULE_NOT_READY",
                              error=mask_text(str(exc) or "İlgili modül henüz uygulanmadı."))
        except services.NotFoundError as exc:
            return ToolResult(tool=name, status=ToolStatus.ERROR, error_code="NOT_FOUND", error=mask_text(str(exc)))
        except services.ServiceError as exc:
            return ToolResult(tool=name, status=ToolStatus.ERROR, error_code="SERVICE_ERROR",
                              error=mask_text(str(exc)))
        except Exception as exc:  # noqa: BLE001 - hiçbir tool hatası orchestrator'ı düşürmemeli
            return ToolResult(tool=name, status=ToolStatus.ERROR, error_code="TOOL_EXCEPTION",
                              error=mask_text(f"{type(exc).__name__}: {exc}"))
        if isinstance(out, Proposal):
            return self._register_pending(task_id, name, args_in, out)
        assert isinstance(out, HandlerOutput)
        return ToolResult(tool=name, status=ToolStatus.OK, data=to_jsonable_python(out.data), artifacts=out.artifacts)

    # -- onay akışı ----------------------------------------------------------------------------
    def _register_pending(self, task_id: str, name: str, args: dict[str, Any], p: Proposal) -> ToolResult:
        change = PendingChange(id=f"chg-{uuid.uuid4().hex[:10]}", task_id=task_id, description=p.description,
                               tool=name, args=to_jsonable_python(args))
        self._pending[change.id] = _PendingEntry(change, p)
        return ToolResult(tool=name, status=ToolStatus.NEEDS_APPROVAL, pending_change_id=change.id,
                          data={**to_jsonable_python(p.data), "description": p.description,
                                "applied": False, "message": "Kullanıcı onayı bekleniyor; layout henüz değişmedi."})

    def pending_changes(self, task_id: str | None = None, *, unresolved_only: bool = True) -> list[PendingChange]:
        return [e.change for e in self._pending.values()
                if (task_id is None or e.change.task_id == task_id)
                and (not unresolved_only or e.change.approved is None)]

    def approve_change(self, change_id: str) -> ToolResult:
        """Kullanıcı onayı: değişikliği kalıcı uygular. Layout bu arada değiştiyse uygulamaz (STALE)."""
        entry = self._pending.get(change_id)
        t0 = time.perf_counter()
        if entry is None:
            return ToolResult(tool="approve_change", status=ToolStatus.ERROR, error_code="UNKNOWN_CHANGE",
                              error=f"Bekleyen değişiklik bulunamadı: {change_id}")
        ch, prop = entry.change, entry.proposal
        if ch.approved is not None:
            return ToolResult(tool=ch.tool, status=ToolStatus.ERROR, error_code="ALREADY_RESOLVED",
                              error="Bu değişiklik zaten onaylandı/reddedildi.", pending_change_id=change_id)
        try:
            current = services.get_layout(prop.project_id).id
            if current != prop.base_layout_id:
                result = ToolResult(tool=ch.tool, status=ToolStatus.ERROR, error_code="STALE_CHANGE",
                                    pending_change_id=change_id,
                                    error="Aktif layout öneriden sonra değişti; öneriyi yeniden oluşturun.")
            else:
                data = prop.commit()
                ch.approved = True
                result = ToolResult(tool=ch.tool, status=ToolStatus.OK, pending_change_id=change_id,
                                    data=to_jsonable_python({**data, "applied": True}))
        except Exception as exc:  # noqa: BLE001
            result = ToolResult(tool=ch.tool, status=ToolStatus.ERROR, error_code="COMMIT_FAILED",
                                pending_change_id=change_id, error=mask_text(f"{type(exc).__name__}: {exc}"))
        self.log.log_call(ch.task_id, "approve_change", {"change_id": change_id, "tool": ch.tool}, result.status,
                          (time.perf_counter() - t0) * 1000.0, output=result.data, error=result.error)
        return result

    def reject_change(self, change_id: str) -> ToolResult:
        entry = self._pending.get(change_id)
        if entry is None:
            return ToolResult(tool="reject_change", status=ToolStatus.ERROR, error_code="UNKNOWN_CHANGE",
                              error=f"Bekleyen değişiklik bulunamadı: {change_id}")
        ch = entry.change
        if ch.approved is not None:
            return ToolResult(tool=ch.tool, status=ToolStatus.ERROR, error_code="ALREADY_RESOLVED",
                              error="Bu değişiklik zaten onaylandı/reddedildi.", pending_change_id=change_id)
        ch.approved = False
        result = ToolResult(tool=ch.tool, status=ToolStatus.REJECTED, pending_change_id=change_id,
                            error_code="REJECTED_BY_USER", error="Değişiklik kullanıcı tarafından reddedildi.",
                            data={"applied": False})
        self.log.log_call(ch.task_id, "reject_change", {"change_id": change_id, "tool": ch.tool}, result.status, 0.0,
                          output=result.data, error=result.error)
        return result


def model_payload(result: ToolResult, max_chars: int = 24000) -> dict[str, Any]:
    """Gemini'ye gidecek tool yanıtı; çok büyükse kısaltılır (kısaltma açıkça belirtilir)."""
    import json

    payload = result.model_dump(mode="json", exclude_none=True)
    if len(json.dumps(payload, ensure_ascii=False, default=str)) <= max_chars:
        return payload
    payload["data"] = {"truncated": True, "keys": sorted(result.data.keys()),
                       "note": "Sonuç çok büyük olduğu için özetlendi; ayrıntı için ilgili tool'u daraltılmış "
                               "parametrelerle çağırın."}
    return payload


__all__ = ["DEFAULT_MAX_TOOL_CALLS", "ToolExecutor", "model_payload"]
