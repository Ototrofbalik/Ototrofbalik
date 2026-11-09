"""[Gemini Integration Engineer] Görev (task_id) ve tool çağrısı kaydı + sır maskeleme.

Kural: API anahtarı / token / parola değerleri hiçbir log, kayıt veya sonuçta düz metin olarak görünmez.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from backend.schemas import ToolCallRecord, ToolStatus

logger = logging.getLogger("factoryflow.agent")

MASK = "***"
_SENSITIVE_KEY = re.compile(r"(api[_-]?key|secret|token|password|passwd|authorization|credential|bearer)", re.I)
_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),                 # Google API anahtarı biçimi
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|key|token)=([^&\s\"']{6,})"),
]


def _configured_secrets() -> list[str]:
    try:
        from backend.config import get_settings

        key = get_settings().gemini_api_key
        return [key.get_secret_value()] if key and key.get_secret_value() else []
    except Exception:  # noqa: BLE001 - ayar okunamasa da maskeleme çalışmalı
        return []


def mask_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Bilinen sır değerlerini ve sır biçimli dizeleri maskeler."""
    for s in [*_configured_secrets(), *extra_secrets]:
        if s and len(s) >= 4:
            text = text.replace(s, MASK)
    for pat in _SECRET_PATTERNS:
        text = pat.sub(MASK, text)
    return text


def mask_secrets(obj: Any, extra_secrets: Iterable[str] = ()) -> Any:
    """dict/list/str içindeki sırları özyinelemeli maskeler (kopya döner)."""
    extra = tuple(extra_secrets)
    if isinstance(obj, dict):
        return {k: (MASK if isinstance(k, str) and _SENSITIVE_KEY.search(k) else mask_secrets(v, extra))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_secrets(v, extra) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj, extra)
    return obj


def new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:12]}"


def summarize(data: Any, limit: int = 500) -> str:
    try:
        s = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(data)
    return s if len(s) <= limit else s[:limit] + f"...(+{len(s) - limit} karakter)"


def _default_call_sink(record: ToolCallRecord) -> None:
    from backend import services

    services.record_tool_call(record)


def _default_task_sink(task_id: str, **kw: Any) -> None:
    from backend import services

    services.record_agent_task(task_id, **kw)


class TaskLogger:
    """Her tool çağrısı için input/output/süre/durum kaydeder (bellek + Python log + DB)."""

    def __init__(self, call_sink: Callable[[ToolCallRecord], None] | None = _default_call_sink,
                 task_sink: Callable[..., None] | None = _default_task_sink) -> None:
        self._call_sink = call_sink
        self._task_sink = task_sink
        self._records: dict[str, list[ToolCallRecord]] = {}

    def start_task(self, user_text: str = "", project_id: str | None = None) -> str:
        task_id = new_task_id()
        self._records[task_id] = []
        self._persist_task(task_id, project_id=project_id, user_text=mask_text(user_text), status="running")
        logger.info("task started id=%s project=%s", task_id, project_id)
        return task_id

    def finish_task(self, task_id: str, reply_text: str, status: str = "done", requests_used: int | None = None,
                    tokens_used: int | None = None) -> None:
        self._persist_task(task_id, reply_text=mask_text(reply_text), status=status,
                           requests_used=requests_used, tokens_used=tokens_used)
        logger.info("task finished id=%s status=%s", task_id, status)

    def log_call(self, task_id: str, tool: str, args: dict[str, Any], status: ToolStatus, duration_ms: float,
                 output: Any = None, error: str | None = None) -> ToolCallRecord:
        seq = len(self._records.setdefault(task_id, [])) + 1
        record = ToolCallRecord(
            task_id=task_id, seq=seq, tool=tool, args=mask_secrets(args), status=status,
            duration_ms=round(duration_ms, 3), output_summary=mask_text(summarize(mask_secrets(output))),
            error=mask_text(error) if error else None)
        self._records[task_id].append(record)
        logger.info("tool call task=%s seq=%d tool=%s status=%s ms=%.1f", task_id, seq, tool, status.value,
                    duration_ms)
        if self._call_sink is not None:
            try:
                self._call_sink(record)
            except Exception as exc:  # noqa: BLE001 - kayıt hatası görevi düşürmemeli
                logger.warning("tool call kaydı yazılamadı: %s", mask_text(str(exc)))
        return record

    def records(self, task_id: str) -> list[ToolCallRecord]:
        return list(self._records.get(task_id, []))

    def count(self, task_id: str) -> int:
        return len(self._records.get(task_id, []))

    def _persist_task(self, task_id: str, **kw: Any) -> None:
        if self._task_sink is None:
            return
        try:
            self._task_sink(task_id, **kw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("görev kaydı yazılamadı: %s", mask_text(str(exc)))
