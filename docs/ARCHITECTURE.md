# Agent Relay — Architecture Specification

## 1. Overview

**Agent Relay** is a local, single-user macOS web application for managing multi-turn AI coding sessions across **Claude Code (`claude`)** and **Antigravity (`agy`)** CLIs, assigning specialized engineering roles with enforced tool permissions, and orchestrating multi-stage pipelines with automated code-review rework loops.

All model interactions occur through locally installed, already-authenticated CLI binaries executed as non-interactive streaming subprocesses (`stream-json`). No API keys or external cloud relays are used.

---

## 2. Tech Stack & Justifications

| Layer | Choice | Version | One-Line Justification |
| :--- | :--- | :--- | :--- |
| **Language / Runtime** | Python (macOS system) | `3.9.6` | Pre-installed on macOS; zero runtime installer needed (uses `typing.Optional`, `List`, `Dict`, `Union` — no `3.10+` syntax). |
| **Web Framework** | FastAPI + Pydantic | `0.115.6` / `2.10.3` | Native `asyncio` request handling, clean REST routing, automatic request/response validation, and built-in `StreamingResponse` for SSE. |
| **ASGI Server** | Uvicorn | `0.32.1` | Lightweight async HTTP/1.1 server capable of holding long-lived SSE connections alongside concurrent subprocess tasks on `127.0.0.1:8765`. |
| **Concurrency** | Python `asyncio` (stdlib) | `3.9` | Multiplexes non-blocking stdout/stderr line readers across multiple CLI subprocesses and SSE subscribers in a single event loop without thread locks. |
| **Database** | SQLite 3 (`sqlite3` stdlib) + JSONL files | `3.9` | Zero-dependency ACID storage for relational state (`state.db`) paired with append-only `.jsonl` files for high-frequency turn stream events. |
| **Frontend** | Vanilla HTML5, CSS3, ES2020 Modules | N/A | Runs directly in the browser with no Node.js, bundler, or external CDN dependencies. |
| **Testing** | `pytest` + `httpx` | `8.3.4` / `0.28.1` | Fast, deterministic unit and API integration testing against fake CLI scripts without touching real models. |

### `requirements.txt`

```text
fastapi==0.115.6
uvicorn==0.32.1
pydantic==2.10.3
pytest==8.3.4
httpx==0.28.1
```

---

## 3. Repository Directory & File Layout

Implementers must create the exact file structure below:

