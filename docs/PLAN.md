# Agent Relay — Implementation Plan

## 1. Overview & Rules for Implementers

This plan breaks the implementation of **Agent Relay** (`docs/ARCHITECTURE.md` and `docs/DESIGN.md`) into **8 self-contained tasks** organized across **4 execution waves**.

Each task is designed for an independent AI implementer session that knows nothing outside this repository's documentation:
1. **Strict File Ownership**: Every file in the repository layout (`docs/ARCHITECTURE.md` §3) belongs to **exactly one task**. Never create, modify, or delete files outside your task's `Files to create` list.
2. **Wave Ordering**: Tasks within the same wave (`Wave N`) have zero dependencies on each other and can execute in parallel. Tasks in `Wave N` only import or depend on files created in `Wave < N`.
3. **Python 3.9.6 Compatibility**: Every Python file MUST start with `from __future__ import annotations` and use `typing.Optional`, `typing.List`, `typing.Dict`, `typing.Set`, `typing.Tuple`, `typing.Any`, `typing.AsyncIterator`, `typing.Union`. Never use runtime `X | Y` union syntax (especially inside Pydantic models or `isinstance` checks) and never use `match` / `case`.
4. **Clarifications as Contract**: Section 2 ("Clarifications & Fixed Cross-Task Interfaces") resolves all method signatures, Pydantic schemas, pub/sub method names, and frontend module exports not fully spelled out in `docs/ARCHITECTURE.md` or `docs/DESIGN.md`. Treat Section 2 as binding specification alongside `docs/ARCHITECTURE.md` and `docs/DESIGN.md`.

---

## 2. Clarifications & Fixed Cross-Task Interfaces

To ensure parallel implementers across waves integrate with zero signature mismatches, all cross-module interfaces are fixed below.

### C1. `backend/models.py` — Complete Pydantic Request/Response Schemas & Helpers
In addition to the enums (`ProviderId`, `RoleId`, `PermissionLevel`, `SessionStatus`, `TurnStatus`, `PipelineRunStatus`, `StageRunStatus`, `NormalizedEventType`) and `NormalizedEvent` defined in `ARCHITECTURE.md` §5.2, `backend/models.py` MUST define and export:

```python
from datetime import datetime, timezone

def utc_now_iso() -> str:
    """Returns current UTC time in ISO-8601 format ending with 'Z', e.g. '2026-11-02T18:40:00Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

And the following Pydantic v2 models (`from pydantic import BaseModel, Field`):
- `CreateSessionRequest`:
  - `name: str = Field(..., min_length=1, max_length=200)`
  - `provider: ProviderId`
  - `model: str = Field(..., min_length=1)`
  - `role: RoleId`
  - `custom_prompt: str = ""`
  - `permission_level: Optional[PermissionLevel] = None`
  - `working_dir: Optional[str] = None`
- `UpdateSessionRequest`:
  - `name: Optional[str] = None`
  - `permission_level: Optional[PermissionLevel] = None`
  - `model: Optional[str] = None`
  - `custom_prompt: Optional[str] = None`
- `StartTurnRequest`:
  - `prompt: str = Field(..., min_length=1)`
  - `timeout_sec: Optional[int] = Field(default=None, ge=1, le=7200)`
- `PipelineStageInput`:
  - `name: str = Field(..., min_length=1)`
  - `session_id: str = Field(..., min_length=1)`
  - `require_approval: bool = False`
- `SavePipelineRequest`:
  - `name: str = Field(..., min_length=1, max_length=200)`
  - `description: str = ""`
  - `max_review_loops: int = Field(default=2, ge=0, le=5)`
  - `stages: List[PipelineStageInput] = Field(..., min_length=1)`
- `StartPipelineRunRequest`:
  - `task_prompt: str = Field(..., min_length=1)`
- `ApprovePipelineRunRequest`:
  - `note: Optional[str] = None`

### C2. `backend/pubsub.py` — `EventBus` Exact Interface
`backend/pubsub.py` MUST implement an `asyncio.Queue`-backed pub/sub bus with the exact signature:

```python
import asyncio
from typing import Any, AsyncIterator, Dict, Optional, Set

