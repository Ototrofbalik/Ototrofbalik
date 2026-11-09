"""Agent / tool yürütme sözleşmeleri."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from .common import Strict


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    NEEDS_APPROVAL = "needs_approval"
    REJECTED = "rejected"


class ToolResult(Strict):
    """Her tool bu zarfı döner. data JSON-serileştirilebilir olmalı."""
    tool: str
    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    pending_change_id: str | None = None   # status=NEEDS_APPROVAL ise
    artifacts: list[str] = Field(default_factory=list)   # üretilen dosya yolları


class PendingChange(Strict):
    """Kullanıcı onayı bekleyen layout değişikliği."""
    id: str
    task_id: str
    description: str
    tool: str
    args: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    approved: bool | None = None


class ToolCallRecord(Strict):
    task_id: str
    seq: int
    tool: str
    args: dict[str, Any]
    status: ToolStatus
    duration_ms: float
    output_summary: str = ""
    error: str | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GeminiStatus(StrEnum):
    CONNECTED = "Connected"
    API_KEY_MISSING = "API Key Missing"
    QUOTA_EXCEEDED = "Quota Exceeded"
    INVALID_MODEL = "Invalid Model"
    CONNECTION_ERROR = "Connection Error"
    DISABLED = "Disabled"


class AgentMessage(Strict):
    role: str    # user | assistant | tool
    text: str
    task_id: str | None = None


class AgentReply(Strict):
    task_id: str
    text: str
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    pending_changes: list[PendingChange] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    gemini_status: GeminiStatus
    requests_used: int = 0
    tokens_used: int = 0