```text
agent-relay/
├── requirements.txt
├── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DESIGN.md
│   └── reference/
│       ├── agy-cli-help.txt
│       ├── claude-cli-help.txt
│       └── gem-script.sh
├── backend/
│   ├── __init__.py
│   ├── main.py                # FastAPI app factory, lifespan startup/shutdown, static file mounts, HTTP routes
│   ├── config.py              # Settings, port validation, data directory paths, CLI binary discovery
│   ├── models.py              # Enums, internal NormalizedEvent dataclass, Pydantic API request/response schemas
│   ├── db.py                  # SQLite connection helper, schema DDL, and synchronous CRUD repository
│   ├── roles.py               # Built-in Role definitions, default permission levels, and system prompts
│   ├── pubsub.py              # In-memory asyncio pub/sub EventBus for Server-Sent Events (SSE)
│   ├── session_manager.py     # Session turn execution, subprocess tracking, stream persistence, stop/kill
│   ├── pipeline_engine.py     # Pipeline state machine, handoff prompt builder, verdict parser, review loop logic
│   └── providers/
│       ├── __init__.py        # Provider registry (`get_adapter`, `list_providers_metadata`)
│       ├── base.py            # Abstract `ProviderAdapter` interface
│       ├── claude.py          # Claude Code CLI (`claude`) adapter implementation
│       └── antigravity.py     # Antigravity CLI (`agy`) adapter implementation
├── frontend/
│   ├── index.html             # Single-page application shell & modal templates
│   ├── styles.css             # CSS variables (light/dark), layout grid, component styles, responsive breakpoints
│   └── js/
│       ├── app.js             # Entry module, view router, global state store, SSE connection manager
│       ├── api.js             # Typed fetch wrapper for all `/api/*` endpoints
│       ├── sessions.js        # Sidebar session list, New Session modal, Session Chat & live stream view
│       ├── pipelines.js       # Pipeline Builder view, stage list editor, pipeline launcher
│       ├── runs.js            # Active Pipeline Run timeline view, approval banner, Run History browser
│       └── components.js      # Reusable DOM builders (badges, tool call cards, markdown/code formatter, toasts)
└── tests/
    ├── __init__.py
    ├── conftest.py            # Pytest fixtures: temporary `AGENT_RELAY_HOME`, fake CLI path injection, TestClient
    ├── fixtures/
    │   ├── fake_claude.py     # Executable Python 3.9 script simulating `claude -p --output-format stream-json`
    │   └── fake_agy.py        # Executable Python 3.9 script simulating `agy -p --output-format stream-json`
    ├── test_providers.py      # Unit tests for CLI flag construction, permission mapping, stream-json normalization
    ├── test_sessions.py       # Unit tests for SessionManager turns, resume IDs, timeouts, SIGTERM/SIGKILL stop
    ├── test_pipelines.py      # Unit tests for PipelineEngine handoffs, verdict parsing, review loops, approvals
    └── test_api.py            # End-to-end REST + SSE integration tests
```

---

## 4. Data Directory Layout

By default, all persistent data is stored under `~/.agent-relay/` (overridable via the `AGENT_RELAY_HOME` environment variable):

```text
~/.agent-relay/
├── state.db                   # SQLite 3 database (WAL mode enabled)
└── turns/
    ├── <turn_id>.jsonl        # Normalized stream events (1 JSON object per line)
    ├── <turn_id>.raw.jsonl    # Raw stdout lines emitted by the provider CLI
    └── <turn_id>.stderr.log   # Raw stderr output from the provider CLI
```

---

## 5. Domain Model & Module Interfaces

All Python files MUST begin with `from __future__ import annotations` or use `typing.Optional`, `typing.List`, `typing.Dict`, `typing.Any`, `typing.Tuple`, `typing.AsyncIterator` for strict Python 3.9.6 compatibility. Never use `X | Y` union syntax at runtime (e.g., in Pydantic models or `isinstance` checks) and never use `match` / `case`.

### 5.1 `backend/config.py`

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

FORBIDDEN_PORTS: Set[int] = {5000, 7000, 5090, 5186}
DEFAULT_PORT: int = 8765
DEFAULT_HOST: str = "127.0.0.1"

@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    data_dir: Path
    db_path: Path
    turns_dir: Path
    claude_bin: str
    agy_bin: str
    default_turn_timeout_sec: int = 600

def load_config() -> AppConfig:
    """
    Reads environment variables:
      - AGENT_RELAY_PORT (default 8765; raises ValueError if in FORBIDDEN_PORTS)
      - AGENT_RELAY_HOME (default ~/.agent-relay)
      - CLAUDE_BIN (default: ~/.local/bin/claude or shutil.which('claude') or 'claude')
      - AGY_BIN (default: ~/.local/bin/agy or shutil.which('agy') or 'agy')
      - AGENT_RELAY_TURN_TIMEOUT (default 600)
    Creates data_dir and turns_dir with mode 0o700 if they do not exist.
    """

def validate_working_dir(path_str: str) -> str:
    """
    Expands ~ and resolves symlinks via os.path.realpath.
    Raises ValueError if:
      - Path does not exist or is not a directory.
      - Path is "/" or starts with a system prefix:
        ("/System", "/etc", "/private/etc", "/var", "/private/var", "/usr", "/bin", "/sbin", "/dev", "/Library")
    Returns the canonical absolute path string.
    """
```

### 5.2 `backend/models.py`

```python
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

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
```

### 5.3 `backend/roles.py`

Built-in roles, their default `PermissionLevel`, and the exact system prompt prepended on a session's first turn:

| `RoleId` | Label | Default Permission | System Prompt Purpose |
| :--- | :--- | :--- | :--- |
| `designer` | Designer | `readonly` | Inspects codebase/requirements and produces concrete UI/UX and architectural specifications without modifying files. |
| `planner` | Planner | `readonly` | Analyzes architecture/design and produces an ordered, file-by-file implementation plan with verification steps without modifying files. |
| `implementer` | Implementer | `edit` | Executes the task or plan by writing/editing code and running tests; ends with a concise summary of modified files. |
| `reviewer` | Code Reviewer | `readonly` | Audits the implementation (`git diff`, file inspection) against the task/plan without modifying files, and MUST conclude with a strict machine-readable verdict block. |
| `custom` | Custom | `readonly` | Uses the user-supplied `custom_prompt` string verbatim. |

#### Exact Role System Prompts

```python
from dataclasses import dataclass
from typing import Dict, List, Optional
from backend.models import PermissionLevel, RoleId

@dataclass(frozen=True)
class RoleDefinition:
    id: RoleId
    name: str
    description: str
    default_permission: PermissionLevel
    system_prompt: str

ROLE_DEFINITIONS: Dict[RoleId, RoleDefinition] = {
    RoleId.DESIGNER: RoleDefinition(
        id=RoleId.DESIGNER,
        name="Designer",
        description="Produces system architecture and UI/UX design specifications. Read-only by default.",
        default_permission=PermissionLevel.READONLY,
        system_prompt=(
            "You are the Software Architect & Product Designer in a multi-agent engineering pipeline.\n"
            "Your job is to inspect the repository and requirements, then output a clear, concrete design "
            "specification covering data models, component structure, user flows, edge cases, and visual/API contracts.\n"
            "Do NOT modify or create project code files. Output your complete design directly in your response so "
            "the next stage (Planner) can act on it."
        ),
    ),
    RoleId.PLANNER: RoleDefinition(
        id=RoleId.PLANNER,
        name="Planner",
        description="Breaks tasks and designs into step-by-step file-level implementation plans. Read-only by default.",
        default_permission=PermissionLevel.READONLY,
        system_prompt=(
            "You are the Lead Engineering Planner in a multi-agent engineering pipeline.\n"
            "Examine the workspace and any prior design output, then produce a concrete, ordered implementation plan.\n"
            "Specify exact file paths to create or modify, function/class signatures, edge cases to handle, "
            "and verification criteria. Do NOT edit files yourself. Output the complete actionable plan in your reply."
        ),
    ),
    RoleId.IMPLEMENTER: RoleDefinition(
        id=RoleId.IMPLEMENTER,
        name="Implementer",
        description="Writes and edits code in the working directory according to the plan or review feedback.",
        default_permission=PermissionLevel.EDIT,
        system_prompt=(
            "You are the Implementer in a multi-agent engineering pipeline.\n"
            "Carry out the requested implementation or address all code review findings directly in the workspace.\n"
            "Follow existing codebase conventions, do not touch unrelated files, and never run git commit/push/reset.\n"
            "End your response with a concise report listing:\n"
            "1. Every file created or modified and a one-line summary of the change.\n"
            "2. How you verified the changes or any constraints encountered."
        ),
    ),
    RoleId.REVIEWER: RoleDefinition(
        id=RoleId.REVIEWER,
        name="Code Reviewer",
        description="Reviews workspace changes against requirements and emits a machine-readable verdict.",
        default_permission=PermissionLevel.READONLY,
        system_prompt=(
            "You are the Code Reviewer in a multi-agent engineering pipeline.\n"
            "Inspect the actual files and diffs in the working directory against the original task and plan.\n"
            "Do NOT modify any files. Check for correctness, regressions, edge cases, security issues, and adherence to spec.\n\n"
            "CRITICAL REQUIREMENT — MACHINE-READABLE VERDICT:\n"
            "You MUST end your response with a verdict block in the exact format below (at the very end of your reply):\n\n"
            "If the implementation is complete and correct:\n"
            "VERDICT: APPROVED\n\n"
            "If changes or fixes are required:\n"
            "VERDICT: CHANGES_REQUESTED\n"
            "FINDINGS:\n"
            "- [file/path:line] Concrete description of issue 1 and how to fix it.\n"
            "- [file/path:line] Concrete description of issue 2 and how to fix it."
        ),
    ),
    RoleId.CUSTOM: RoleDefinition(
        id=RoleId.CUSTOM,
        name="Custom",
        description="Custom role with user-defined system instructions.",
        default_permission=PermissionLevel.READONLY,
        system_prompt="",
    ),
}

def resolve_system_prompt(role: RoleId, custom_prompt: Optional[str] = None) -> str:
    """
    For RoleId.CUSTOM, returns (custom_prompt or "").strip().
    For built-in roles, returns ROLE_DEFINITIONS[role].system_prompt, appending
    custom_prompt if non-empty.
    """
```

---

## 6. Provider Adapter Architecture

All provider-specific logic sits behind `ProviderAdapter` in `backend/providers/base.py`. Adding a third provider in the future requires only implementing a new subclass of `ProviderAdapter` and registering it in `backend/providers/__init__.py`.

### 6.1 Abstract Interface (`backend/providers/base.py`)

```python
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from backend.models import NormalizedEvent, PermissionLevel, ProviderId

class StreamParseState:
    """Mutable per-turn parser state held while reading a CLI stdout stream."""
    def __init__(self) -> None:
        self.seq: int = 0
        self.provider_conversation_id: Optional[str] = None
        self.accumulated_text: List[str] = []
        self.saw_text_delta: bool = False
        self.seen_tool_steps: Dict[str, str] = {}  # step_key -> last state
        self.final_status: Optional[str] = None    # "SUCCESS" | "ERROR"
        self.final_response: Optional[str] = None
        self.error_message: Optional[str] = None
        self.denied_actions: List[str] = []

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

class ProviderAdapter(ABC):
    provider_id: ProviderId
    display_name: str

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model: str,
        permission: PermissionLevel,
        system_prompt: Optional[str],
        provider_conversation_id: Optional[str],
        is_first_turn: bool,
    ) -> List[str]:
        """Returns the exact argv list for asyncio.create_subprocess_exec (never shell=True)."""

    @abstractmethod
    def parse_stream_line(
        self,
        line: str,
        state: StreamParseState,
    ) -> List[NormalizedEvent]:
        """
        Parses a single NDJSON line from the CLI stdout and updates `state`.
        Returns zero or more NormalizedEvent objects.
        Malformed non-JSON lines are ignored safely.
        """

    @abstractmethod
    async def list_models(self) -> List[Dict[str, str]]:
        """Returns [{"id": "...", "label": "..."}, ...] available for this provider."""
```

### 6.2 Permission Mapping Table (Role Permission Level -> Exact CLI Flags)

| `PermissionLevel` | UI Label | Claude CLI (`claude`) Flags | Antigravity CLI (`agy`) Flags |
| :--- | :--- | :--- | :--- |
| `readonly` | **Read-Only (Plan)** | `--permission-mode plan --permission-prompts none --disallowedTools Edit,Write,NotebookEdit,Bash` | `--mode plan` |
| `edit` | **Edit Files** | `--permission-mode acceptEdits --permission-prompts none` | `--mode accept-edits` |
| `full` | **Full Access (Edit + Commands)** | `--permission-mode bypassPermissions --dangerously-skip-permissions` | `--mode accept-edits --dangerously-skip-permissions` |

*Notes on Permission Mapping*:
1. By default, **Designer**, **Planner**, **Code Reviewer**, and **Custom** sessions are created with `readonly`.
2. By default, **Implementer** sessions are created with `edit` (and the user can select `full` when creating or editing the session if the Implementer needs to run shell test suites unattended without permission prompts).
3. The user can override the permission level on any session at creation time or between turns via `PATCH /api/sessions/{session_id}`.

---

### 6.3 Claude Code CLI Adapter (`backend/providers/claude.py`)

#### Command Line Construction
- **First Turn (`is_first_turn=True`, `provider_conversation_id=None`)**:
  ```python
  [
      claude_bin,
      "-p",
      "--output-format", "stream-json",
      "--verbose",
      "--include-partial-messages",
      "--model", model,
      *permission_flags,
      # Included if system_prompt is non-empty:
      "--append-system-prompt", system_prompt,
      "--",
      prompt,
  ]
  ```
- **Subsequent Turns (`is_first_turn=False`, `provider_conversation_id="<uuid>"`)**:
  ```python
  [
      claude_bin,
      "-p",
      "--output-format", "stream-json",
      "--verbose",
      "--include-partial-messages",
      "--resume", provider_conversation_id,
      "--model", model,
      *permission_flags,
      "--",
      prompt,
  ]
  ```
  *(Per `claude-cli-help.txt`, `--system-prompt-snapshot` is `on` by default, so `--append-system-prompt` recorded on Turn 1 persists across `--resume` calls.)*

#### Session ID Capture & Stream-JSON Normalization (`claude`)
Each stdout line is parsed as `obj = json.loads(line)`:
1. **Conversation ID capture**: If `obj.get("session_id")` is a non-empty string and `state.provider_conversation_id is None`, set `state.provider_conversation_id = obj["session_id"]`.
2. **`obj.get("type") == "system"` and `obj.get("subtype") == "init"`**:
   - Emit `NormalizedEvent(type=SESSION_INIT, provider_conversation_id=state.provider_conversation_id, raw=obj)`.
3. **`obj.get("type") == "stream_event"`**:
   - Inspect `inner = obj.get("event") or {}`.
   - If `inner.get("type") == "content_block_delta"`:
     - `delta = inner.get("delta") or {}`
     - If `delta.get("type") == "text_delta"` and `delta.get("text")`:
       - Set `state.saw_text_delta = True`, append `delta["text"]` to `state.accumulated_text`, emit `NormalizedEvent(type=TEXT_DELTA, text=delta["text"])`.
     - If `delta.get("type") == "thinking_delta"` and `delta.get("thinking")`:
       - Emit `NormalizedEvent(type=THINKING_DELTA, text=delta["thinking"])`.
4. **`obj.get("type") == "assistant"`**:
   - Iterate `block` in `(obj.get("message") or {}).get("content") or []`:
     - If `block.get("type") == "text"` and `block.get("text")` and not `state.saw_text_delta`:
       - Append `block["text"]` to `state.accumulated_text`, emit `NormalizedEvent(type=TEXT_DELTA, text=block["text"])`.
     - If `block.get("type") == "thinking"` and `block.get("thinking")` and not `state.saw_text_delta`:
       - Emit `NormalizedEvent(type=THINKING_DELTA, text=block["thinking"])`.
     - If `block.get("type") == "tool_use"`:
       - `tool_id = str(block.get("id") or state.next_seq())`
       - If `tool_id` not in `state.seen_tool_steps`:
         - `state.seen_tool_steps[tool_id] = "RUNNING"`
         - Emit `NormalizedEvent(type=TOOL_USE, tool_call_id=tool_id, tool_name=block.get("name"), tool_input=block.get("input") or {})`.
5. **`obj.get("type") == "user"`**:
   - Iterate `block` in `(obj.get("message") or {}).get("content") or []`:
     - If `block.get("type") == "tool_result"`:
       - Extract string content from `block.get("content")` (join text blocks if list).
       - Emit `NormalizedEvent(type=TOOL_RESULT, tool_call_id=str(block.get("tool_use_id") or ""), tool_output=content_str, is_error=bool(block.get("is_error")))`.
6. **`obj.get("type") == "result"`**:
   - `is_err = bool(obj.get("is_error")) or obj.get("subtype") != "success"`
   - `state.final_status = "ERROR" if is_err else "SUCCESS"`
   - `result_text = obj.get("result") or "".join(state.accumulated_text)`
   - `state.final_response = result_text`
   - If `is_err`: `state.error_message = str(obj.get("error") or obj.get("result") or "Claude CLI reported an error")`
   - Emit `NormalizedEvent(type=TURN_RESULT, provider_conversation_id=state.provider_conversation_id, text=result_text, is_error=is_err, raw=obj)`.

#### Model List (`claude`)
Returns static aliases + full IDs:
```python
[
    {"id": "sonnet", "label": "Claude Sonnet (latest alias)"},
    {"id": "opus", "label": "Claude Opus (latest alias)"},
    {"id": "haiku", "label": "Claude Haiku (latest alias)"},
    {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
    {"id": "claude-opus-4-5", "label": "Claude Opus 4.5"},
]
```

---

### 6.4 Antigravity CLI Adapter (`backend/providers/antigravity.py`)

Based directly on the verified behavior in `docs/reference/gem-script.sh` and `docs/reference/agy-cli-help.txt`.

#### System Prompt Handling on Turn 1
`agy` does not have a `--system-prompt` CLI flag (`agy-cli-help.txt`). Following `gem-script.sh` line 142 (`cat "$PREAMBLE_FILE" && printf '\n\n# Task\n\n'; read_prompt`), on `is_first_turn=True`, if `system_prompt` is non-empty, `build_command` constructs the effective prompt as:
```python
effective_prompt = f"{system_prompt.strip()}\n\n# Task\n\n{prompt}"
```
On subsequent turns (`is_first_turn=False`, resuming via `--conversation`), `effective_prompt = prompt`.

#### Command Line Construction
- **First Turn (`is_first_turn=True`, `provider_conversation_id=None`)**:
  ```python
  [
      agy_bin,
      "--output-format", "stream-json",
      *permission_flags,       # e.g. ["--mode", "plan"] or ["--mode", "accept-edits"]
      "--model", model,        # omitted if model is empty
      "-p", effective_prompt,
  ]
  ```
- **Subsequent Turns (`is_first_turn=False`, `provider_conversation_id="<id>"`)**:
  ```python
  [
      agy_bin,
      "--output-format", "stream-json",
      *permission_flags,
      "--model", model,
      "--conversation", provider_conversation_id,
      "-p", prompt,
  ]
  ```

#### Conversation ID Capture & Stream-JSON Normalization (`agy`)
Matches `gem-script.sh` lines 74–119:
Each stdout line is parsed as `obj = json.loads(line)`:
1. **Conversation ID capture**:
   - Check `conv_id = obj.get("conversation_id") or (obj.get("result") or {}).get("conversation_id")`.
   - If `conv_id` is non-empty and `state.provider_conversation_id is None`:
     - Set `state.provider_conversation_id = str(conv_id)`.
     - Emit `NormalizedEvent(type=SESSION_INIT, provider_conversation_id=state.provider_conversation_id)`.
2. **Step Updates (`"step_update"` in `obj`)**:
   - `s = obj.get("step_update") or {}`
   - `step_type = s.get("step_type")`
   - **Agent response (`step_type == "agent_response"`)**:
     - `delta = s.get("text_delta") or ""`
     - If `delta`:
       - Append `delta` to `state.accumulated_text`.
       - Emit `NormalizedEvent(type=TEXT_DELTA, text=delta, provider_conversation_id=state.provider_conversation_id)`.
   - **Thinking (`step_type == "thinking"`)**:
     - `delta = s.get("text_delta") or ""`
     - If `delta`: emit `NormalizedEvent(type=THINKING_DELTA, text=delta)`.
   - **Tool step (`step_type == "tool"`)**:
     - `step_idx = str(s.get("step_index") or "")`
     - `tool_name = str(s.get("tool_name") or "tool")`
     - `tool_state = str(s.get("state") or "RUNNING")`
     - `tool_info = s.get("tool_info") or {}`
     - `params = tool_info.get("parameters") or {}`
     - If `step_idx` not in `state.seen_tool_steps`:
       - `state.seen_tool_steps[step_idx] = "RUNNING"`
       - Emit `NormalizedEvent(type=TOOL_USE, tool_call_id=step_idx, tool_name=tool_name, tool_input=params)`.
     - If `tool_state in ("DONE", "ERROR", "COMPLETED")` and `state.seen_tool_steps.get(step_idx) != tool_state`:
       - `state.seen_tool_steps[step_idx] = tool_state`
       - `is_err = (tool_state == "ERROR")`
       - `err_obj = tool_info.get("error") or {}`
       - `out_str = str(err_obj.get("message") if is_err else (tool_info.get("output") or "OK"))`
       - Emit `NormalizedEvent(type=TOOL_RESULT, tool_call_id=step_idx, tool_name=tool_name, tool_input=params, tool_output=out_str, is_error=is_err)`.
3. **Final Result (`obj.get("event") == "result"`)**:
   - `res = obj.get("result") or {}`
   - `status = str(res.get("status") or "ERROR")`
   - `state.final_status = status`
   - `response_text = (res.get("response") or "".join(state.accumulated_text)).strip()`
   - `state.final_response = response_text`
   - `denied = [str(a.get("display_name") or a.get("action") or "unknown") for a in (res.get("denied_actions") or [])]`
   - `state.denied_actions = denied`
   - `is_err = (status != "SUCCESS")`
   - If `is_err`: `state.error_message = str(res.get("error") or "Turn ended without SUCCESS status")`
   - Emit `NormalizedEvent(type=TURN_RESULT, provider_conversation_id=state.provider_conversation_id, text=response_text, is_error=is_err, denied_actions=denied, raw=obj)`.

#### Model List (`agy`)
Executes `agy models` asynchronously with a 5-second timeout (`asyncio.create_subprocess_exec(agy_bin, "models", ...)`), parses model IDs from stdout tokens matching `^[a-z0-9][a-z0-9._-]+$`, and merges with the fallback list if the CLI output is empty or fails:
```python
FALLBACK_AGY_MODELS = [
    {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
    {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)"},
    {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5 (Medium via AGY)"},
]
```

---

## 7. Session Manager & Subprocess Lifecycle (`backend/session_manager.py`)

### 7.1 Responsibilities
- Owns active subprocess handles in `self._active_procs: Dict[str, asyncio.subprocess.Process]` keyed by `session_id`.
- Prevents concurrent turns on the same session (raises HTTP `409 Conflict` if `session_id` is already in `self._active_procs`).
- Runs turns asynchronously, streaming `NormalizedEvent` items to both `~/.agent-relay/turns/<turn_id>.jsonl` and `EventBus` (`pubsub.py`).
- Captures the `provider_conversation_id` from the stream and persists it onto `sessions.provider_session_id` so subsequent turns resume the conversation.
- Implements clean stop (`SIGTERM` -> 3s grace -> `SIGKILL`) and turn timeouts.

### 7.2 Public Interface

```python
import asyncio
from typing import Dict, List, Optional
from backend.config import AppConfig
from backend.db import Database
from backend.models import NormalizedEvent, TurnStatus
from backend.pubsub import EventBus

class SessionManager:
    def __init__(self, config: AppConfig, db: Database, bus: EventBus) -> None: ...

    def is_session_running(self, session_id: str) -> bool: ...

    async def start_turn(
        self,
        session_id: str,
        prompt: str,
        pipeline_run_id: Optional[str] = None,
        stage_run_id: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ) -> str:
        """
        Validates session exists and is not currently running.
        Creates a new row in `turns` with status='running'.
        Schedules `self._execute_turn(...)` as an asyncio.Task and returns `turn_id` immediately.
        """

    async def run_turn_and_wait(
        self,
        session_id: str,
        prompt: str,
        pipeline_run_id: Optional[str] = None,
        stage_run_id: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ) -> Dict[str, object]:
        """
        Used by PipelineEngine: starts a turn and awaits its completion, returning the final
        turn record dict from SQLite.
        """

    async def stop_session(self, session_id: str) -> bool:
        """
        If a subprocess is running for `session_id`, marks the active turn as `stopped`,
        sends SIGTERM to its process group, waits up to 3.0s, sends SIGKILL if needed,
        and updates session status to 'stopped'. Returns True if a running process was stopped.
        """

    def read_turn_events(self, turn_id: str) -> List[NormalizedEvent]:
        """Reads `~/.agent-relay/turns/<turn_id>.jsonl` and returns parsed NormalizedEvent list."""
```

### 7.3 Subprocess Execution, Process Groups & Stop/Timeout Logic

1. **Spawning**:
   ```python
   proc = await asyncio.create_subprocess_exec(
       *cmd,
       cwd=session["working_dir"],
       stdin=asyncio.subprocess.DEVNULL,
       stdout=asyncio.subprocess.PIPE,
       stderr=asyncio.subprocess.PIPE,
       start_new_session=True,  # Makes proc.pid the process group leader (pgid == proc.pid)
   )
   ```
   `stdin=DEVNULL` ensures the CLI never blocks waiting on terminal input (equivalent to `< /dev/null` in `gem-script.sh`).
2. **Streaming**:
   - An async reader reads `proc.stdout` line by line (`await proc.stdout.readline()`), writes the raw line to `<turn_id>.raw.jsonl`, calls `adapter.parse_stream_line(line_str, state)`, writes each `NormalizedEvent` to `<turn_id>.jsonl`, and publishes `{"event": "turn_event", "data": ...}` to `EventBus`.
   - If `state.provider_conversation_id` becomes populated and `session["provider_session_id"]` was `None`, `db.update_session_provider_id(session_id, state.provider_conversation_id)` is called immediately.
   - A concurrent task reads `proc.stderr` into `<turn_id>.stderr.log`.
3. **Timeout & Stop**:
   - The stdout/stderr reading + `proc.wait()` is wrapped in `asyncio.wait_for(..., timeout=effective_timeout)`.
   - On `asyncio.TimeoutError`:
     - Send `os.killpg(proc.pid, signal.SIGTERM)`, wait up to `3.0` seconds, then `os.killpg(proc.pid, signal.SIGKILL)`.
     - Set turn status to `TurnStatus.TIMEOUT`, session status to `SessionStatus.ERROR`, and record error `"Turn timed out after {effective_timeout}s"`.
   - On `stop_session(session_id)`:
     - Add `turn_id` to `self._stopped_turns`, send `os.killpg(proc.pid, signal.SIGTERM)`, wait `3.0` seconds, then `os.killpg(proc.pid, signal.SIGKILL)`.
     - Set turn status to `TurnStatus.STOPPED` and session status to `SessionStatus.STOPPED`.
4. **Completion Determination**:
   - Consistent with `gem-script.sh` lines 79–81: a turn succeeds (`TurnStatus.COMPLETED`) if and only if it was not stopped/timed out, `proc.returncode == 0`, and `state.final_status == "SUCCESS"`. Otherwise, status is `TurnStatus.ERROR`, and the last 15 non-empty lines of `<turn_id>.stderr.log` are appended to the turn's `error_message`.

---

## 8. Pipeline Engine (`backend/pipeline_engine.py`)

### 8.1 State Machine

```text
                  start_run()
   [PENDING] ─────────────────────► [RUNNING]
                                     │   ▲
          stage has                  │   │ approve_run()
       require_approval=True         ▼   │
                             [WAITING_APPROVAL]
                                     │
       ┌─────────────────────────────┼─────────────────────────────┐
       │ all stages complete         │ stage error / timeout /     │ stop_run()
       │ (and Reviewer APPROVED)     │ max review loops exceeded   │
       ▼                             ▼                             ▼
  [COMPLETED]                    [FAILED]                      [STOPPED]
```

### 8.2 Stage Execution & Review Rework Loop Flow

Given a pipeline with stages $S_0, S_1, \dots, S_{k-1}$ and `max_review_loops` (default `2`, configurable `0..5`):

1. **Initialization**:
   - Create `pipeline_runs` record with `status = 'running'`, `current_stage_index = 0`, `review_loop_count = 0`.
   - Start background task `_run_pipeline_loop(run_id)`.
2. **Stage Input Construction (Exact Handoff Prompt Templates)**:
   - **Case A — Stage 0 (First stage in pipeline)**:
     ```text
     # Pipeline Task

     {pipeline_run.task_prompt}
     ```
   - **Case B — Normal Forward Handoff (Stage $i$, where $i > 0$, not a rework loop)**:
     ```text
     # Pipeline Task (Original User Request)

     {pipeline_run.task_prompt}

     ---

     # Output from Previous Stage ({prev_stage_name} — {prev_session_role})

     {prev_stage_output}

     ---

     # Your Instructions for Stage {i + 1} ({current_stage_name})

     Proceed with your role based on the task and the previous stage output above.{optional_approval_note_block}
     ```
   - **Case C — Reviewer -> Implementer Rework Handoff (when Reviewer returns `CHANGES_REQUESTED`)**:
     Sent to the **most recent Implementer stage's session** prior to the Reviewer stage (resuming its existing conversation):
     ```text
     # Code Review Feedback (Rework Loop {loop_num} of {max_review_loops})

     The Code Reviewer reviewed your implementation and requested changes.

     ## Reviewer Findings
     {verdict.findings_text}

     ## Full Reviewer Report
     {reviewer_stage_output}

     ---

     Please fix every finding above in the working directory and reply with a summary of the fixes made.{optional_approval_note_block}
     ```
   - **Case D — Implementer -> Reviewer Re-Review Handoff (after Implementer completes rework)**:
     Sent back to the **Reviewer stage's session** (resuming its existing conversation):
     ```text
     # Re-Review Request (Rework Loop {loop_num} of {max_review_loops})

     The Implementer has addressed your previous findings.

     ## Implementer Rework Report
     {implementer_rework_output}

     ---

     Please inspect the updated workspace changes and conclude your reply with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED` + `FINDINGS:`.{optional_approval_note_block}
     ```
   *(If the user provided an optional `note` when clicking "Approve & Continue", `{optional_approval_note_block}` is `\n\n## User Approval Note\n{note}`.)*

3. **Reviewer Detection & Verdict Parsing**:
   - A stage is treated as a **Reviewer Stage** if its bound session has `role == RoleId.REVIEWER`.
   - When a Reviewer stage exists in a pipeline, validation at pipeline creation/run time requires that at least one stage *before* the Reviewer stage is bound to a session with `role == RoleId.IMPLEMENTER` (if not, `POST /api/pipelines` returns `400 Bad Request` with a clear validation message).
   - After a Reviewer stage's turn finishes with `TurnStatus.COMPLETED`, `parse_reviewer_verdict(reply_text)` is called:

```python
import re
from dataclasses import dataclass
from typing import Optional

@dataclass
class ReviewVerdict:
    verdict: str          # "APPROVED" | "CHANGES_REQUESTED" | "MISSING"
    findings_text: str    # Extracted findings bullet list or trailing text

_VERDICT_RE = re.compile(
    r"(?:^|\n)\s*[*`#_~>\-\s]*VERDICT\s*:\s*[*`_\s]*(APPROVED|CHANGES_REQUESTED)[*`_\s]*(?:\n|$)",
    re.IGNORECASE,
)
_FINDINGS_RE = re.compile(
    r"FINDINGS\s*:\s*\n(.*)",
    re.IGNORECASE | re.DOTALL,
)

def parse_reviewer_verdict(reply_text: str) -> ReviewVerdict:
    """
    Finds the LAST occurrence of `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED` in `reply_text`.
    - If not found, returns ReviewVerdict(verdict="MISSING", findings_text="").
    - If "APPROVED", returns ReviewVerdict(verdict="APPROVED", findings_text="").
    - If "CHANGES_REQUESTED", extracts text after `FINDINGS:` following the last verdict match
      (or all text following the verdict line if `FINDINGS:` header was omitted).
    """
```

4. **Handling the Parsed Verdict**:
   - **`verdict == "MISSING"`**:
     - The stage run is marked `StageRunStatus.FAILED` with error `"Reviewer reply did not contain a valid 'VERDICT: APPROVED' or 'VERDICT: CHANGES_REQUESTED' line."`.
     - The pipeline run transitions to `PipelineRunStatus.FAILED` and stops cleanly.
   - **`verdict == "APPROVED"`**:
     - The stage run is marked `StageRunStatus.COMPLETED` with `verdict = "APPROVED"`.
     - If `stage.require_approval` is `True` and there is a subsequent stage $i + 1$, pause in `WAITING_APPROVAL` before advancing to stage $i + 1$.
     - Otherwise advance to stage $i + 1$ (or mark pipeline `PipelineRunStatus.COMPLETED` if Reviewer was the last stage).
   - **`verdict == "CHANGES_REQUESTED"`**:
     - The stage run is marked `StageRunStatus.CHANGES_REQUESTED` with `verdict = "CHANGES_REQUESTED"` and `findings = verdict.findings_text`.
     - Check `if review_loop_count >= pipeline.max_review_loops`:
       - Mark pipeline run `PipelineRunStatus.FAILED` with error `"Maximum review loops ({max_review_loops}) reached; Reviewer still requested changes."` and stop cleanly.
     - Otherwise (`review_loop_count < max_review_loops`):
       - Increment `review_loop_count += 1`.
       - Locate the nearest preceding stage $imp\_idx < i$ whose bound session has `role == RoleId.IMPLEMENTER`.
       - If `stage.require_approval` is `True`, pause in `WAITING_APPROVAL` before jumping back to $imp\_idx$.
       - Execute rework on stage $imp\_idx$ (Case C prompt), optionally pause if stage $imp\_idx$ has `require_approval=True`, and then execute re-review on stage $i$ (Case D prompt).

5. **Per-Stage Pause for Approval (`require_approval`)**:
   - When a stage with `require_approval=True` finishes and there is a next step (either the next stage or a rework handoff), `PipelineEngine`:
     1. Updates `pipeline_runs.status = 'waiting_approval'` and `pipeline_stage_runs.status = 'waiting_approval'`.
     2. Emits SSE event `pipeline_waiting_approval`.
     3. Awaits an `asyncio.Event` stored in `self._approval_events[run_id]`.
   - When `approve_run(run_id, note)` is called via `POST /api/pipeline-runs/{run_id}/approve`, it stores `note`, sets the `asyncio.Event`, and transitions the pipeline back to `running`.
   - When `stop_run(run_id)` is called via `POST /api/pipeline-runs/{run_id}/stop`, it cancels any waiting approval event, calls `session_manager.stop_session(...)` on any currently running stage session, and transitions the pipeline run to `PipelineRunStatus.STOPPED`.

### 8.3 `PipelineEngine` Public Interface

```python
from typing import Dict, Optional
from backend.db import Database
from backend.pubsub import EventBus
from backend.session_manager import SessionManager

class PipelineEngine:
    def __init__(self, db: Database, session_manager: SessionManager, bus: EventBus) -> None: ...

    async def start_pipeline_run(self, pipeline_id: str, task_prompt: str) -> Dict[str, object]:
        """Creates a pipeline_run record, launches background execution task, returns run dict."""

    async def approve_pipeline_run(self, run_id: str, note: Optional[str] = None) -> Dict[str, object]:
        """Resumes a pipeline run currently in 'waiting_approval' status."""

    async def stop_pipeline_run(self, run_id: str) -> Dict[str, object]:
        """Stops a running or waiting_approval pipeline run and kills any active stage turn."""
```

---

## 9. Persistence Schema (`backend/db.py`)

SQLite database at `~/.agent-relay/state.db` initialized with `PRAGMA journal_mode=WAL;` and `PRAGMA foreign_keys=ON;`.

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,                      -- UUID v4
    name TEXT NOT NULL,
    provider TEXT NOT NULL,                   -- 'claude' | 'antigravity'
    model TEXT NOT NULL,
    role TEXT NOT NULL,                       -- 'designer' | 'planner' | 'implementer' | 'reviewer' | 'custom'
    custom_prompt TEXT NOT NULL DEFAULT '',
    permission_level TEXT NOT NULL,           -- 'readonly' | 'edit' | 'full'
    working_dir TEXT NOT NULL,
    provider_session_id TEXT,                 -- Captured CLI session_id / conversation_id for resume
    status TEXT NOT NULL DEFAULT 'idle',      -- 'idle' | 'running' | 'error' | 'stopped'
    turn_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,                 -- ISO-8601 UTC
    updated_at TEXT NOT NULL                  -- ISO-8601 UTC
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,                      -- UUID v4
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL,             -- 1-indexed per session
    prompt TEXT NOT NULL,
    response_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',   -- 'running' | 'completed' | 'error' | 'stopped' | 'timeout'
    error_message TEXT,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    failed_tool_count INTEGER NOT NULL DEFAULT 0,
    denied_actions_json TEXT NOT NULL DEFAULT '[]',
    pipeline_run_id TEXT,
    stage_run_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_turns_session_id ON turns(session_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_turns_pipeline_run_id ON turns(pipeline_run_id);

CREATE TABLE IF NOT EXISTS pipelines (
    id TEXT PRIMARY KEY,                      -- UUID v4
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    max_review_loops INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_stages (
    id TEXT PRIMARY KEY,                      -- UUID v4
    pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    stage_order INTEGER NOT NULL,             -- 0-indexed position
    name TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
    require_approval INTEGER NOT NULL DEFAULT 0 -- 0 or 1 (boolean)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_stages_pipeline ON pipeline_stages(pipeline_id, stage_order);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,                      -- UUID v4
    pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    pipeline_name TEXT NOT NULL,
    task_prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- 'pending'|'running'|'waiting_approval'|'completed'|'failed'|'stopped'
    current_stage_order INTEGER NOT NULL DEFAULT 0,
    review_loop_count INTEGER NOT NULL DEFAULT 0,
    max_review_loops INTEGER NOT NULL DEFAULT 2,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started_at ON pipeline_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_stage_runs (
    id TEXT PRIMARY KEY,                      -- UUID v4
    pipeline_run_id TEXT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    stage_order INTEGER NOT NULL,
    stage_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,     -- 0 on initial pass, 1..N on review rework loops
    require_approval INTEGER NOT NULL DEFAULT 0,
    turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
    input_prompt TEXT NOT NULL DEFAULT '',
    output_text TEXT NOT NULL DEFAULT '',
    verdict TEXT,                             -- NULL | 'APPROVED' | 'CHANGES_REQUESTED' | 'MISSING'
    findings TEXT,
    approval_note TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- 'pending'|'running'|'waiting_approval'|'completed'|'changes_requested'|'failed'|'stopped'
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_run_id ON pipeline_stage_runs(pipeline_run_id, started_at);
```

### Startup Crash Recovery
On application startup (`lifespan` in `backend/main.py`), `db.recover_orphaned_states()` runs once:
- Any `turns` with `status = 'running'` are updated to `status = 'error'`, `error_message = 'Server restarted while turn was in progress'`.
- Any `sessions` with `status = 'running'` are updated to `status = 'idle'`.
- Any `pipeline_runs` or `pipeline_stage_runs` in `'running'` or `'waiting_approval'` are updated to `status = 'failed'`, `error_message = 'Server restarted while pipeline was active'`.

---

## 10. HTTP API & SSE Live-Update Specification

All endpoints are served from `http://127.0.0.1:8765`. JSON responses use `application/json`. Errors return `{"detail": "Human-readable error message"}` with standard HTTP status codes (`400`, `404`, `409`, `422`).

### 10.1 Metadata Endpoints

#### `GET /api/meta`
Returns providers, available models, built-in roles, permission levels, and default workspace path (`os.getcwd()`).
- **Response `200 OK`**:
```json
{
  "default_working_dir": "/Users/mac/MY/Benevia/agent-relay",
  "providers": [
    {
      "id": "claude",
      "name": "Claude Code",
      "models": [
        {"id": "sonnet", "label": "Claude Sonnet (latest alias)"},
        {"id": "opus", "label": "Claude Opus (latest alias)"},
        {"id": "haiku", "label": "Claude Haiku (latest alias)"}
      ]
    },
    {
      "id": "antigravity",
      "name": "Antigravity (AGY)",
      "models": [
        {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
        {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)"},
        {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5 (Medium via AGY)"}
      ]
    }
  ],
  "roles": [
    {
      "id": "designer",
      "name": "Designer",
      "description": "Produces system architecture and UI/UX design specifications. Read-only by default.",
      "default_permission": "readonly",
      "system_prompt": "..."
    }
  ],
  "permissions": [
    {"id": "readonly", "label": "Read-Only (Plan)", "description": "Inspect files only; no edits or shell commands."},
    {"id": "edit", "label": "Edit Files", "description": "Auto-accept file edits in workspace."},
    {"id": "full", "label": "Full Access", "description": "Auto-approve file edits and shell commands."}
  ]
}
```

### 10.2 Session Endpoints

#### `GET /api/sessions`
Lists all sessions ordered by `created_at ASC`.
- **Response `200 OK`**: `{"sessions": [SessionObject, ...]}`

#### `POST /api/sessions`
Creates a new session. If `permission_level` is omitted, defaults to the selected role's `default_permission`. Validates `working_dir` via `validate_working_dir`.
- **Request Body**:
```json
{
  "name": "Arch Designer",
  "provider": "antigravity",
  "model": "gemini-4-argon-high",
  "role": "designer",
  "custom_prompt": "",
  "permission_level": "readonly",
  "working_dir": "/Users/mac/MY/Benevia/agent-relay"
}
```
- **Response `201 Created`**: `SessionObject`

#### `GET /api/sessions/{session_id}`
Returns session metadata and all its turns (ordered by `turn_number ASC`).
- **Response `200 OK`**:
```json
{
  "session": {
    "id": "f47ac10b-...",
    "name": "Arch Designer",
    "provider": "antigravity",
    "model": "gemini-4-argon-high",
    "role": "designer",
    "custom_prompt": "",
    "permission_level": "readonly",
    "working_dir": "/Users/mac/MY/Benevia/agent-relay",
    "provider_session_id": "conv_987",
    "status": "idle",
    "turn_count": 1,
    "created_at": "2026-11-02T18:40:00Z",
    "updated_at": "2026-11-02T18:41:12Z"
  },
  "turns": [
    {
      "id": "b12cd34e-...",
      "session_id": "f47ac10b-...",
      "turn_number": 1,
      "prompt": "Design the database schema",
      "response_text": "Here is the schema...",
      "status": "completed",
      "error_message": null,
      "tool_call_count": 2,
      "failed_tool_count": 0,
      "denied_actions": [],
      "pipeline_run_id": null,
      "stage_run_id": null,
      "started_at": "2026-11-02T18:40:05Z",
      "finished_at": "2026-11-02T18:41:12Z",
      "duration_ms": 67000
    }
  ]
}
```

#### `PATCH /api/sessions/{session_id}`
Updates mutable session fields (`name`, `permission_level`, `model`, `custom_prompt`) when the session is not currently running.
- **Request Body**:
```json
{
  "name": "Arch Designer",
  "permission_level": "edit",
  "model": "gemini-4-argon-high"
}
```
- **Response `200 OK`**: `SessionObject`

#### `DELETE /api/sessions/{session_id}`
Stops the session if running and deletes the session and its turns. Returns `409 Conflict` if the session is bound to an existing pipeline stage (instructing the user to delete or update the pipeline first).
- **Response `200 OK`**: `{"deleted": true, "session_id": "..."}`

#### `POST /api/sessions/{session_id}/turns`
Starts a new conversational turn on the session asynchronously. Returns `409 Conflict` if the session is already running a turn.
- **Request Body**:
```json
{
  "prompt": "Read docs/reference/gem-script.sh and summarize how conversation IDs work.",
  "timeout_sec": 600
}
```
- **Response `202 Accepted`**:
```json
{
  "turn_id": "b12cd34e-...",
  "session_id": "f47ac10b-...",
  "turn_number": 1,
  "status": "running"
}
```

#### `POST /api/sessions/{session_id}/stop`
Stops a running turn on the session via `SIGTERM` / `SIGKILL`.
- **Response `200 OK`**: `{"stopped": true, "session_id": "..."}`

#### `GET /api/turns/{turn_id}`
Returns the turn record and its full list of persisted `NormalizedEvent` items from `~/.agent-relay/turns/<turn_id>.jsonl`.
- **Response `200 OK`**:
```json
{
  "turn": { "...": "..." },
  "events": [
    {
      "seq": 1,
      "timestamp": "2026-11-02T18:40:06Z",
      "type": "session_init",
      "provider_conversation_id": "conv_987",
      "text": null,
      "tool_call_id": null,
      "tool_name": null,
      "tool_input": null,
      "tool_output": null,
      "is_error": false,
      "denied_actions": []
    }
  ]
}
```

---

### 10.3 Pipeline & Run History Endpoints

#### `GET /api/pipelines`
Lists all saved pipeline definitions with their ordered stages.
- **Response `200 OK`**: `{"pipelines": [PipelineObject, ...]}`

#### `POST /api/pipelines`
Creates a new pipeline definition. Validates that `stages` is non-empty, each `session_id` exists, and if any stage has a `reviewer` session, at least one preceding stage has an `implementer` session.
- **Request Body**:
```json
{
  "name": "Full Feature Pipeline",
  "description": "Design -> Plan -> Implement -> Review",
  "max_review_loops": 2,
  "stages": [
    {"name": "Design", "session_id": "sess-1", "require_approval": false},
    {"name": "Plan", "session_id": "sess-2", "require_approval": true},
    {"name": "Implement", "session_id": "sess-3", "require_approval": false},
    {"name": "Code Review", "session_id": "sess-4", "require_approval": false}
  ]
}
```
- **Response `201 Created`**: `PipelineObject`

#### `PUT /api/pipelines/{pipeline_id}`
Updates an existing pipeline definition and replaces its ordered stages.
- **Request Body**: Same as `POST /api/pipelines`.
- **Response `200 OK`**: `PipelineObject`

#### `DELETE /api/pipelines/{pipeline_id}`
Deletes a pipeline definition (returns `409 Conflict` if a run of this pipeline is currently active).
- **Response `200 OK`**: `{"deleted": true, "pipeline_id": "..."}`

#### `POST /api/pipelines/{pipeline_id}/runs`
Starts a new pipeline run with a single user task prompt. Returns `409 Conflict` if any session in the pipeline is currently busy.
- **Request Body**:
```json
{
  "task_prompt": "Add a healthcheck endpoint /api/health and unit test it."
}
```
- **Response `202 Accepted`**: `PipelineRunDetailObject`

#### `GET /api/pipeline-runs`
Lists all pipeline runs (newest first) for Run History.
- **Response `200 OK`**: `{"runs": [PipelineRunSummaryObject, ...]}`

#### `GET /api/pipeline-runs/{run_id}`
Returns the pipeline run and all of its `stage_runs` (ordered chronologically by creation/execution order, including rework loop iterations).
- **Response `200 OK`**:
```json
{
  "run": {
    "id": "run-123",
    "pipeline_id": "pipe-1",
    "pipeline_name": "Full Feature Pipeline",
    "task_prompt": "Add a healthcheck endpoint /api/health and unit test it.",
    "status": "waiting_approval",
    "current_stage_order": 1,
    "review_loop_count": 0,
    "max_review_loops": 2,
    "error_message": null,
    "started_at": "2026-11-02T18:45:00Z",
    "finished_at": null,
    "duration_ms": null
  },
  "stage_runs": [
    {
      "id": "sr-1",
      "pipeline_run_id": "run-123",
      "stage_id": "st-1",
      "stage_order": 0,
      "stage_name": "Design",
      "session_id": "sess-1",
      "session_name": "Arch Designer",
      "provider": "antigravity",
      "model": "gemini-4-argon-high",
      "role": "designer",
      "iteration": 0,
      "require_approval": false,
      "turn_id": "turn-101",
      "input_prompt": "# Pipeline Task\n\nAdd a healthcheck endpoint...",
      "output_text": "Design spec...",
      "verdict": null,
      "findings": null,
      "approval_note": null,
      "status": "completed",
      "error_message": null,
      "started_at": "2026-11-02T18:45:00Z",
      "finished_at": "2026-11-02T18:45:25Z",
      "duration_ms": 25000
    }
  ]
}
```

#### `POST /api/pipeline-runs/{run_id}/approve`
Approves a paused stage (`status == "waiting_approval"`) and continues the pipeline.
- **Request Body**:
```json
{
  "note": "Make sure /api/health also returns the SQLite status."
}
```
- **Response `200 OK`**: `PipelineRunDetailObject`

#### `POST /api/pipeline-runs/{run_id}/stop`
Stops an active or waiting pipeline run immediately.
- **Response `200 OK`**: `PipelineRunDetailObject`

---

### 10.4 Server-Sent Events (SSE) Channel (`GET /api/events`)

A single multiplexed SSE stream (`Content-Type: text/event-stream`, `Cache-Control: no-cache`) using `backend/pubsub.py`. Each message is formatted as:
```text
event: <event_name>
data: <single-line JSON object>

```
A `: keepalive` comment or `event: ping` is sent every 15 seconds if idle.

| SSE `event` Name | Payload Shape (`data`) | When Emitted |
| :--- | :--- | :--- |
| `session_updated` | `{"session": SessionObject}` | Session created, status changed, provider_session_id captured, or updated. |
| `turn_started` | `{"turn": TurnObject}` | A turn begins executing. |
| `turn_event` | `{"session_id": "...", "turn_id": "...", "pipeline_run_id": "...", "stage_run_id": "...", "event": NormalizedEvent}` | Every normalized streaming event (`text_delta`, `tool_use`, `tool_result`, etc.). |
| `turn_completed` | `{"turn": TurnObject}` | A turn finishes (`completed`, `error`, `stopped`, `timeout`). |
| `pipeline_run_updated` | `{"run": PipelineRunObject}` | Pipeline run status, current stage, or loop counter changes. |
| `pipeline_stage_updated` | `{"stage_run": StageRunObject}` | Stage run starts, completes, requests changes, fails, or pauses. |
| `pipeline_waiting_approval` | `{"run_id": "...", "stage_run": StageRunObject}` | Stage with `require_approval=True` finishes and awaits user action. |

---

## 11. Security Architecture

1. **Localhost Binding Only**: Uvicorn binds strictly to `127.0.0.1` (`DEFAULT_HOST = "127.0.0.1"`). Port validation rejects `{5000, 7000, 5090, 5186}` at startup.
2. **No Shell Execution (`shell=False`)**: Every CLI invocation uses `asyncio.create_subprocess_exec(*cmd, ...)` where `cmd` is a `List[str]`. User prompts, system prompts, and model names are passed as distinct argv elements, eliminating shell injection vectors.
3. **Working Directory Path Validation**: `validate_working_dir()` canonicalizes paths via `os.path.realpath()`, verifies directory existence, and rejects root (`/`) and sensitive OS trees (`/System`, `/etc`, `/private/etc`, `/var`, `/private/var`, `/usr`, `/bin`, `/sbin`, `/dev`, `/Library`).
4. **Data Directory Permissions**: `~/.agent-relay/` is created with `0o700` permissions so session transcripts and prompts are readable only by the local macOS user.

---

## 12. Setup & Run Commands

```bash
# 1. Create virtual environment with macOS system Python 3.9.6
python3 -m venv .venv

# 2. Install dependencies
.venv/bin/pip install -r requirements.txt

# 3. Start Agent Relay on http://127.0.0.1:8765
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

---

## 13. Testing Strategy (Zero Real Model Calls)

All unit and integration tests run offline in milliseconds using fake CLI scripts in `tests/fixtures/`:

1. **`tests/fixtures/fake_claude.py` & `tests/fixtures/fake_agy.py`**:
   - Executable Python 3.9 scripts (`#!/usr/bin/env python3`) injected into `AppConfig` via `CLAUDE_BIN` and `AGY_BIN` environment variables in `tests/conftest.py`.
   - Each script logs its received `sys.argv` and `os.getcwd()` and emits realistic `stream-json` NDJSON lines matching the real CLI formats:
     - `fake_claude.py`: Emits `{"type": "system", "subtype": "init", "session_id": "fake-claude-uuid-1"}` (or reuses the ID passed to `--resume`), `stream_event` text deltas, optional `assistant` `tool_use` + `user` `tool_result`, and final `{"type": "result", "subtype": "success", "is_error": false, "result": "..."}`.
     - `fake_agy.py`: Supports `agy models` subcommand; in `-p` mode emits `{"conversation_id": "fake-agy-conv-1", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "..."}}`, tool `step_update` events, and final `{"conversation_id": "...", "event": "result", "result": {"status": "SUCCESS", "response": "..."}}`.
   - **Deterministic Behavior Triggers via Prompt Tokens**:
     - Prompt contains `[CHANGES_REQUESTED]` -> Reviewer script emits `VERDICT: CHANGES_REQUESTED\nFINDINGS:\n- Fix bug in foo.py` on its first turn, and `VERDICT: APPROVED` on subsequent `--resume` / `--conversation` turns!
     - Prompt contains `[ALWAYS_REJECT]` -> Reviewer always emits `VERDICT: CHANGES_REQUESTED` (to test `max_review_loops` exhaustion).
     - Prompt contains `[MISSING_VERDICT]` -> Reviewer replies without any `VERDICT:` line (to test missing verdict failure handling).
     - Prompt contains `[FAIL]` -> Script exits with code `1` and writes an error to `stderr` (to test error propagation).
     - Prompt contains `[SLOW]` -> Script sleeps for `10` seconds (to test `stop_session` `SIGTERM`/`SIGKILL` and turn timeout handling).

2. **Test Suite Breakdown**:
   - `tests/test_providers.py`: Verifies exact CLI argv construction for Turn 1 and Turn 2+ across `readonly`, `edit`, and `full` permissions for both `claude` and `antigravity`, plus `parse_stream_line` event normalization.
   - `tests/test_sessions.py`: Verifies multi-turn conversation ID capture & persistence, `working_dir` validation, turn timeout, and `stop_session`.
   - `tests/test_pipelines.py`: Verifies `parse_reviewer_verdict` edge cases, 4-stage pipeline handoff prompts, 1-loop rework recovery (`CHANGES_REQUESTED` -> Implementer -> `APPROVED`), `max_review_loops` limit failure, `require_approval` pause/resume with note, and pipeline stop.
   - `tests/test_api.py`: Verifies all REST endpoints and SSE stream delivery via FastAPI `TestClient`.

Run the test suite with:
```bash
.venv/bin/pytest -v
```
