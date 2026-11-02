# Agent Relay

Agent Relay is a local, single-user macOS web application for managing multi-turn AI coding sessions across **Claude Code (`claude`)** and **Antigravity (`agy`)** CLIs, assigning specialized engineering roles with enforced tool permissions, and orchestrating multi-stage pipelines with automated code-review rework loops.

All model interactions occur through locally installed, already-authenticated CLI binaries executed as non-interactive streaming subprocesses (`stream-json`). No API keys or external cloud relays are used.

---

## Prerequisites

- **macOS** with system **Python 3.9+** (tested on macOS system Python 3.9.6).
- **Claude Code CLI (`claude`)** installed and authenticated via `claude login`.
- **Antigravity CLI (`agy`)** installed and authenticated.

---

## Quickstart

1. **Create and activate a virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Start Agent Relay**:
   ```bash
   python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
   ```

4. **Open in browser**:
   Navigate to [http://127.0.0.1:8765](http://127.0.0.1:8765).

---

## Configuration & Environment Variables

Agent Relay runs on `127.0.0.1` by default and validates environment variables at startup:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `AGENT_RELAY_HOME` | `~/.agent-relay` | Root data directory for SQLite database (`state.db`) and turn stream event logs (`turns/`). Directory permissions are enforced as `0o700`. |
| `AGENT_RELAY_PORT` | `8765` | Server port on `127.0.0.1`. Blocked system/Apple ports (`5000`, `7000`, `5090`, `5186`) are rejected at startup. |
| `CLAUDE_BIN` | `claude` | Path or binary name for Claude Code CLI. Resolved via `~/.local/bin/claude` or `PATH`. |
| `AGY_BIN` | `agy` | Path or binary name for Antigravity CLI. Resolved via `~/.local/bin/agy` or `PATH`. |
| `AGENT_RELAY_TURN_TIMEOUT` | `600` | Default timeout in seconds for a single agent conversational turn. |

---

## Roles & Permission Mapping

Every session is assigned an engineering role and an enforced permission level.

### Built-in Roles

- **Designer** (`readonly`): Produces system architecture and UI/UX design specifications. Inspects files only; does not edit code.
- **Planner** (`readonly`): Decomposes requirements and design specifications into step-by-step file-level implementation plans.
- **Implementer** (`edit`): Writes and edits code in the working directory according to the plan or review findings.
- **Code Reviewer** (`readonly`): Inspects workspace changes and emits a machine-readable verdict (`VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED` + `FINDINGS:`).
- **Custom** (`readonly`): Custom role with user-defined system instructions.

### Permission Levels

| Permission Level | Description | Claude CLI (`claude`) Flags | Antigravity CLI (`agy`) Flags |
| :--- | :--- | :--- | :--- |
| `readonly` | Read-only inspection; edits and shell commands blocked. | `--permission-mode plan --permission-prompts none --disallowedTools Edit,Write,NotebookEdit,Bash` | `--mode plan` |
| `edit` | Automatically accepts file edits; interactive shell prompts denied. | `--permission-mode acceptEdits --permission-prompts none` | `--mode accept-edits` |
| `full` | Full access; auto-approves file edits and shell commands. | `--permission-mode bypassPermissions --dangerously-skip-permissions` | `--mode accept-edits --dangerously-skip-permissions` |

---

## Pipelines & Automated Review Loops

Pipelines orchestrate multi-stage workflows across sessions (for example: **Designer → Planner → Implementer → Reviewer**):

1. **Stage Handoffs**: Outputs from completed stages are passed as context to subsequent stages using standardized markdown handoff prompts.
2. **Review Verdicts**: The Code Reviewer outputs a machine-readable verdict block:
   - `VERDICT: APPROVED`
   - `VERDICT: CHANGES_REQUESTED` with a `FINDINGS:` bullet list
3. **Automated Rework Loops**: When changes are requested and the review loop count is below `max_review_loops` (default: 2), the pipeline automatically routes findings back to the preceding Implementer session to address, then prompts the Reviewer for re-review.
4. **Approval Checkpoints**: Individual stages can be configured with `require_approval` to pause execution in `waiting_approval` status until the user approves via the UI with an optional note.

---

## Running Tests

Agent Relay includes an automated test suite using deterministic fake CLI fixtures with zero real model or network calls:

```bash
.venv/bin/pytest -v
```
