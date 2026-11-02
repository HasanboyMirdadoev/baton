from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union
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
    # Team Mode roles
    PM = "pm"
    UI_DESIGNER = "ui_designer"
    ARCHITECT = "architect"
    UI_CHECKER = "ui_checker"


TeamRoleId = RoleId


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


class TeamRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class TeamStageState(str, Enum):
    PM_TRIAGE = "pm_triage"
    WAITING_USER_ANSWERS = "waiting_user_answers"
    UI_DESIGN = "ui_design"
    PM_MOCKUP_REVIEW = "pm_mockup_review"
    WAITING_MOCKUP_APPROVAL = "waiting_mockup_approval"
    ARCHITECTURE = "architecture"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    PM_ANSWER_RELAY = "pm_answer_relay"
    CHECKING = "checking"
    PM_FINAL_SUMMARY = "pm_final_summary"
    WAITING_FINAL_SIGNOFF = "waiting_final_signoff"
    PAUSED_LIMIT = "paused_limit"
    COMPLETED = "completed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class TeamSignoffAction(str, Enum):
    MERGE = "merge"
    KEEP_BRANCH = "keep_branch"
    DISCARD = "discard"


class TeamMessageKind(str, Enum):
    QUESTION = "question"
    ANSWER = "answer"
    DELIVERABLE = "deliverable"
    APPROVAL_REQUEST = "approval_request"
    APPROVE = "approve"
    REJECT = "reject"
    DONE = "done"
    USER_ACTION = "user_action"


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


# --- Team Mode Request & Contract Schemas ---

class AppRunContract(BaseModel):
    requires_ui_check: bool = True
    start_cmd: str = Field(..., min_length=1)      # e.g. "python3 -m http.server {port}"
    app_url: str = Field(..., min_length=1)        # e.g. "http://127.0.0.1:{port}"
    healthcheck_path: str = "/"
    stop_signal: str = "SIGTERM"


class TeamMemberInput(BaseModel):
    role: RoleId
    agent_name: str = Field(..., min_length=1, max_length=200)
    provider: ProviderId
    model: str = Field(..., min_length=1)
    permission_level: PermissionLevel
    custom_prompt: str = ""


class SaveTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    max_rework_loops: int = Field(default=3, ge=0, le=10)
    max_question_rounds: int = Field(default=4, ge=1, le=10)
    max_total_turns: int = Field(default=30, ge=5, le=100)
    turn_timeout_sec: int = Field(default=600, ge=30, le=7200)
    run_timeout_sec: int = Field(default=3600, ge=60, le=28800)
    auto_approve_mockup: bool = False
    members: List[TeamMemberInput] = Field(default_factory=list)


class StartTeamRunRequest(BaseModel):
    task_prompt: str = Field(..., min_length=1)
    target_repo: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    auto_approve_mockup: bool = False
    needs_ui_mockup_override: Optional[bool] = None
    max_rework_loops: Optional[int] = Field(default=None, ge=0, le=10)
    max_question_rounds: Optional[int] = Field(default=None, ge=1, le=10)
    max_total_turns: Optional[int] = Field(default=None, ge=5, le=100)
    turn_timeout_sec: Optional[int] = Field(default=None, ge=30, le=7200)
    run_timeout_sec: Optional[int] = Field(default=None, ge=60, le=28800)


class AnswerTeamQuestionsRequest(BaseModel):
    answers: List[str] = Field(default_factory=list)


class ReviewTeamMockupRequest(BaseModel):
    approved: bool
    feedback: Optional[str] = ""


class ResumeTeamRunRequest(BaseModel):
    add_rework_loops: int = 0
    add_question_rounds: int = 0
    add_turns: int = 0
    add_timeout_sec: int = 0
    force_signoff: bool = False
    note: Optional[str] = ""


class SignoffTeamRunRequest(BaseModel):
    action: TeamSignoffAction
    commit_message: Optional[str] = None
