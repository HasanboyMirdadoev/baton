from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    """Returns current UTC time in ISO-8601 format ending with 'Z', e.g. '2026-11-02T18:40:00Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Domain Exceptions ---

class NotFoundError(Exception):
    """Resource not found (mapped to 404 Not Found)."""
    pass


class ConflictError(Exception):
    """Resource conflict or invalid state transition (mapped to 409 Conflict)."""
    pass


class ValidationError(Exception):
    """Input validation failure (mapped to 400 Bad Request)."""
    pass


class UnprocessableError(ValidationError):
    """Semantic validation failure (mapped to 422 Unprocessable Entity)."""
    pass


# --- Enums ---

class ProviderId(str, Enum):
    CLAUDE = "claude"
    ANTIGRAVITY = "antigravity"


class RoleId(str, Enum):
    DESIGNER = "designer"
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    CUSTOM = "custom"


class PermissionLevel(str, Enum):
    READONLY = "readonly"   # Plan / read-only mode; cannot edit files or run shell commands
    EDIT = "edit"           # Accept file edits automatically; interactive shell prompts denied
    FULL = "full"           # Auto-approve all tools including shell commands (--dangerously-skip-permissions)


class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


class TurnStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    STOPPED = "stopped"
    TIMEOUT = "timeout"


class PipelineRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class StageRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    CHANGES_REQUESTED = "changes_requested"
    FAILED = "failed"
    STOPPED = "stopped"


class NormalizedEventType(str, Enum):
    SESSION_INIT = "session_init"
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    TURN_RESULT = "turn_result"
    ERROR = "error"


# --- Event Models ---

class NormalizedEvent(BaseModel):
    seq: int
    timestamp: str                      # ISO-8601 UTC timestamp
    type: NormalizedEventType
    provider_conversation_id: Optional[str] = None
    text: Optional[str] = None          # For text_delta, thinking_delta, turn_result, error
    tool_call_id: Optional[str] = None  # Provider tool_use_id or step_index string
    tool_name: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[str] = None
    is_error: bool = False
    denied_actions: List[str] = Field(default_factory=list)
    raw: Optional[Dict[str, Any]] = None


# --- Pydantic Request Schemas ---

class CreateSessionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    provider: ProviderId
    model: str = Field(..., min_length=1)
    role: RoleId
    custom_prompt: str = ""
    permission_level: Optional[PermissionLevel] = None
    working_dir: Optional[str] = None


class UpdateSessionRequest(BaseModel):
    name: Optional[str] = None
    permission_level: Optional[PermissionLevel] = None
    model: Optional[str] = None
    custom_prompt: Optional[str] = None


class StartTurnRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    timeout_sec: Optional[int] = Field(default=None, ge=1, le=7200)


class ForwardTurnRequest(BaseModel):
    target_session_id: str = Field(..., min_length=1)
    instructions: Optional[str] = None


class PipelineStageInput(BaseModel):
    name: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    require_approval: bool = False


class SavePipelineRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    max_review_loops: int = Field(default=2, ge=0, le=5)
    stages: List[PipelineStageInput] = Field(..., min_length=1)


class StartPipelineRunRequest(BaseModel):
    task_prompt: Optional[str] = ""
    start_stage_index: int = 0
    initial_input: Optional[str] = None
    initial_input_turn_id: Optional[str] = None


class ApprovePipelineRunRequest(BaseModel):
    note: Optional[str] = None
