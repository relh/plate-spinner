from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class SessionStatus(str, Enum):
    RUNNING = "running"
    IDLE = "idle"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_APPROVAL = "awaiting_approval"
    ERROR = "error"
    CLOSED = "closed"

    @classmethod
    def from_tool(cls, tool_name: str) -> "SessionStatus":
        if tool_name == "AskUserQuestion":
            return cls.AWAITING_INPUT
        if tool_name == "ExitPlanMode":
            return cls.AWAITING_APPROVAL
        return cls.RUNNING


class HookEvent(BaseModel):
    session_id: str
    project_path: str
    event_type: str
    tool_name: str | None = None
    tool_params: dict | None = None
    transcript_path: str | None = None
    git_branch: str | None = None
    provider: str | None = None
    error: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "cwd" in data and "project_path" not in data:
                data["project_path"] = data.pop("cwd")
            if "hook_event_name" in data and "event_type" not in data:
                data["event_type"] = "tool_call"
                data.pop("hook_event_name")
            if "tool_input" in data and "tool_params" not in data:
                data["tool_params"] = data.pop("tool_input")
        return data


class Session(BaseModel):
    session_id: str
    project_path: str
    provider: str | None = None
    status: SessionStatus = SessionStatus.RUNNING
    last_event_type: str | None = None
    last_tool: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def project_name(self) -> str:
        return self.project_path.rstrip("/").split("/")[-1]

    @property
    def needs_attention(self) -> bool:
        return self.status in (
            SessionStatus.AWAITING_INPUT,
            SessionStatus.AWAITING_APPROVAL,
            SessionStatus.IDLE,
            SessionStatus.ERROR,
        )