class EventBus:
    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue[Dict[str, Any]]] = set()

    async def publish(self, event_name: str, data: Dict[str, Any]) -> None:
        """
        Pushes {"event": event_name, "data": data} to every active subscriber queue
        using put_nowait (ignoring QueueFull if a slow client lags).
        """

    async def subscribe(self, heartbeat_sec: float = 15.0) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator yielding {"event": str, "data": dict} items.
        If no event arrives within `heartbeat_sec`, yields {"event": "ping", "data": {}}.
        Removes its queue from `self._subscribers` in a `finally:` block on disconnect/cancel.
        """
```

### C3. `backend/db.py` — `Database` Exact Method Signatures & Return Shapes
`backend/db.py` provides synchronous SQLite CRUD methods. Each connection enables `PRAGMA journal_mode=WAL;`, `PRAGMA foreign_keys=ON;`, and `conn.row_factory = sqlite3.Row`. Returned dicts must match the JSON shapes in `ARCHITECTURE.md` §10:
- In `turns` dicts: `denied_actions` is deserialized from `denied_actions_json` into a Python `List[str]` (and `denied_actions_json` is omitted or kept internal so the dict has `"denied_actions": [...]` matching `ARCHITECTURE.md` §10.2).
- In `pipeline_stages` and `pipeline_stage_runs` dicts: `require_approval` is converted from SQLite `0`/`1` integer to Python `bool` (`True`/`False`).
- In `pipelines` dicts: includes `"stages": List[Dict[str, Any]]` ordered by `stage_order ASC`.

Exact `Database` methods:
```python
class Database:
    def __init__(self, db_path: Path) -> None:
        """Stores db_path and calls self.init_db()."""

    def init_db(self) -> None:
        """Executes schema DDL from ARCHITECTURE.md §9."""

    def recover_orphaned_states(self) -> None:
        """Implements startup crash recovery per ARCHITECTURE.md §9."""

    # --- Sessions ---
    def create_session(
        self,
        name: str,
        provider: str,
        model: str,
        role: str,
        custom_prompt: str,
        permission_level: str,
        working_dir: str,
    ) -> Dict[str, Any]: ...

    def list_sessions(self) -> List[Dict[str, Any]]: ...
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]: ...
    def update_session(
        self,
        session_id: str,
        name: Optional[str] = None,
        permission_level: Optional[str] = None,
        model: Optional[str] = None,
        custom_prompt: Optional[str] = None,
        status: Optional[str] = None,
        provider_session_id: Optional[str] = None,
        turn_count: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]: ...
    def update_session_provider_id(self, session_id: str, provider_session_id: str) -> Optional[Dict[str, Any]]: ...
    def is_session_bound_to_pipeline(self, session_id: str) -> bool: ...
    def delete_session(self, session_id: str) -> bool: ...

    # --- Turns ---
    def create_turn(
        self,
        session_id: str,
        prompt: str,
        pipeline_run_id: Optional[str] = None,
        stage_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Atomically increments sessions.turn_count (turn_number = new turn_count),
        sets sessions.status = 'running', inserts turn with status='running', and returns turn dict.
        """

    def update_turn(
        self,
        turn_id: str,
        status: str,
        response_text: str = "",
        error_message: Optional[str] = None,
        tool_call_count: int = 0,
        failed_tool_count: int = 0,
        denied_actions: Optional[List[str]] = None,
        finished_at: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]: ...

    def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]: ...
    def list_turns_for_session(self, session_id: str) -> List[Dict[str, Any]]: ...
    def list_all_turns(self, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Lists turns across all sessions ordered by started_at DESC, joined with
        session_name, provider, model, and role for the Run History 'All Session Turns' view.
        """

    # --- Pipelines ---
    def create_pipeline(
        self,
        name: str,
        description: str,
        max_review_loops: int,
        stages: List[Dict[str, Any]],
    ) -> Dict[str, Any]: ...

    def update_pipeline(
        self,
        pipeline_id: str,
        name: str,
        description: str,
        max_review_loops: int,
        stages: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]: ...

    def get_pipeline(self, pipeline_id: str) -> Optional[Dict[str, Any]]: ...
    def list_pipelines(self) -> List[Dict[str, Any]]: ...
    def has_active_pipeline_runs(self, pipeline_id: str) -> bool: ...
    def delete_pipeline(self, pipeline_id: str) -> bool: ...

    # --- Pipeline Runs & Stage Runs ---
    def create_pipeline_run(
        self,
        pipeline_id: str,
        pipeline_name: str,
        task_prompt: str,
        max_review_loops: int,
        status: str = "running",
    ) -> Dict[str, Any]: ...

    def update_pipeline_run(
        self,
        run_id: str,
        status: Optional[str] = None,
        current_stage_order: Optional[int] = None,
        review_loop_count: Optional[int] = None,
        error_message: Optional[str] = None,
        finished_at: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]: ...

    def get_pipeline_run(self, run_id: str) -> Optional[Dict[str, Any]]: ...
    def list_pipeline_runs(self) -> List[Dict[str, Any]]: ...

    def create_stage_run(
        self,
        pipeline_run_id: str,
        stage_id: str,
        stage_order: int,
        stage_name: str,
        session_id: str,
        session_name: str,
        provider: str,
        model: str,
        role: str,
        iteration: int,
        require_approval: bool,
        input_prompt: str,
        status: str = "running",
        started_at: Optional[str] = None,
    ) -> Dict[str, Any]: ...

    def update_stage_run(
        self,
        stage_run_id: str,
        turn_id: Optional[str] = None,
        output_text: Optional[str] = None,
        verdict: Optional[str] = None,
        findings: Optional[str] = None,
        approval_note: Optional[str] = None,
        status: Optional[str] = None,
        error_message: Optional[str] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]: ...

    def get_stage_run(self, stage_run_id: str) -> Optional[Dict[str, Any]]: ...
    def list_stage_runs(self, pipeline_run_id: str) -> List[Dict[str, Any]]: ...
```

### C4. `backend/providers/__init__.py` — Provider Registry Functions
`backend/providers/__init__.py` MUST export:
```python
from typing import Any, Dict, List
from backend.config import AppConfig
from backend.models import ProviderId
from backend.providers.base import ProviderAdapter
from backend.providers.claude import ClaudeAdapter
from backend.providers.antigravity import AntigravityAdapter

def get_adapter(provider: ProviderId, config: AppConfig) -> ProviderAdapter:
    """Returns ClaudeAdapter(config.claude_bin) or AntigravityAdapter(config.agy_bin)."""

async def list_providers_metadata(config: AppConfig) -> List[Dict[str, Any]]:
    """
    Returns the `providers` list for `GET /api/meta`:
    [
        {"id": "claude", "name": "Claude Code", "models": await claude_adapter.list_models()},
        {"id": "antigravity", "name": "Antigravity (AGY)", "models": await agy_adapter.list_models()},
    ]
    """
```
- `ClaudeAdapter.__init__(self, claude_bin: str = "claude") -> None` sets `self.provider_id = ProviderId.CLAUDE` and `self.display_name = "Claude Code"`.
- `AntigravityAdapter.__init__(self, agy_bin: str = "agy") -> None` sets `self.provider_id = ProviderId.ANTIGRAVITY` and `self.display_name = "Antigravity (AGY)"`.

### C5. Error Handling & Domain Exceptions across `SessionManager`, `PipelineEngine`, and `main.py`
To keep HTTP layer concerns clean while mapping domain errors to exact status codes (`400`, `404`, `409`):
- Define three lightweight exception classes in `backend/models.py`:
  - `class NotFoundError(Exception): pass` -> mapped to `404 Not Found`
  - `class ConflictError(Exception): pass` -> mapped to `409 Conflict`
  - `class ValidationError(Exception): pass` -> mapped to `400 Bad Request`
- `SessionManager.start_turn` / `run_turn_and_wait`:
  - Raises `NotFoundError` if `session_id` does not exist.
  - Raises `ConflictError` if `self.is_session_running(session_id)` is `True`.
- `PipelineEngine`:
  - `validate_pipeline_stages(db: Database, stages: List[Dict[str, Any]]) -> None` (helper or method on `PipelineEngine`):
    - Raises `ValidationError` if `stages` is empty.
    - Raises `ValidationError` if any stage's `session_id` does not exist in `db`.
    - Raises `ValidationError` if any stage is bound to a session with `role == "reviewer"` and there is no preceding stage (`idx < reviewer_idx`) bound to a session with `role == "implementer"`.
  - `start_pipeline_run(pipeline_id, task_prompt)` returns `{"run": run_dict, "stage_runs": stage_runs_list}` (`PipelineRunDetailObject`). Raises `NotFoundError` if `pipeline_id` is not found; raises `ConflictError` if any session bound to any stage of the pipeline currently has `session_manager.is_session_running(session_id) == True` or `session["status"] == "running"`.
  - `approve_pipeline_run(run_id, note)` returns `{"run": run_dict, "stage_runs": stage_runs_list}`. Raises `NotFoundError` if `run_id` not found; raises `ConflictError` if `run["status"] != "waiting_approval"`.
  - `stop_pipeline_run(run_id)` returns `{"run": run_dict, "stage_runs": stage_runs_list}`. Raises `NotFoundError` if `run_id` not found.

### C6. Additional `GET /api/turns` Endpoint for Run History "All Session Turns" Tab
`DESIGN.md` §4.5 specifies that the Run History view has two sub-filter tabs: `"Pipeline Runs"` and `"All Session Turns"`.
- In addition to `GET /api/turns/{turn_id}`, `backend/main.py` MUST expose `GET /api/turns` returning `200 OK` with `{"turns": db.list_all_turns()}` so the frontend can populate the `"All Session Turns"` list in one call.

### C7. Frontend ES Module Exports & Shared State Contract
The frontend uses native ES2020 modules (`<script type="module" src="/js/app.js"></script>`). To guarantee `Wave 2` and `Wave 3` frontend tasks integrate cleanly, their module exports are fixed as follows:
1. **`frontend/js/api.js`** exports an `api` object (and named functions):
   - `getMeta()` -> `GET /api/meta`
   - `listSessions()` -> `GET /api/sessions`
   - `createSession(payload)` -> `POST /api/sessions`
   - `getSession(sessionId)` -> `GET /api/sessions/${sessionId}`
   - `updateSession(sessionId, payload)` -> `PATCH /api/sessions/${sessionId}`
   - `deleteSession(sessionId)` -> `DELETE /api/sessions/${sessionId}`
   - `startTurn(sessionId, payload)` -> `POST /api/sessions/${sessionId}/turns`
   - `stopSession(sessionId)` -> `POST /api/sessions/${sessionId}/stop`
   - `listTurns()` -> `GET /api/turns`
   - `getTurn(turnId)` -> `GET /api/turns/${turnId}`
   - `listPipelines()` -> `GET /api/pipelines`
   - `createPipeline(payload)` -> `POST /api/pipelines`
   - `updatePipeline(pipelineId, payload)` -> `PUT /api/pipelines/${pipelineId}`
   - `deletePipeline(pipelineId)` -> `DELETE /api/pipelines/${pipelineId}`
   - `startPipelineRun(pipelineId, payload)` -> `POST /api/pipelines/${pipelineId}/runs`
   - `listPipelineRuns()` -> `GET /api/pipeline-runs`
   - `getPipelineRun(runId)` -> `GET /api/pipeline-runs/${runId}`
   - `approvePipelineRun(runId, payload)` -> `POST /api/pipeline-runs/${runId}/approve`
   - `stopPipelineRun(runId)` -> `POST /api/pipeline-runs/${runId}/stop`
2. **`frontend/js/components.js`** exports pure DOM/HTML helper functions:
   - `escapeHtml(str)`
   - `renderMarkdown(text)` — Safely escapes HTML first, then formats fenced code blocks (`` ```lang ... ``` ``), inline code (`` `...` ``), headings (`#`..`###`), bold (`**...**`), bullet lists, and Reviewer verdict banners (`VERDICT: APPROVED` / `VERDICT: CHANGES_REQUESTED`).
   - `renderProviderBadge(providerId)` — Returns HTML string for `.badge-provider` (`Claude` or `AGY`).
   - `renderRoleBadge(roleId)` — Returns HTML string for `.badge-role`.
   - `renderPermissionBadge(permissionLevel)` — Returns HTML string for `.badge-permission`.
   - `renderStatusDot(status)` — Returns HTML string for `.status-dot`.
   - `renderStatusBadge(status)` — Returns HTML string for status pill.
   - `renderToolCard(eventOrToolState)` — Returns HTML or DOM element for `.tool-card` (collapsible `<details>` with tool name, primary param summary, and pretty-printed JSON input/output).
   - `formatDuration(durationMs)` — Formats ms into `"14.2s"` or `"1m 05s"`.
   - `formatTimestamp(isoStr)` — Formats ISO timestamp into readable local time.
   - `showToast(message, type = "info")` — Renders a toast notification into `#toast-container`.
3. **`frontend/js/app.js`** exports shared application state and navigation helpers:
   - `store`: shared reactive state object containing `{ meta, sessions, activeSessionId, activeSessionTurns, liveTurnEvents, pipelines, activePipelineId, pipelineRuns, activeRunId, activeRunDetail, allTurns, activeView, sseConnected }`.
   - `switchView(viewName, options = {})` — Switches top-level view (`"sessions"` | `"pipelines"` | `"history"`) and optionally selects a `sessionId` or `runId`.
   - `refreshSessions()`, `refreshPipelines()`, `refreshRuns()` — Re-fetches lists from API and triggers view re-render.
4. **`frontend/js/sessions.js`** exports:
   - `initSessionsView(store, callbacks)`
   - `renderSessionsSidebar()`
   - `renderSessionChatView()`
   - `openNewSessionModal()`
   - `handleSessionSseEvent(eventName, data)`
5. **`frontend/js/pipelines.js`** exports:
   - `initPipelinesView(store, callbacks)`
   - `renderPipelineBuilder()`
   - `handlePipelineSseEvent(eventName, data)`
6. **`frontend/js/runs.js`** exports:
   - `initRunsView(store, callbacks)`
   - `renderActiveRunPanel()`
   - `renderHistoryView()`
   - `handleRunSseEvent(eventName, data)`

### C8. DOM IDs in `frontend/index.html`
`frontend/index.html` MUST provide these exact container IDs so `app.js`, `sessions.js`, `pipelines.js`, `runs.js`, and `components.js` bind without mismatch:
- Top bar: `#nav-tab-sessions`, `#nav-tab-pipelines`, `#nav-tab-history`, `#sse-status-indicator`
- Sidebar: `#sessions-sidebar`, `#btn-new-session`, `#sessions-list`
- Main views (toggled via `.hidden` class):
  - `#view-sessions`: contains `#session-chat-empty`, `#session-chat-active`, `#session-header`, `#chat-transcript`, `#session-prompt-form`, `#session-prompt-input`, `#btn-send-turn`, `#btn-stop-turn`
  - `#view-pipelines`: contains mobile sub-tab bar (`#pipe-subtab-builder`, `#pipe-subtab-run`), `#pipeline-builder-panel`, `#pipeline-active-run-panel`
  - `#view-history`: contains `#history-list-panel` (with sub-tabs `#history-tab-runs`, `#history-tab-turns`, list `#history-items-list`) and `#history-detail-panel`
- Modal: `#new-session-modal`, `#new-session-form`
- Toast container: `#toast-container`

---

## 3. Wave & Task Breakdown

### Wave Summary
- **Wave 1 (Foundation — Parallel)**:
  - **Task 1**: Core Backend Configuration, Models, Roles, PubSub & SQLite Persistence
  - **Task 2**: Frontend Shell, CSS Design System, API Client & Shared UI Components
- **Wave 2 (Adapters & Session UI — Parallel)**:
  - **Task 3**: CLI Provider Adapters (`base`, `claude`, `antigravity`)
  - **Task 4**: Frontend App Router, SSE Store & Sessions Chat View (`app.js`, `sessions.js`)
- **Wave 3 (Orchestration Engine & Pipeline/History UI — Parallel)**:
  - **Task 5**: Session Manager & Pipeline Orchestration Engine (`session_manager.py`, `pipeline_engine.py`)
  - **Task 6**: Frontend Pipeline Builder, Live Run Timeline & Run History Views (`pipelines.js`, `runs.js`)
- **Wave 4 (HTTP Server Assembly & Automated Test Suite — Parallel)**:
  - **Task 7**: FastAPI Application, REST + SSE Endpoints & Project Documentation (`backend/main.py`, `README.md`)
  - **Task 8**: Fake CLI Executables & Comprehensive Pytest Suite (`tests/*`)

---

### Task 1: Core Backend Configuration, Models, Roles, PubSub & SQLite Persistence

- **ID**: `Task 1`
- **Title**: Core Backend Configuration, Models, Roles, PubSub & SQLite Persistence
- **Wave**: `1`
- **Complexity**: `medium`
- **Files to create (exclusive ownership)**:
  - `requirements.txt`
  - `backend/__init__.py`
  - `backend/config.py`
  - `backend/models.py`
  - `backend/roles.py`
  - `backend/pubsub.py`
  - `backend/db.py`
- **Files/sections to read first**:
  - `docs/ARCHITECTURE.md` §2 (`requirements.txt`), §4 (Data Directory Layout), §5.1 (`backend/config.py`), §5.2 (`backend/models.py`), §5.3 (`backend/roles.py`), §9 (`backend/db.py` schema & crash recovery), §10 (API response object shapes), §11 (Security Architecture)
  - `docs/PLAN.md` §2 Clarifications `C1`, `C2`, `C3`, `C5`
- **Precise instructions**:
  1. Create `requirements.txt` with the exact 5 packages and versions in `ARCHITECTURE.md` §2.
  2. Create `backend/__init__.py` (can be empty or export package version).
  3. Implement `backend/config.py`:
     - Define `FORBIDDEN_PORTS = {5000, 7000, 5090, 5186}`, `DEFAULT_PORT = 8765`, `DEFAULT_HOST = "127.0.0.1"`, and `@dataclass(frozen=True) class AppConfig`.
     - Implement `load_config() -> AppConfig`:
       - Read `AGENT_RELAY_PORT` (default `8765`), parse as `int`, raise `ValueError` if port is in `FORBIDDEN_PORTS` or outside `1..65535`.
       - Read `AGENT_RELAY_HOME` (default `~/.agent-relay`), expand `~`, create `data_dir` and `turns_dir` (`data_dir / "turns"`) with `mode=0o700` (`os.chmod(path, 0o700)` after `mkdir(parents=True, exist_ok=True)`).
       - Resolve `claude_bin` (`os.environ.get("CLAUDE_BIN")` or `~/.local/bin/claude` if executable or `shutil.which("claude")` or `"claude"`) and `agy_bin` (`os.environ.get("AGY_BIN")` or `~/.local/bin/agy` if executable or `shutil.which("agy")` or `"agy"`).
       - Read `AGENT_RELAY_TURN_TIMEOUT` (default `600`).
     - Implement `validate_working_dir(path_str: str) -> str`:
       - Expand `~` (`os.path.expanduser`), resolve symlinks (`os.path.realpath`).
       - Raise `ValueError` if empty, does not exist, or is not a directory (`not os.path.isdir(real)`).
       - Reject `"/"` and any path equal to or starting with `p + "/"` for `p` in `("/System", "/etc", "/private/etc", "/var", "/private/var", "/usr", "/bin", "/sbin", "/dev", "/Library")`. Note: on macOS `/tmp` and `/var/folders/...` (used by `pytest` `tmp_path`) resolve under `/private/tmp` or `/private/var/folders/...`. **Important edge case**: allow `/private/tmp` and `/private/var/folders/` so `tmp_path` directories in unit tests and user temp workspaces work while blocking `/var` and `/private/var` system directories (e.g. `/private/var/db`, `/private/var/root`, `/private/var/log`, or `/private/var` itself). Specifically, if `real` starts with `"/private/var/folders/"` or `"/private/tmp/"` (or equals `"/private/tmp"`), allow it before checking the blocked prefix list.
  4. Implement `backend/models.py`:
     - Include all enums and `NormalizedEvent` from `ARCHITECTURE.md` §5.2.
     - Include `utc_now_iso()`, domain exceptions (`NotFoundError`, `ConflictError`, `ValidationError`), and all Pydantic request schemas specified in Clarification `C1` and `C5`.
  5. Implement `backend/roles.py`:
     - Implement `RoleDefinition`, `ROLE_DEFINITIONS`, and `resolve_system_prompt(role: RoleId, custom_prompt: Optional[str] = None) -> str` exactly as specified in `ARCHITECTURE.md` §5.3.
     - If `role != RoleId.CUSTOM` and `custom_prompt` is non-empty after `.strip()`, return `f"{base_prompt}\n\nAdditional Instructions:\n{custom_prompt.strip()}"`.
  6. Implement `backend/pubsub.py`:
     - Implement `EventBus` with `publish(event_name, data)` and `subscribe(heartbeat_sec=15.0)` per Clarification `C2`.
  7. Implement `backend/db.py`:
     - Implement `Database` with all SQLite tables and indexes in `ARCHITECTURE.md` §9 and all methods in Clarification `C3`.
     - Ensure every connection sets `PRAGMA journal_mode=WAL;` and `PRAGMA foreign_keys=ON;`.
     - Ensure `recover_orphaned_states()` marks orphaned `'running'` turns as `'error'` (`'Server restarted while turn was in progress'`), `'running'` sessions as `'idle'`, and `'running'`/`'waiting_approval'` pipeline runs and stage runs as `'failed'` (`'Server restarted while pipeline was active'`).
  8. **What NOT to do**: Do not create provider files, session manager, pipeline engine, or `main.py`. Do not use Python 3.10+ union syntax (`str | None`).
- **Definition of done**:
  - `python3 -c "from backend import config, models, roles, pubsub, db"` succeeds with zero errors under Python 3.9.
  - `Database(tmp_path / "state.db")` creates all 6 tables (`sessions`, `turns`, `pipelines`, `pipeline_stages`, `pipeline_runs`, `pipeline_stage_runs`) and supports full CRUD and `recover_orphaned_states()`.
  - `validate_working_dir("/etc")` and `validate_working_dir("/")` raise `ValueError`, while `validate_working_dir(os.getcwd())` returns the canonical path.

---

### Task 2: Frontend Shell, CSS Design System, API Client & Shared UI Components

- **ID**: `Task 2`
- **Title**: Frontend Shell, CSS Design System, API Client & Shared UI Components
- **Wave**: `1`
- **Complexity**: `medium`
- **Files to create (exclusive ownership)**:
  - `frontend/index.html`
  - `frontend/styles.css`
  - `frontend/js/api.js`
  - `frontend/js/components.js`
- **Files/sections to read first**:
  - `docs/DESIGN.md` §1 (Design Philosophy), §2 (Visual Style, CSS Variables, Badges & Status Dots), §3 (Application Shell & Responsive Layout `1440px` to `800px`), §4.1–4.5 (Screen Specifications)
  - `docs/ARCHITECTURE.md` §10 (HTTP API endpoints & payload formats)
  - `docs/PLAN.md` §2 Clarifications `C6`, `C7`, `C8`
- **Precise instructions**:
  1. Create `frontend/index.html`:
     - Semantic HTML5 shell loading `/styles.css` and `<script type="module" src="/js/app.js"></script>`.
     - Include all required DOM IDs specified in Clarification `C8`:
       - 52px top navigation bar with brand title (`⚡ Agent Relay`), view switcher buttons (`#nav-tab-sessions`, `#nav-tab-pipelines`, `#nav-tab-history`), and SSE connection pill (`#sse-status-indicator`).
       - Persistent left sidebar (`#sessions-sidebar`) with header, `+ New Session` button (`#btn-new-session`), and `#sessions-list`.
       - Main workspace housing `#view-sessions`, `#view-pipelines`, and `#view-history`.
       - `#new-session-modal` dialog overlay containing `#new-session-form` with inputs for Session Name, Provider toggle (`claude` / `antigravity`), Model `<select>` + custom model input, Role `<select>`, collapsible role system prompt preview + `custom_prompt` textarea, Permission Level `<select>` with override warning hint, Working Directory `<input>`, inline error box, and Cancel/Create buttons.
       - `#toast-container` for transient alerts.
  2. Create `frontend/styles.css`:
     - Include the exact `:root` light and `@media (prefers-color-scheme: dark)` CSS variable palettes from `DESIGN.md` §2.1.
     - Style the application grid (52px header, `var(--sidebar-width)` left sidebar + `1fr` main workspace), session cards, chat transcript bubbles, collapsible `<details class="thinking-block">` and `<details class="tool-card">`, markdown code blocks, verdict banners (`.verdict-banner.approved`, `.verdict-banner.changes-requested`), pipeline stage cards, sticky approval banner, run history split view, modals, and toasts.
     - Implement `@keyframes pulse` for `.status-dot.running` and `.status-dot.waiting_approval`.
     - Implement the responsive breakpoints from `DESIGN.md` §3 (`1024px` and `800px`) so no horizontal scrollbars occur at `800px` width.
  3. Create `frontend/js/api.js`:
     - Implement the typed `fetch` wrapper and export both `api` object and named functions for every endpoint listed in Clarification `C7.1`.
     - Parse JSON responses; on non-2xx responses, extract `body.detail || res.statusText` and throw an `Error` with `err.status = res.status` and `err.message = detail`.
  4. Create `frontend/js/components.js`:
     - Implement and export all reusable helpers listed in Clarification `C7.2` (`escapeHtml`, `renderMarkdown`, `renderProviderBadge`, `renderRoleBadge`, `renderPermissionBadge`, `renderStatusDot`, `renderStatusBadge`, `renderToolCard`, `formatDuration`, `formatTimestamp`, `showToast`).
     - Ensure `renderMarkdown(text)` always escapes raw HTML entities (`&`, `<`, `>`, `"`, `'`) BEFORE applying markdown transformations to prevent XSS when displaying model output or code diffs.
  5. **What NOT to do**: Do not create `app.js`, `sessions.js`, `pipelines.js`, or `runs.js`. Do not reference any external CDN fonts, scripts, or stylesheets.
- **Definition of done**:
  - `frontend/index.html` contains all required element IDs from Clarification `C8`.
  - `frontend/styles.css` defines all light/dark CSS variables and responsive breakpoints down to `800px`.
  - `frontend/js/api.js` and `frontend/js/components.js` use valid ES2020 module syntax and export every symbol in Clarification `C7.1` and `C7.2`.

---

### Task 3: CLI Provider Adapters (`base`, `claude`, `antigravity`)

- **ID**: `Task 3`
- **Title**: CLI Provider Adapters (`base`, `claude`, `antigravity`)
- **Wave**: `2` (depends on Task 1)
- **Complexity**: `high`
- **Files to create (exclusive ownership)**:
  - `backend/providers/__init__.py`
  - `backend/providers/base.py`
  - `backend/providers/claude.py`
  - `backend/providers/antigravity.py`
- **Files/sections to read first**:
  - `docs/ARCHITECTURE.md` §6.1 (`ProviderAdapter` & `StreamParseState`), §6.2 (Permission Mapping Table), §6.3 (`ClaudeAdapter`), §6.4 (`AntigravityAdapter`)
  - `docs/reference/claude-cli-help.txt`, `docs/reference/agy-cli-help.txt`, `docs/reference/gem-script.sh`
  - `docs/PLAN.md` §2 Clarification `C4`
- **Precise instructions**:
  1. Implement `backend/providers/base.py`:
     - Define `StreamParseState` and `ProviderAdapter(ABC)` exactly as specified in `ARCHITECTURE.md` §6.1.
  2. Implement `backend/providers/claude.py` (`ClaudeAdapter`):
     - Map `PermissionLevel` to exact flags (`ARCHITECTURE.md` §6.2):
       - `READONLY`: `["--permission-mode", "plan", "--permission-prompts", "none", "--disallowedTools", "Edit,Write,NotebookEdit,Bash"]`
       - `EDIT`: `["--permission-mode", "acceptEdits", "--permission-prompts", "none"]`
       - `FULL`: `["--permission-mode", "bypassPermissions", "--dangerously-skip-permissions"]`
     - Implement `build_command(...)` for first turn (`is_first_turn=True` or `not provider_conversation_id`) and subsequent turns (`--resume <provider_conversation_id>`) per `ARCHITECTURE.md` §6.3. Include `["--append-system-prompt", system_prompt]` on first turn when `system_prompt` is non-empty. Always place `"--", prompt` at the end so prompts starting with `-` are never misinterpreted as flags.
     - Implement `parse_stream_line(line, state)`:
       - Strip whitespace; return `[]` if empty or invalid JSON (`json.JSONDecodeError`).
       - Capture `session_id` if present and `state.provider_conversation_id is None`.
       - Handle `system` (`subtype == "init"` -> `SESSION_INIT`), `stream_event` (`content_block_delta` -> `TEXT_DELTA` / `THINKING_DELTA`), `assistant` (`text`, `thinking`, `tool_use` -> `TOOL_USE`), `user` (`tool_result` -> `TOOL_RESULT`), and `result` (`TURN_RESULT`, setting `state.final_status`, `state.final_response`, `state.error_message`) per `ARCHITECTURE.md` §6.3.
       - Every emitted `NormalizedEvent` gets `seq=state.next_seq()` and `timestamp=utc_now_iso()`.
     - Implement `list_models()` returning the 5 Claude model entries from `ARCHITECTURE.md` §6.3.
  3. Implement `backend/providers/antigravity.py` (`AntigravityAdapter`):
     - Map `PermissionLevel` to exact flags (`ARCHITECTURE.md` §6.2):
       - `READONLY`: `["--mode", "plan"]`
       - `EDIT`: `["--mode", "accept-edits"]`
       - `FULL`: `["--mode", "accept-edits", "--dangerously-skip-permissions"]`
     - Implement `build_command(...)` per `ARCHITECTURE.md` §6.4:
       - On `is_first_turn=True` (or when `provider_conversation_id` is `None`), if `system_prompt` is non-empty after `.strip()`, construct `effective_prompt = f"{system_prompt.strip()}\n\n# Task\n\n{prompt}"`; otherwise `effective_prompt = prompt`.
       - On subsequent turns (`provider_conversation_id` is set), include `["--conversation", provider_conversation_id]` and pass `prompt` directly (`-p`, `prompt`).
       - Include `["--model", model]` if `model` is non-empty.
     - Implement `parse_stream_line(line, state)` per `ARCHITECTURE.md` §6.4:
       - Ignore blank or non-JSON lines safely.
       - Capture `conversation_id` from top-level `obj.get("conversation_id")` or `(obj.get("result") or {}).get("conversation_id")`. When first seen, set `state.provider_conversation_id` and emit `NormalizedEvent(type=SESSION_INIT, provider_conversation_id=...)`.
       - Parse `step_update` for `agent_response` (`TEXT_DELTA`), `thinking` (`THINKING_DELTA`), and `tool` (`TOOL_USE` on first sight of `step_index`, `TOOL_RESULT` when `state in ("DONE", "ERROR", "COMPLETED")`).
       - Parse `obj.get("event") == "result"`: set `state.final_status`, `state.final_response`, `state.denied_actions`, `state.error_message`, and emit `NormalizedEvent(type=TURN_RESULT, ...)`.
     - Implement `list_models()` per `ARCHITECTURE.md` §6.4: run `asyncio.create_subprocess_exec(self.agy_bin, "models", stdin=DEVNULL, stdout=PIPE, stderr=PIPE)` with a 5.0s timeout; parse model IDs matching `^[a-z0-9][a-z0-9._-]+$` (filtering out common words like `"available"`, `"models"`, `"default"`, `"usage"`) and merge with `FALLBACK_AGY_MODELS` so the 3 fallback models are always available even if the binary is absent or fails.
  4. Implement `backend/providers/__init__.py`:
     - Export `get_adapter` and `list_providers_metadata` per Clarification `C4`.
  5. **What NOT to do**: Never use `shell=True`. Never raise an unhandled exception on malformed stdout lines from a CLI.
- **Definition of done**:
  - `ClaudeAdapter` and `AntigravityAdapter` produce the exact argv lists for all 3 permission levels on both Turn 1 and Turn 2+.
  - `parse_stream_line` accurately normalizes init, text deltas, thinking deltas, tool calls, tool results, denied actions, and final results for both providers.

---

### Task 4: Frontend App Router, SSE Store & Sessions Chat View (`app.js`, `sessions.js`)

- **ID**: `Task 4`
- **Title**: Frontend App Router, SSE Store & Sessions Chat View (`app.js`, `sessions.js`)
- **Wave**: `2` (depends on Task 2)
- **Complexity**: `high`
- **Files to create (exclusive ownership)**:
  - `frontend/js/app.js`
  - `frontend/js/sessions.js`
- **Files/sections to read first**:
  - `docs/DESIGN.md` §3 (Application Shell), §4.1 (Sidebar Sessions List), §4.2 (New Session Modal), §4.3 (Session Chat View, Transcript, Tool Cards, Verdict Banner, Composer Bar), §5 (Interaction Flows)
  - `docs/ARCHITECTURE.md` §10.1–10.2 (Meta & Session APIs), §10.4 (SSE event payloads)
  - `docs/PLAN.md` §2 Clarifications `C7`, `C8`
- **Precise instructions**:
  1. Implement `frontend/js/app.js`:
     - Define and export `store`, `switchView`, `refreshSessions`, `refreshPipelines`, `refreshRuns` per Clarification `C7.3`.
     - On `DOMContentLoaded`:
       - Fetch `/api/meta`, `/api/sessions`, `/api/pipelines`, and `/api/pipeline-runs` in parallel via `api.js`.
       - Initialize `initSessionsView(store, ...)`, `initPipelinesView(store, ...)`, and `initRunsView(store, ...)`.
       - Bind top bar navigation tabs (`#nav-tab-sessions`, `#nav-tab-pipelines`, `#nav-tab-history`).
       - Connect to `/api/events` using browser `EventSource`.
     - Manage `EventSource("/api/events")`:
       - Update `#sse-status-indicator` (`SSE Connected` green dot when open; `Reconnecting...` amber dot on error).
       - Listen to named SSE events: `session_updated`, `turn_started`, `turn_event`, `turn_completed`, `pipeline_run_updated`, `pipeline_stage_updated`, `pipeline_waiting_approval`, and `ping`.
       - Route events to `handleSessionSseEvent`, `handlePipelineSseEvent`, and `handleRunSseEvent` so UI state updates live without polling.
  2. Implement `frontend/js/sessions.js`:
     - Export `initSessionsView`, `renderSessionsSidebar`, `renderSessionChatView`, `openNewSessionModal`, and `handleSessionSseEvent` per Clarification `C7.4`.
     - **Sidebar (`renderSessionsSidebar`)**:
       - Render `.session-card` items showing status dot, session name, inline `■` stop button when `status === 'running'`, Provider pill, Role pill, model name, turn count, and basename of `working_dir` (with `title` attribute showing full path).
       - Clicking a card selects the session (`store.activeSessionId = session.id`), loads its turns (`GET /api/sessions/{id}`), and switches to `#view-sessions`.
       - Render the empty state card with `+ Create First Session` button when `store.sessions` is empty.
     - **New Session Modal (`openNewSessionModal`)**:
       - Populate Provider toggle, Model dropdown from `store.meta.providers`, Role dropdown from `store.meta.roles`, Permission Level dropdown from `store.meta.permissions`, and Working Directory input (`store.meta.default_working_dir`).
       - Changing Provider immediately updates the Model dropdown; selecting `"Other / Custom model ID..."` reveals a custom text input.
       - Changing Role updates the role description, system prompt preview, and sets Permission Level to `role.default_permission`.
       - Selecting `edit` or `full` on a role whose default is `readonly` shows the amber override hint (`"Override active: this role is read-only by default."`).
       - Submitting calls `api.createSession(...)`, closes the modal, refreshes sessions, and selects the newly created session. Display inline error in the modal if the API returns `400`.
     - **Session Chat View (`renderSessionChatView`)**:
       - **Header Bar**: session name, status badge, provider/model/role badges, `working_dir`, conversation ID badge (`conv: <short-id>` or `not started`), Permission Level quick-override `<select>` (calls `PATCH /api/sessions/{id}` immediately when changed; disabled while running), and Delete button (`DELETE /api/sessions/{id}` with confirmation; shows toast if `409 Conflict`).
       - **Transcript (`#chat-transcript`)**:
         - For completed/historical turns, allow expanding tool calls (fetches `GET /api/turns/{turn_id}` on demand if events aren't already cached in `store.liveTurnEvents[turn_id]`).
         - For a live turn (`turn_started` / `turn_event` / `turn_completed`), stream `thinking_delta` into a collapsible `<details class="thinking-block">` (auto-collapses once `text_delta` starts), render `tool_use` / `tool_result` cards inline via `renderToolCard`, and stream `text_delta` into `.assistant-markdown` rendered via `renderMarkdown`.
         - Display turn footer with duration, tool call count, `denied_actions` warning banner if non-empty, error alert box on `error`/`timeout`, or stopped badge on `stopped`.
       - **Composer Bar**:
         - `textarea#session-prompt-input` submits on `Cmd+Enter` / `Ctrl+Enter` or clicking `#btn-send-turn`.
         - While `session.status === 'running'`, disable textarea, show live elapsed timer, and show `#btn-stop-turn` (`■ Stop Turn`) which calls `api.stopSession(session.id)`.
  3. **What NOT to do**: Do not edit `index.html`, `styles.css`, `api.js`, `components.js`, `pipelines.js`, or `runs.js`.
- **Definition of done**:
  - `app.js` and `sessions.js` export all functions in Clarification `C7.3` and `C7.4`.
  - Creating a session, changing permissions, sending a turn, streaming SSE `turn_event` deltas/tool calls, and stopping a running turn all work end-to-end in the UI.

---

### Task 5: Session Manager & Pipeline Orchestration Engine (`session_manager.py`, `pipeline_engine.py`)

- **ID**: `Task 5`
- **Title**: Session Manager & Pipeline Orchestration Engine (`session_manager.py`, `pipeline_engine.py`)
- **Wave**: `3` (depends on Tasks 1 and 3)
- **Complexity**: `high`
- **Files to create (exclusive ownership)**:
  - `backend/session_manager.py`
  - `backend/pipeline_engine.py`
- **Files/sections to read first**:
  - `docs/ARCHITECTURE.md` §7 (`SessionManager` public interface, subprocess execution, process groups, timeout & stop logic, completion determination), §8 (`PipelineEngine` state machine, exact handoff prompt templates Case A–D, `parse_reviewer_verdict`, review rework loop flow, per-stage approval pause/resume), §10.4 (SSE event names & payloads)
  - `docs/PLAN.md` §2 Clarifications `C1`, `C2`, `C3`, `C4`, `C5`
- **Precise instructions**:
  1. Implement `backend/session_manager.py` (`SessionManager`):
     - Follow the exact class signature in `ARCHITECTURE.md` §7.2.
     - Track active subprocesses in `self._active_procs: Dict[str, asyncio.subprocess.Process]`, active turn tasks in `self._active_tasks: Dict[str, asyncio.Task[Dict[str, Any]]]`, and stopped turn IDs in `self._stopped_turns: Set[str]`.
     - `is_session_running(session_id: str) -> bool`: returns `session_id in self._active_procs`.
     - `start_turn(...) -> str`:
       - Fetch session from `self.db.get_session(session_id)`; if `None`, raise `NotFoundError(f"Session {session_id} not found")`.
       - If `self.is_session_running(session_id)`: raise `ConflictError(f"Session {session_id} is already running a turn")`.
       - Call `turn = self.db.create_turn(session_id, prompt, pipeline_run_id, stage_run_id)`.
       - Publish `session_updated` and `turn_started` to `self.bus`.
       - Launch `task = asyncio.create_task(self._execute_turn(session, turn, timeout_sec))` and store in `self._active_tasks[turn["id"]]`.
       - Return `turn["id"]`.
     - `run_turn_and_wait(...) -> Dict[str, Any]`:
       - Call `turn_id = await self.start_turn(...)`, then `await self._active_tasks[turn_id]` and return `self.db.get_turn(turn_id)`.
     - `_execute_turn(session, turn, timeout_sec)`:
       - Resolve adapter via `get_adapter(ProviderId(session["provider"]), self.config)`.
       - Determine `is_first_turn = (session.get("provider_session_id") is None)`.
       - Resolve system prompt via `resolve_system_prompt(RoleId(session["role"]), session.get("custom_prompt"))`.
       - Build `cmd = adapter.build_command(prompt=turn["prompt"], model=session["model"], permission=PermissionLevel(session["permission_level"]), system_prompt=sys_prompt, provider_conversation_id=session.get("provider_session_id"), is_first_turn=is_first_turn)`.
       - Open turn files under `self.config.turns_dir`: `<turn_id>.jsonl`, `<turn_id>.raw.jsonl`, `<turn_id>.stderr.log`.
       - Spawn process with `asyncio.create_subprocess_exec(*cmd, cwd=session["working_dir"], stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)`.
       - Store `self._active_procs[session_id] = proc`.
       - Read `proc.stdout` line by line and `proc.stderr` concurrently:
         - For each stdout line: write raw line to `<turn_id>.raw.jsonl`, call `events = adapter.parse_stream_line(line_str, state)`.
         - If `state.provider_conversation_id` is newly set and `session.get("provider_session_id") is None`: update `session["provider_session_id"] = state.provider_conversation_id`, call `updated_sess = self.db.update_session_provider_id(session_id, state.provider_conversation_id)`, and publish `session_updated`.
         - Count `TOOL_USE` events (`tool_call_count`) and `TOOL_RESULT` events with `is_error=True` (`failed_tool_count`).
         - For each `NormalizedEvent`: append `event.model_dump_json() + "\n"` to `<turn_id>.jsonl` and publish `turn_event` (`{"session_id": session_id, "turn_id": turn_id, "pipeline_run_id": turn["pipeline_run_id"], "stage_run_id": turn["stage_run_id"], "event": event.model_dump()}`).
       - Wrap stream consumption + `proc.wait()` in `asyncio.wait_for(..., timeout=effective_timeout)`.
       - Handle `asyncio.TimeoutError`, `stop_session`, and normal exit exactly per `ARCHITECTURE.md` §7.3 (kill process group via `os.killpg(proc.pid, signal.SIGTERM)`, wait up to 3.0s, then `SIGKILL` if still alive, ignoring `ProcessLookupError`).
       - Determine final `TurnStatus` and `SessionStatus`:
         - If `turn_id in self._stopped_turns`: `TurnStatus.STOPPED`, `SessionStatus.STOPPED`.
         - Else if timed out: `TurnStatus.TIMEOUT`, `SessionStatus.ERROR`, `error_message = f"Turn timed out after {effective_timeout}s"`.
         - Else if `proc.returncode == 0 and state.final_status == "SUCCESS"`: `TurnStatus.COMPLETED`, `SessionStatus.IDLE`.
         - Else: `TurnStatus.ERROR`, `SessionStatus.ERROR`, construct `error_message` from `state.error_message` + last 15 non-empty lines of `<turn_id>.stderr.log`.
       - Compute `duration_ms`, update turn via `self.db.update_turn(...)`, update session status via `self.db.update_session(...)`, and publish `turn_completed` and `session_updated`.
       - Clean up `self._active_procs.pop(session_id, None)` in `finally:`.
     - `stop_session(session_id: str) -> bool`:
       - If `session_id` not in `self._active_procs`, return `False`.
       - Mark active turn ID in `self._stopped_turns`, send `os.killpg(proc.pid, signal.SIGTERM)`, wait up to 3.0s for `proc.wait()`, send `SIGKILL` if needed, await the turn task to finish updating DB, and return `True`.
     - `read_turn_events(turn_id: str) -> List[NormalizedEvent]`:
       - Read `<turns_dir>/<turn_id>.jsonl` if it exists and parse each line into `NormalizedEvent`.
  2. Implement `backend/pipeline_engine.py` (`PipelineEngine`, `ReviewVerdict`, `parse_reviewer_verdict`, `validate_pipeline_stages`):
     - Implement `ReviewVerdict` and `parse_reviewer_verdict(reply_text: str) -> ReviewVerdict` using the exact regexes and fallback extraction rules in `ARCHITECTURE.md` §8.2.
     - Implement `validate_pipeline_stages(db: Database, stages: List[Dict[str, Any]]) -> None` per Clarification `C5`.
     - Implement ` build_handoff_prompt(...)` helper producing the exact 4 prompt templates (**Case A**, **Case B**, **Case C**, **Case D**) and optional `\n\n## User Approval Note\n{note}` suffix from `ARCHITECTURE.md` §8.2.
     - Implement `PipelineEngine`:
       - `start_pipeline_run(pipeline_id, task_prompt)`:
         - Validate pipeline exists (`NotFoundError` if not), validate stages (`validate_pipeline_stages`), check no bound session is currently running (`ConflictError` if busy).
         - Create `pipeline_run` in DB with `status="running"`, publish `pipeline_run_updated`, spawn background task `self._run_pipeline_loop(run_id)`, and return `{"run": run, "stage_runs": []}`.
       - `_run_pipeline_loop(run_id)`:
         - Execute stages sequentially (`idx = 0..len(stages)-1`).
         - For each stage execution: create `stage_run` row (`status="running"`), update `pipeline_run.current_stage_order`, publish `pipeline_run_updated` and `pipeline_stage_updated`.
         - Run turn via `turn = await self.session_manager.run_turn_and_wait(session_id, input_prompt, pipeline_run_id=run_id, stage_run_id=stage_run["id"])`.
         - Attach `turn_id` and `output_text = turn["response_text"]` to `stage_run`.
         - If run was stopped (`run_id in self._stopped_runs` or `turn["status"] == "stopped"`): mark `stage_run` and `pipeline_run` as `stopped`, publish updates, and return.
         - If `turn["status"] != "completed"`: mark `stage_run` and `pipeline_run` as `failed` with `turn["error_message"]`, publish updates, and return.
         - If stage session has `role == "reviewer"`:
           - Parse verdict via `parse_reviewer_verdict(turn["response_text"])`.
           - Handle `"MISSING"` -> mark `stage_run` and `pipeline_run` `failed` (`"Reviewer reply did not contain a valid 'VERDICT: APPROVED' or 'VERDICT: CHANGES_REQUESTED' line."`).
           - Handle `"APPROVED"` -> mark `stage_run` `completed` (`verdict="APPROVED"`). If `stage["require_approval"]` and there is a subsequent stage, pause via `await self._wait_for_approval(run_id, stage_run["id"])`. Advance to `idx + 1`.
           - Handle `"CHANGES_REQUESTED"` -> mark `stage_run` `changes_requested` (`verdict="CHANGES_REQUESTED"`, `findings=verdict.findings_text`).
             - If `review_loop_count >= max_review_loops`: mark `pipeline_run` `failed` (`f"Maximum review loops ({max_review_loops}) reached; Reviewer still requested changes."`) and return.
             - Increment `review_loop_count += 1` in DB and publish `pipeline_run_updated`.
             - If `stage["require_approval"]`: pause via `await self._wait_for_approval(run_id, stage_run["id"])`.
             - Find nearest preceding stage `imp_idx < idx` with session `role == "implementer"`.
             - Run Implementer rework `stage_run` (`iteration = review_loop_count`, Case C prompt). If Implementer stage fails/stops, handle accordingly.
             - If Implementer stage has `require_approval`: pause via `await self._wait_for_approval(...)`.
             - Loop back to run Reviewer re-review `stage_run` (`iteration = review_loop_count`, Case D prompt) and evaluate its new verdict.
         - For non-reviewer stages:
           - Mark `stage_run` `completed`.
           - If `stage["require_approval"]` and `idx < len(stages) - 1`: pause via `await self._wait_for_approval(run_id, stage_run["id"])`.
           - Advance to `idx + 1`.
         - When all stages complete: mark `pipeline_run` `completed` with `finished_at` and `duration_ms`, and publish `pipeline_run_updated`.
       - `approve_pipeline_run(run_id, note)` and `stop_pipeline_run(run_id)`: implement per `ARCHITECTURE.md` §8.2.5 and Clarification `C5`.
  3. **What NOT to do**: Do not modify `db.py`, `models.py`, or provider files.
- **Definition of done**:
  - `SessionManager` executes turns, streams `NormalizedEvent` items to disk and `EventBus`, captures conversation IDs, and terminates process groups on stop/timeout.
  - `PipelineEngine` handles forward stage handoffs, Reviewer `APPROVED` / `CHANGES_REQUESTED` / `MISSING` verdicts, rework loops back to Implementer, loop limits, and approval pauses.

---

### Task 6: Frontend Pipeline Builder, Live Run Timeline & Run History Views (`pipelines.js`, `runs.js`)

- **ID**: `Task 6`
- **Title**: Frontend Pipeline Builder, Live Run Timeline & Run History Views (`pipelines.js`, `runs.js`)
- **Wave**: `3` (depends on Tasks 2 and 4)
- **Complexity**: `medium`
- **Files to create (exclusive ownership)**:
  - `frontend/js/pipelines.js`
  - `frontend/js/runs.js`
- **Files/sections to read first**:
  - `docs/DESIGN.md` §4.4 (View 2: Pipelines & Live Run View — Panel 1 Builder & Launcher, Panel 2 Active Pipeline Run Timeline & Sticky Approval Banner), §4.5 (View 3: Run History Browser), §5 (Interaction Flows)
  - `docs/ARCHITECTURE.md` §10.3 (Pipeline & Run endpoints), §10.4 (SSE events)
  - `docs/PLAN.md` §2 Clarifications `C6`, `C7`, `C8`
- **Precise instructions**:
  1. Implement `frontend/js/pipelines.js`:
     - Export `initPipelinesView`, `renderPipelineBuilder`, and `handlePipelineSseEvent` per Clarification `C7.5`.
     - Render Panel 1 (`#pipeline-builder-panel`):
       - Saved Pipelines selector dropdown + `+ New Pipeline` button + `Delete` button (`DELETE /api/pipelines/{id}`).
       - Pipeline Name input, Description input, and `Max Review Loops` number input (`0..5`, default `2`).
       - Ordered Stages Editor (`#pipeline-stages-list`):
         - Each stage row renders stage `#N` badge, Move Up (`↑`), Move Down (`↓`), Remove (`×`), Stage Name input, Bound Session `<select>` (populated from `store.sessions` showing `{name} [{PROVIDER} · {Role} · {model}]`), dynamic Provider/Role pills, Reviewer verdict hint badge when bound to a `reviewer` session, and `"Pause for my approval before passing on"` checkbox (`require_approval`).
       - `+ Add Stage` button and `"Auto-Fill 4-Stage Pipeline"` button (automatically populates 4 stages: `Architecture Design`, `Implementation Plan`, `Implementation`, `Code Review` bound to existing `designer`, `planner`, `implementer`, `reviewer` sessions if present).
       - `Save Pipeline` button (`POST /api/pipelines` or `PUT /api/pipelines/{id}`).
       - Task Launch Card at bottom: `textarea#pipeline-task-input` and `▶ Run Pipeline` button (saves unsaved changes or uses active pipeline, calls `POST /api/pipelines/{id}/runs`, sets `store.activeRunId`, and focuses Panel 2).
     - Support compact-laptop sub-tab switching (`#pipe-subtab-builder` vs `#pipe-subtab-run`) per `DESIGN.md` §3.
  2. Implement `frontend/js/runs.js`:
     - Export `initRunsView`, `renderActiveRunPanel`, `renderHistoryView`, and `handleRunSseEvent` per Clarification `C7.6`.
     - **Active Pipeline Run View (`renderActiveRunPanel` in `#pipeline-active-run-panel`)**:
       - Run Summary Header: pipeline name, status badge, `Review Loop: X / Y` badge, elapsed timer, collapsible original task prompt, and `■ Stop Pipeline` button (`POST /api/pipeline-runs/{id}/stop`) when status is `running` or `waiting_approval`.
       - Sticky Approval Banner (visible when `run.status === 'waiting_approval'`): amber alert card showing paused stage name, optional approval note input, `✓ Approve & Continue` button (`POST /api/pipeline-runs/{id}/approve`), and `■ Stop Pipeline` button.
       - Live Per-Stage Timeline (`#stage-runs-timeline`):
         - Render each `stage_run` card in chronological order, badging rework iterations (`Rework Loop #1 · Implementer`, `Re-Review #1 · Code Reviewer`).
         - Header shows status dot, stage name, session name, provider/role/model pills, verdict badge (`APPROVED` or `CHANGES_REQUESTED`), and duration.
         - Collapsible tabs inside each stage card:
           - Tab 1 (`Live Output / Reply`): renders live streaming tool cards and assistant markdown (from SSE `turn_event` matching `stage_run_id`) or final `output_text` + highlighted Reviewer `findings` block.
           - Tab 2 (`Handoff Input Prompt`): displays `stage_run.input_prompt` in monospace.
     - **Run History Browser (`renderHistoryView` in `#view-history`)**:
       - Left column (`#history-list-panel`): toggle between `"Pipeline Runs"` (`store.pipelineRuns`) and `"All Session Turns"` (`store.allTurns` fetched via `api.listTurns()`).
       - Right column (`#history-detail-panel`):
         - When a pipeline run is selected, fetch `api.getPipelineRun(runId)` and render the full Per-Stage Timeline (allowing on-demand loading of tool call events via `api.getTurn(stage_run.turn_id)`).
         - When a session turn is selected, fetch `api.getTurn(turnId)` and render its prompt, thinking blocks, tool call cards, assistant response, and an `"Open Session Chat →"` button that calls `switchView("sessions", { sessionId: turn.session_id })`.
  3. **What NOT to do**: Do not edit `index.html`, `styles.css`, `app.js`, or `sessions.js`.
- **Definition of done**:
  - `pipelines.js` and `runs.js` export all functions in Clarification `C7.5` and `C7.6`.
  - Users can build/edit/save pipelines, launch runs, watch live per-stage streaming output and rework loops, approve paused stages with a note, stop runs, and browse historical pipeline runs and turns.

---

### Task 7: FastAPI Application, REST + SSE Endpoints & Project Documentation (`backend/main.py`, `README.md`)

- **ID**: `Task 7`
- **Title**: FastAPI Application, REST + SSE Endpoints & Project Documentation (`backend/main.py`, `README.md`)
- **Wave**: `4` (depends on Tasks 1, 2, 3, 4, 5, 6)
- **Complexity**: `medium`
- **Files to create (exclusive ownership)**:
  - `backend/main.py`
  - `README.md`
- **Files/sections to read first**:
  - `docs/ARCHITECTURE.md` §9 (Startup Crash Recovery), §10.1–10.4 (All HTTP API & SSE specifications), §11 (Security Architecture), §12 (Setup & Run Commands)
  - `docs/PLAN.md` §2 Clarifications `C1`–`C6`
- **Precise instructions**:
  1. Implement `backend/main.py`:
     - Provide `create_app(config: Optional[AppConfig] = None) -> FastAPI` factory AND a module-level `app = create_app()` instance so `uvicorn backend.main:app` works out of the box while tests can also instantiate or use `TestClient(app)` with environment variables.
     - In the FastAPI `lifespan` context manager:
       - Load config via `load_config()` (if not passed into `create_app`), initialize `db = Database(config.db_path)`, call `db.recover_orphaned_states()`, initialize `bus = EventBus()`, `session_manager = SessionManager(config, db, bus)`, and `pipeline_engine = PipelineEngine(db, session_manager, bus)`. Attach them to `app.state`.
       - On shutdown: stop any active sessions cleanly.
     - Register exception handlers for `NotFoundError` (`404`), `ConflictError` (`409`), `ValidationError` (`400`), and `ValueError` (`400`), returning `JSONResponse(status_code=..., content={"detail": str(exc)})`.
     - Implement all REST endpoints from `ARCHITECTURE.md` §10 and Clarification `C6`:
       - `GET /api/meta` -> returns `default_working_dir` (`os.getcwd()`), `providers` (from `await list_providers_metadata(config)`), `roles` (from `ROLE_DEFINITIONS`), and `permissions` (the 3 permission dicts from `ARCHITECTURE.md` §10.1).
       - `GET /api/sessions` -> `{"sessions": db.list_sessions()}`
       - `POST /api/sessions` (`status_code=201`) -> validates `working_dir` via `validate_working_dir(req.working_dir or os.getcwd())`, defaults `permission_level` to `ROLE_DEFINITIONS[req.role].default_permission` if omitted, creates session in DB, publishes `session_updated`, returns session dict.
       - `GET /api/sessions/{session_id}` -> returns `{"session": session, "turns": db.list_turns_for_session(session_id)}` (or `404`).
       - `PATCH /api/sessions/{session_id}` -> raises `404` if not found, `409` if `session_manager.is_session_running(session_id)`, updates fields in DB, publishes `session_updated`, returns updated session dict.
       - `DELETE /api/sessions/{session_id}` -> raises `404` if not found, `409` if `db.is_session_bound_to_pipeline(session_id)`, calls `await session_manager.stop_session(session_id)` if running, deletes session, returns `{"deleted": True, "session_id": session_id}`.
       - `POST /api/sessions/{session_id}/turns` (`status_code=202`) -> calls `turn_id = await session_manager.start_turn(session_id, req.prompt, timeout_sec=req.timeout_sec)`, fetches `turn = db.get_turn(turn_id)`, returns `{"turn_id": turn_id, "session_id": session_id, "turn_number": turn["turn_number"], "status": turn["status"]}`.
       - `POST /api/sessions/{session_id}/stop` -> raises `404` if session not found; calls `stopped = await session_manager.stop_session(session_id)`, returns `{"stopped": stopped, "session_id": session_id}`.
       - `GET /api/turns` -> `{"turns": db.list_all_turns()}` (Clarification `C6`).
       - `GET /api/turns/{turn_id}` -> raises `404` if not found; returns `{"turn": turn, "events": [e.model_dump() for e in session_manager.read_turn_events(turn_id)]}`.
       - `GET /api/pipelines` -> `{"pipelines": db.list_pipelines()}`
       - `POST /api/pipelines` (`status_code=201`) -> calls `validate_pipeline_stages(db, stages_dicts)`, creates pipeline in DB, returns pipeline dict.
       - `PUT /api/pipelines/{pipeline_id}` -> raises `404` if not found, calls `validate_pipeline_stages(db, stages_dicts)`, updates pipeline in DB, returns pipeline dict.
       - `DELETE /api/pipelines/{pipeline_id}` -> raises `404` if not found, `409` if `db.has_active_pipeline_runs(pipeline_id)`, deletes pipeline, returns `{"deleted": True, "pipeline_id": pipeline_id}`.
       - `POST /api/pipelines/{pipeline_id}/runs` (`status_code=202`) -> calls `await pipeline_engine.start_pipeline_run(pipeline_id, req.task_prompt)`.
       - `GET /api/pipeline-runs` -> `{"runs": db.list_pipeline_runs()}`
       - `GET /api/pipeline-runs/{run_id}` -> raises `404` if not found; returns `{"run": run, "stage_runs": db.list_stage_runs(run_id)}`.
       - `POST /api/pipeline-runs/{run_id}/approve` -> calls `await pipeline_engine.approve_pipeline_run(run_id, req.note)`.
       - `POST /api/pipeline-runs/{run_id}/stop` -> calls `await pipeline_engine.stop_pipeline_run(run_id)`.
       - `GET /api/events` -> returns `StreamingResponse` with `media_type="text/event-stream"` and headers `{"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}`. Formats each item from `bus.subscribe()` as `f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"`.
     - Mount `frontend/` static files at `/` (`app.mount("/", StaticFiles(directory=..., html=True), name="frontend")`) AFTER all `/api/*` routes so `GET /` serves `frontend/index.html` and `/js/*`, `/styles.css` resolve cleanly.
  2. Create `README.md`:
     - Concise developer guide covering: Overview, Prerequisites (`python3` 3.9+, `claude` and `agy` CLIs installed and logged in), Quickstart (`venv` setup, `pip install -r requirements.txt`, `uvicorn` launch command on `http://127.0.0.1:8765`), Environment Variables (`AGENT_RELAY_HOME`, `AGENT_RELAY_PORT`, `CLAUDE_BIN`, `AGY_BIN`, `AGENT_RELAY_TURN_TIMEOUT`), Roles & Permission Mapping summary, Pipeline & Review Loop overview, and Running Tests (`.venv/bin/pytest -v`).
  3. **What NOT to do**: Do not modify any other backend or frontend files.
- **Definition of done**:
  - `GET /api/meta` returns `200 OK` with `providers`, `roles`, `permissions`, and `default_working_dir`.
  - `GET /` serves `frontend/index.html` (`200 OK`).
  - All REST endpoints in `ARCHITECTURE.md` §10 respond with the exact documented status codes and JSON schemas.

---

### Task 8: Fake CLI Executables & Comprehensive Pytest Suite (`tests/*`)

- **ID**: `Task 8`
- **Title**: Fake CLI Executables & Comprehensive Pytest Suite (`tests/*`)
- **Wave**: `4` (depends on Tasks 1, 3, 5, 7)
- **Complexity**: `high`
- **Files to create (exclusive ownership)**:
  - `tests/__init__.py`
  - `tests/conftest.py`
  - `tests/fixtures/fake_claude.py`
  - `tests/fixtures/fake_agy.py`
  - `tests/test_providers.py`
  - `tests/test_sessions.py`
  - `tests/test_pipelines.py`
  - `tests/test_api.py`
- **Files/sections to read first**:
  - `docs/ARCHITECTURE.md` §6 (`ProviderAdapter`), §7 (`SessionManager`), §8 (`PipelineEngine`), §10 (HTTP API & SSE), §13 (Testing Strategy & Deterministic Prompt Triggers)
  - `docs/PLAN.md` §2 Clarifications `C1`–`C6`
- **Precise instructions**:
  1. Create `tests/__init__.py`.
  2. Create `tests/fixtures/fake_claude.py` and `tests/fixtures/fake_agy.py` and mark both executable (`chmod 0o755`):
     - Start with `#!/usr/bin/env python3` and `from __future__ import annotations`.
     - Implement all deterministic prompt triggers specified in `ARCHITECTURE.md` §13.1:
       - Normal prompt: emits init/conversation ID, thinking delta, tool use + tool result, text deltas echoing received prompt/flags summary, and final `SUCCESS` result.
       - `--resume <id>` (in `fake_claude.py`) and `--conversation <id>` (in `fake_agy.py`): reuses that conversation ID in output events and detects that this is a resumed turn (`turn > 1`).
       - `[CHANGES_REQUESTED]`: on the first turn (no `--resume` / `--conversation` or when prompt does not contain `# Re-Review Request`), ends response with `VERDICT: CHANGES_REQUESTED\nFINDINGS:\n- [foo.py:10] Fix bug in foo.py`. On subsequent re-review turns (when prompt contains `# Re-Review Request`), ends response with `VERDICT: APPROVED`.
       - `[ALWAYS_REJECT]`: always ends response with `VERDICT: CHANGES_REQUESTED\nFINDINGS:\n- Still broken`.
       - `[MISSING_VERDICT]`: replies with `"Looks okay to me"` without any `VERDICT:` line.
       - `[FAIL]`: writes `"Fatal CLI error occurred\n"` to `sys.stderr`, emits an error result JSON (or exits immediately), and exits with code `1`.
       - `[SLOW]`: flushes an init event and sleeps `10` seconds (handling `SIGTERM` cleanly or allowing `SessionManager` to terminate it).
     - In `fake_agy.py`: when invoked with `sys.argv[1:] == ["models"]`, print the 3 model IDs (`gemini-4-argon-high`, `gemini-3.8-flash-medium`, `claude-sonnet-5-5-medium`) and exit `0`.
  3. Create `tests/conftest.py`:
     - Ensure `fake_claude.py` and `fake_agy.py` have `0o755` permissions on disk (`os.chmod`).
     - Provide fixtures that set `AGENT_RELAY_HOME` to a fresh `tmp_path / "home"`, `CLAUDE_BIN` to `str(Path(__file__).parent / "fixtures" / "fake_claude.py")`, and `AGY_BIN` to `str(Path(__file__).parent / "fixtures" / "fake_agy.py")`.
     - Provide fixtures for `app_config`, `db`, `bus`, `session_manager`, `pipeline_engine`, and a FastAPI `TestClient(create_app(app_config))`.
  4. Create `tests/test_providers.py`:
     - Test `ClaudeAdapter.build_command` and `AntigravityAdapter.build_command` for Turn 1 and Turn 2+ across `READONLY`, `EDIT`, and `FULL` permissions, verifying exact CLI flags and system prompt injection.
     - Test `ClaudeAdapter.parse_stream_line` and `AntigravityAdapter.parse_stream_line` against canned NDJSON lines (init, thinking delta, text delta, tool use, tool result, denied actions, error result, malformed non-JSON line).
     - Test `list_models()` for both adapters using `fake_agy.py`.
  5. Create `tests/test_sessions.py`:
     - Test `validate_working_dir` rejection of `/` and `/System` and acceptance of valid workspace directories.
     - Test multi-turn execution via `SessionManager`: Turn 1 captures `provider_session_id` (`fake-claude-uuid-1` / `fake-agy-conv-1`), persists `.jsonl` and `.raw.jsonl` files, and Turn 2 passes `--resume` / `--conversation`.
     - Test concurrent turn prevention (`ConflictError` when starting a second turn while `[SLOW]` turn is running).
     - Test `stop_session` on a `[SLOW]` turn transitions turn and session to `stopped`.
     - Test turn timeout (`timeout_sec=1` on a `[SLOW]` turn) transitions turn to `timeout` and session to `error`.
     - Test `[FAIL]` turn captures stderr tail into `turn.error_message` and sets status to `error`.
  6. Create `tests/test_pipelines.py`:
     - Test `parse_reviewer_verdict` for `APPROVED`, `CHANGES_REQUESTED` with `FINDINGS:`, `CHANGES_REQUESTED` without `FINDINGS:` header, markdown-formatted `**VERDICT: APPROVED**`, multiple verdicts (takes last), and `MISSING`.
     - Test `validate_pipeline_stages` rejects a Reviewer stage without a preceding Implementer stage.
     - Test full 4-stage pipeline happy path (`Designer -> Planner -> Implementer -> Reviewer` ending in `COMPLETED`).
     - Test rework loop (`[CHANGES_REQUESTED]` triggers Case C rework on Implementer, then Case D re-review on Reviewer, which returns `APPROVED` -> pipeline `COMPLETED` with `review_loop_count == 1` and 6 total `stage_runs`).
     - Test loop limit exhaustion (`[ALWAYS_REJECT]` with `max_review_loops=2` fails after 2 rework loops with `PipelineRunStatus.FAILED`).
     - Test missing verdict (`[MISSING_VERDICT]` fails pipeline run cleanly).
     - Test `require_approval=True` pauses pipeline in `waiting_approval`, `approve_pipeline_run(run_id, note="Looks good")` injects `## User Approval Note` into the next stage's `input_prompt` and completes the run.
     - Test `stop_pipeline_run` while a `[SLOW]` stage is running stops both the stage turn and the pipeline run.
  7. Create `tests/test_api.py`:
     - Smoke and integration tests using `TestClient` covering:
       - `GET /` (serves HTML shell)
       - `GET /api/meta`
       - `POST /api/sessions`, `GET /api/sessions`, `GET /api/sessions/{id}`, `PATCH /api/sessions/{id}`, `DELETE /api/sessions/{id}` (including `409` when bound to a pipeline)
       - `POST /api/sessions/{id}/turns`, `GET /api/turns`, `GET /api/turns/{turn_id}`, `POST /api/sessions/{id}/stop`
       - `POST /api/pipelines` (including `400` validation when Reviewer has no prior Implementer), `GET /api/pipelines`, `PUT /api/pipelines/{id}`, `DELETE /api/pipelines/{id}`
       - `POST /api/pipelines/{id}/runs`, `GET /api/pipeline-runs`, `GET /api/pipeline-runs/{run_id}`, `POST /api/pipeline-runs/{run_id}/approve`, `POST /api/pipeline-runs/{run_id}/stop`
  8. **What NOT to do**: Never invoke real `claude` or `agy` binaries in any test. Do not edit application code outside `tests/`.
- **Definition of done**:
  - `.venv/bin/pytest -v` passes 100% of tests in `tests/test_providers.py`, `tests/test_sessions.py`, `tests/test_pipelines.py`, and `tests/test_api.py` in under 15 seconds with zero network or real model calls.

---

## 4. Integration Checklist

Once all 4 waves (Tasks 1–8) are complete, the reviewer / lead engineer can verify the full system with the steps below.

### 4.1 Environment Setup & Automated Test Verification
```bash
# 1. Create virtual environment using macOS Python 3.9
python3 -m venv .venv

# 2. Install pinned dependencies
.venv/bin/pip install -r requirements.txt

# 3. Ensure fake CLI test fixtures are executable
chmod +x tests/fixtures/fake_claude.py tests/fixtures/fake_agy.py

# 4. Run the full offline test suite (must pass 100% with zero real model calls)
.venv/bin/pytest -v
```

### 4.2 Starting the Local Server
```bash
# Start Agent Relay on http://127.0.0.1:8765
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```
Verify metadata endpoint in another terminal:
```bash
curl -s http://127.0.0.1:8765/api/meta | python3 -m json.tool | head -n 30
```

### 4.3 Manual End-to-End Scenario with Real CLIs (`claude` & `agy`)
1. Open `http://127.0.0.1:8765` in a browser (verify the top bar shows `● SSE Connected`).
2. **Create 4 Role Sessions** (pointing `Working Directory` to a scratch test directory, e.g. `/tmp/relay-demo`):
   - **Designer**: Provider `Antigravity (AGY)`, Role `Designer` (`Read-Only`).
   - **Planner**: Provider `Claude Code`, Model `haiku` (or `sonnet`), Role `Planner` (`Read-Only`).
   - **Implementer**: Provider `Antigravity (AGY)` (or `Claude Code`), Role `Implementer` (`Edit Files`).
   - **Code Reviewer**: Provider `Claude Code`, Model `haiku` (or `sonnet`), Role `Code Reviewer` (`Read-Only`).
3. **Verify Multi-Turn Session Chat**:
   - Select the **Planner** session, send `"In one sentence, what is your role?"`, watch the live SSE streaming response and verify the header displays `conv: <session-id>` once Turn 1 completes.
   - Send a follow-up turn `"Repeat the exact word 'ACKNOWLEDGED' if you remember my previous question."` to confirm `--resume` / `--conversation` context continuity.
4. **Verify 4-Stage Pipeline & Review Loop**:
   - Switch to the **Pipelines** tab, click **Auto-Fill 4-Stage Pipeline**, check `"Pause for my approval before passing on"` on Stage 2 (`Plan`), set `Max Review Loops` to `2`, and click **Save Pipeline**.
   - Enter the trivial task: `"Create a file hello.py with a function greet(name) that returns 'Hello, <name>!'."` and click **▶ Run Pipeline**.
   - Watch Stage 1 (`Designer`) and Stage 2 (`Planner`) stream live tool calls and text in the Active Pipeline Run timeline.
   - When Stage 2 pauses in `WAITING FOR APPROVAL`, enter an optional approval note (`"Keep hello.py under 10 lines."`) and click **✓ Approve & Continue**.
   - Watch Stage 3 (`Implementer`) write `hello.py` and Stage 4 (`Code Reviewer`) inspect the workspace and emit `VERDICT: APPROVED` (or trigger a rework loop if changes are requested), finishing with pipeline status `COMPLETED`.
5. **Verify Persistence Across Restart**:
   - Stop `uvicorn` (`Ctrl+C`) and restart it, refresh `http://127.0.0.1:8765`, open the **Run History** tab, and confirm the completed pipeline run and all individual turns (including tool calls and handoff prompts) load from `~/.agent-relay/`.

---

## 5. Task Summary Table

| ID | Wave | Complexity | Exclusive Files Owned |
| :--- | :---: | :---: | :--- |
| **Task 1** | 1 | Medium | `requirements.txt`, `backend/__init__.py`, `backend/config.py`, `backend/models.py`, `backend/roles.py`, `backend/pubsub.py`, `backend/db.py` |
| **Task 2** | 1 | Medium | `frontend/index.html`, `frontend/styles.css`, `frontend/js/api.js`, `frontend/js/components.js` |
| **Task 3** | 2 | High | `backend/providers/__init__.py`, `backend/providers/base.py`, `backend/providers/claude.py`, `backend/providers/antigravity.py` |
| **Task 4** | 2 | High | `frontend/js/app.js`, `frontend/js/sessions.js` |
| **Task 5** | 3 | High | `backend/session_manager.py`, `backend/pipeline_engine.py` |
| **Task 6** | 3 | Medium | `frontend/js/pipelines.js`, `frontend/js/runs.js` |
| **Task 7** | 4 | Medium | `backend/main.py`, `README.md` |
| **Task 8** | 4 | High | `tests/__init__.py`, `tests/conftest.py`, `tests/fixtures/fake_claude.py`, `tests/fixtures/fake_agy.py`, `tests/test_providers.py`, `tests/test_sessions.py`, `tests/test_pipelines.py`, `tests/test_api.py` |
