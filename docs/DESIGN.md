# Agent Relay — UI/UX & Visual Design Specification

## 1. Design Philosophy & Principles

- **Developer-First Clarity**: High information density without clutter; monospace typography for prompts, tool calls, file paths, and diffs; instant visual distinction between **Providers** (`Claude` vs. `Antigravity`) and **Roles** (`Designer`, `Planner`, `Implementer`, `Code Reviewer`, `Custom`).
- **Live Observability**: Every streaming token, thinking block, and tool invocation renders in real time via SSE with zero page reloads.
- **Zero-Build Native Web**: Pure HTML5, CSS3 custom properties (`prefers-color-scheme` automatic light/dark theming), and modular ES2020 JavaScript. No external fonts, icon packs, or CDN scripts required (all icons use inline SVG).

---

## 2. Visual Style & CSS Variables (`frontend/styles.css`)

### 2.1 Color System (Light & Dark Themes via `prefers-color-scheme`)

```css
:root {
  /* Surfaces & Backgrounds (Light Default) */
  --bg-app: #f8fafc;
  --bg-surface: #ffffff;
  --bg-elevated: #f1f5f9;
  --bg-hover: #e2e8f0;
  --bg-code: #0f172a;
  --text-code: #f8fafc;

  /* Text Hierarchy */
  --text-primary: #0f172a;
  --text-secondary: #475569;
  --text-muted: #64748b;

  /* Borders & Dividers */
  --border-subtle: #e2e8f0;
  --border-strong: #cbd5e1;
  --focus-ring: #2563eb;

  /* Brand & Primary Actions */
  --accent-primary: #2563eb;
  --accent-primary-hover: #1d4ed8;
  --accent-primary-fg: #ffffff;

  /* Semantic Status Colors */
  --status-idle-bg: #f1f5f9;
  --status-idle-fg: #475569;
  --status-running-bg: #dbeafe;
  --status-running-fg: #1d4ed8;
  --status-success-bg: #dcfce7;
  --status-success-fg: #15803d;
  --status-warning-bg: #fef3c7;
  --status-warning-fg: #b45309;
  --status-danger-bg: #fee2e2;
  --status-danger-fg: #b91c1c;
  --status-rework-bg: #f3e8ff;
  --status-rework-fg: #7e22ce;

  /* Provider Badge Colors */
  --provider-claude-bg: #fff1eb;
  --provider-claude-fg: #c2410c;
  --provider-claude-border: #fdba74;

  --provider-agy-bg: #eef2ff;
  --provider-agy-fg: #4338ca;
  --provider-agy-border: #a5b4fc;

  /* Role Badge Colors */
  --role-designer-bg: #fdf2f8;
  --role-designer-fg: #be185d;
  --role-planner-bg: #ecfeff;
  --role-planner-fg: #0e7490;
  --role-implementer-bg: #ecfdf5;
  --role-implementer-fg: #047857;
  --role-reviewer-bg: #fffbeb;
  --role-reviewer-fg: #b45309;
  --role-custom-bg: #f1f5f9;
  --role-custom-fg: #334155;

  /* Layout & Spacing Scale */
  --header-height: 52px;
  --sidebar-width: 280px;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.06);
  --shadow-lg: 0 10px 25px -5px rgba(15, 23, 42, 0.18);

  /* Typography */
  --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
  --font-mono: "SF Mono", ui-monospace, Menlo, Monaco, Consolas, monospace;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg-app: #0b0f19;
    --bg-surface: #111827;
    --bg-elevated: #1e293b;
    --bg-hover: #334155;
    --bg-code: #020617;
    --text-code: #e2e8f0;

    --text-primary: #f8fafc;
    --text-secondary: #cbd5e1;
    --text-muted: #94a3b8;

    --border-subtle: #1e293b;
    --border-strong: #334155;
    --focus-ring: #60a5fa;

    --accent-primary: #3b82f6;
    --accent-primary-hover: #2563eb;
    --accent-primary-fg: #ffffff;

    --status-idle-bg: #1e293b;
    --status-idle-fg: #94a3b8;
    --status-running-bg: rgba(59, 130, 246, 0.2);
    --status-running-fg: #60a5fa;
    --status-success-bg: rgba(34, 197, 94, 0.2);
    --status-success-fg: #4ade80;
    --status-warning-bg: rgba(245, 158, 11, 0.2);
    --status-warning-fg: #fbbf24;
    --status-danger-bg: rgba(239, 68, 68, 0.2);
    --status-danger-fg: #f87171;
    --status-rework-bg: rgba(168, 85, 247, 0.2);
    --status-rework-fg: #c084fc;

    --provider-claude-bg: rgba(234, 88, 12, 0.18);
    --provider-claude-fg: #fb923c;
    --provider-claude-border: rgba(251, 146, 60, 0.35);

    --provider-agy-bg: rgba(99, 102, 241, 0.18);
    --provider-agy-fg: #818cf8;
    --provider-agy-border: rgba(129, 140, 248, 0.35);

    --role-designer-bg: rgba(236, 72, 153, 0.18);
    --role-designer-fg: #f472b6;
    --role-planner-bg: rgba(6, 182, 212, 0.18);
    --role-planner-fg: #22d3ee;
    --role-implementer-bg: rgba(16, 185, 129, 0.18);
    --role-implementer-fg: #34d399;
    --role-reviewer-bg: rgba(245, 158, 11, 0.18);
    --role-reviewer-fg: #fbbf24;
    --role-custom-bg: #1e293b;
    --role-custom-fg: #cbd5e1;
  }
}
```

### 2.2 Badges & Status Indicators

- **Provider Pill**: Rounded pill (`font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 999px; border: 1px solid ...`):
  - `claude` -> `Claude` (warm terracotta `--provider-claude-*`)
  - `antigravity` -> `AGY` (indigo `--provider-agy-*`)
- **Role Pill**: Rounded rectangle (`font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 4px`):
  - `Designer` (pink), `Planner` (cyan), `Implementer` (emerald), `Code Reviewer` (amber), `Custom` (slate).
- **Permission Pill**: Compact badge showing lock/wrench icon + label:
  - `readonly` -> `Read-Only`
  - `edit` -> `Edit Files`
  - `full` -> `Full Access`
- **Status Indicator Dot (`.status-dot`)**: `8px × 8px` circle:
  - `idle`: solid slate (`--status-idle-fg`)
  - `running`: blue (`--status-running-fg`) with `@keyframes pulse` ring animation
  - `waiting_approval`: amber (`--status-warning-fg`) with `@keyframes pulse` ring animation
  - `completed`: solid green (`--status-success-fg`)
  - `changes_requested`: purple (`--status-rework-fg`)
  - `error` / `failed` / `timeout`: solid red (`--status-danger-fg`)
  - `stopped`: hollow grey ring

---

## 3. Application Shell & Responsive Layout

```text
┌───────────────────────────────────────────────────────────────────────────────────┐
│  [⚡ Agent Relay]     [💬 Sessions]  [🔗 Pipelines]  [🕓 Run History]    [● SSE OK] │  52px Top Bar
├──────────────────────┬────────────────────────────────────────────────────────────┤
│ SESSIONS     [+ New] │                                                            │
│ ──────────────────── │                                                            │
│ ● Arch Designer      │                                                            │
│   AGY · Designer     │                                                            │
│ ● Lead Planner       │                     MAIN WORKSPACE                         │
│   Claude · Planner   │       (Switches between active tab:                        │
│ ◉ Code Builder  [■]  │        1. Session Chat View                                │
│   AGY · Implementer  │        2. Pipelines & Live Run View                        │
│ ● Strict Reviewer    │        3. Run History Browser)                             │
│   Claude · Reviewer  │                                                            │
│                      │                                                            │
│                      │                                                            │
└──────────────────────┴────────────────────────────────────────────────────────────┘
       280px Sidebar                         1fr Flexible Main Area
```

### Responsive Breakpoints (`1440px` down to `800px`)
1. **Desktop (`> 1024px`)**: `--sidebar-width: 280px`. Pipeline view displays a 2-column split (left `380px` Pipeline Builder/Selector, right `1fr` Active Run Timeline & Live Stream).
2. **Compact Laptop (`800px – 1024px`)**:
   - `--sidebar-width: 220px` so the Sessions sidebar and Main Workspace fit comfortably side-by-side at `800px` without horizontal scrollbars.
   - Inside the Pipelines view, the 2-column split stacks vertically or switches via a sub-tab toggle (`Builder` | `Active Run`) so stage cards and live stream output always have at least `540px` of readable width.

---

## 4. Screen Specifications

### 4.1 Persistent Left Sidebar: Sessions List (`frontend/js/sessions.js`)

- **Header**: Title `"Sessions"` + primary button `"+ New Session"` (opens New Session Modal).
- **Session List Items (`.session-card`)**:
  - Clicking any session card selects that session and switches the Main Workspace to the **Session Chat View** (if not already there).
  - **Row 1**: Status dot (`.status-dot`) + **Session Name** (`font-weight: 600`, truncated with ellipsis) + inline **Stop Button (`■`)** (visible only when `session.status === 'running'`).
  - **Row 2**: Provider Pill (`Claude` / `AGY`) + Role Pill (`Designer` / `Planner` / `Implementer` / `Reviewer` / `Custom`) + Model name in `--font-mono` (`11px`).
  - **Row 3**: Turn count (`3 turns`) + basename of `working_dir` (hover shows full path).
- **Empty State**: Centered card inside sidebar: *"No sessions yet. Create a Claude or Antigravity session to start chatting or building pipelines."* with a `"+ Create First Session"` button.

---

### 4.2 Modal: New Session (`#new-session-modal`)

Triggered by `"+ New Session"`. Contains a clean form:
1. **Session Name** (`input[type=text]`, required, placeholder `"e.g., AGY Implementer"`).
2. **Provider** (Segmented toggle: `Claude Code` | `Antigravity (AGY)`).
   - Changing provider immediately repopulates the **Model** dropdown from `/api/meta`.
3. **Model** (`<select>` with options from `/api/meta` + an `"Other / Custom model ID..."` option that reveals a text input).
4. **Role** (`<select>`: `Designer`, `Planner`, `Implementer`, `Code Reviewer`, `Custom`).
   - Selecting a role displays its role description and **automatically updates the Permission Level selector** to that role's default (`readonly` for Designer/Planner/Reviewer/Custom; `edit` for Implementer).
   - A collapsible `"View / Append Role Prompt"` box shows the built-in system prompt (read-only preview) and a textarea (`custom_prompt`) where the user can provide instructions (required if `Custom`, optional addendum for built-in roles).
5. **Permission Level** (`<select>`):
   - `Read-Only (Plan)` — Cannot edit files or run commands.
   - `Edit Files (Accept Edits)` — Can create and modify files in workspace.
   - `Full Access (Edit + Commands)` — Auto-approves all tools including shell commands (`--dangerously-skip-permissions`).
   - If the user overrides a non-Implementer role to `Edit Files` or `Full Access`, a subtle amber hint appears: *"Override active: this role is read-only by default."*
6. **Working Directory** (`input[type=text]`, defaults to `meta.default_working_dir`).
7. **Footer**: `"Cancel"` button and `"Create Session"` primary button. Inline error banner if API returns `400` (e.g., invalid path).

---

### 4.3 View 1: Session Chat View (`frontend/js/sessions.js`)

Displayed when a session is selected and the `"Sessions"` top tab is active.

#### Region A — Session Header Bar
- **Left**: Session Name + Status badge + Provider Pill + Model badge + Role Pill.
- **Middle**: Working directory path in `--font-mono` + Conversation ID badge (`conv: <short-id>` once captured after Turn 1, or `not started`).
- **Right**:
  - **Permission Level Quick-Override Dropdown** (`readonly` / `edit` / `full`) — sends `PATCH /api/sessions/{id}` immediately when changed (disabled while a turn is running).
  - **Delete Session (`🗑`)** button (with confirmation prompt; disabled if running).

#### Region B — Multi-Turn Conversation Transcript (`#chat-transcript`)
Scrollable vertical container showing each `Turn` in chronological order (`turn_number` 1..N):
- **User Prompt Bubble**: Right-aligned or full-width top card with header `"Turn #N · User"` (+ `"Pipeline: <name>"` badge if triggered by a pipeline run), timestamp, and pre-wrapped monospace/markdown text.
- **Agent Turn Response Container**:
  1. **Thinking Stream (Collapsible `<details class="thinking-block">`)**:
     - Header: `"💭 Reasoning / Thinking"` (auto-collapsed once text output begins).
  2. **Tool Call Cards (`.tool-card`, inline in stream order)**:
     - Compact collapsible bar showing:
       - Status icon (`⏳` running, `✓` done, `✗` error)
       - Tool name in `--font-mono` (e.g., `write_file`, `Edit`, `Read`, `Bash`)
       - Primary parameter summary (e.g., `Path: backend/main.py` or `command: pytest`)
     - Expanding the card reveals pretty-printed JSON `tool_input` and `tool_output` / error details.
  3. **Assistant Response Body (`.assistant-markdown`)**:
     - Streams live token-by-token as `text_delta` SSE events arrive.
     - Renders fenced code blocks (` ```lang ... ``` `), inline code, headings, bullet lists, and bold text cleanly (HTML-escaped first to prevent XSS).
     - If the turn belongs to a `Code Reviewer` and ends with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`, renders a prominent **Verdict Banner** (`green` for `APPROVED`, `purple/amber` for `CHANGES_REQUESTED`) at the bottom of the message.
  4. **Turn Footer / Error State**:
     - Shows duration (`14.2s`), tool count (`3 tool calls`), and any `denied_actions` warnings (`⚠️ Denied in Read-Only mode: write_file`).
     - If `turn.status === 'error'` or `'timeout'`, shows a red **Error Alert Box** containing `turn.error_message` (including captured CLI `stderr` tail).
     - If `turn.status === 'stopped'`, shows a neutral badge: `"⏹ Turn stopped by user"`.

#### Region C — Prompt Composer Bar
- `textarea#session-prompt-input`: Auto-expanding (`3` to `8` rows), `Cmd+Enter` (or `Ctrl+Enter`) submits.
- **When `session.status !== 'running'`**: Primary button `"Send Turn (⌘↵)"`.
- **When `session.status === 'running'`**: Textarea is disabled; primary button transforms into a high-contrast red button `"■ Stop Turn"` which calls `POST /api/sessions/{id}/stop` + live elapsed timer (`Running... 00:12`).

---

### 4.4 View 2: Pipelines & Live Run View (`frontend/js/pipelines.js` & `frontend/js/runs.js`)

Activated via the `"Pipelines"` top tab. Divided into two coordinated panels:

#### Panel 1 — Pipeline Builder & Launcher
- **Saved Pipelines Selector**: Dropdown or horizontal pill bar of saved pipelines + `"+ New Pipeline"` button + `"Delete"` button.
- **Pipeline Settings Form**:
  - **Pipeline Name** (`input[type=text]`, e.g., `"Design → Plan → Implement → Review"`).
  - **Max Review Loops** (`input[type=number]`, min `0`, max `5`, default `2`) with helper caption: *"On CHANGES_REQUESTED, findings return to the Implementer session and re-run Reviewer up to N loops."*
- **Ordered Stages List (`#pipeline-stages-list`)**:
  - Each stage row (`.stage-editor-card`) displays:
    1. **Stage Number Badge** (`#1`, `#2`, ...) and **Move Up (`↑`) / Move Down (`↓`) / Remove (`×`)** buttons.
    2. **Stage Name Input** (e.g., `"Architecture Design"`, `"Implementation"`, `"Code Review"`).
    3. **Bound Session Dropdown (`<select>`)**: Lists all existing sessions formatted as `"{session.name} [{PROVIDER} · {Role} · {model}]"`.
       - Dynamically shows Role Pill + Provider Pill next to the selected session.
       - If the selected session has role `Code Reviewer`, displays an inline badge: `"🔍 Emits APPROVED / CHANGES_REQUESTED verdict"`.
    4. **Approval Toggle (`checkbox`)**: `"Pause for my approval before passing on"`.
  - **Quick Template Button**: `"+ Add Stage"` button, plus a convenience `"Auto-Fill 4-Stage Pipeline"` button (automatically creates 4 stages bound to Designer, Planner, Implementer, and Code Reviewer sessions if available).
  - **Save Pipeline Button**: Calls `POST /api/pipelines` or `PUT /api/pipelines/{id}`.
- **Task Launch Card (Bottom of Panel 1)**:
  - `textarea#pipeline-task-input`: `"Enter the single task for Stage 1..."`
  - Primary button: `"▶ Run Pipeline"` (validates and calls `POST /api/pipelines/{id}/runs`, then focuses Panel 2).

#### Panel 2 — Active Pipeline Run View (Live Per-Stage Timeline)
Displays the currently running or most recently launched `PipelineRun`:

1. **Run Summary Header**:
   - Pipeline Name, Run Status Badge (`RUNNING`, `WAITING FOR APPROVAL`, `COMPLETED`, `FAILED`, `STOPPED`), **Review Loop Counter Badge** (`Review Loop: 1 / 2`), Total Elapsed Timer, and a red `"■ Stop Pipeline"` button (visible while `status` is `running` or `waiting_approval`).
   - Collapsible `"Original Task Prompt"` box.

2. **Sticky Approval Banner (Visible ONLY when `run.status === 'waiting_approval'`)**:
   - Highlighted amber card (`--status-warning-bg` / `--status-warning-fg`):
     - Header: `"⏸ Paused for Approval after Stage: {stage_name} ({session_name})"`
     - Optional input: `"Add an optional note/instruction for the next stage..."`
     - Actions: `"✓ Approve & Continue"` (calls `POST /api/pipeline-runs/{id}/approve`) and `"■ Stop Pipeline"` (calls `POST /api/pipeline-runs/{id}/stop`).

3. **Live Per-Stage Timeline (`#stage-runs-timeline`)**:
   - Renders every `StageRun` record in execution order (including rework loop iterations, which are clearly badged as `Rework Loop #1 · Implementer` and `Re-Review #1 · Code Reviewer`).
   - Each **Timeline Stage Node (`.stage-run-card`)** shows:
     - **Header**: Status dot + Stage Order & Name + Bound Session (`Name`, Provider Pill, Role Pill, Model) + Iteration Badge (if `iteration > 0`) + Verdict Badge (`APPROVED` in green or `CHANGES_REQUESTED` in purple/amber) + Duration (`18.4s`).
     - **Body (Collapsible Tabs inside the Stage Card)**:
       - **Tab 1: `Live Output / Reply` (Default open for running & latest stage)**:
         - Shows live streaming tool calls (`.tool-card`) and streaming assistant text (`text_delta`) in real time!
         - If Reviewer returned `CHANGES_REQUESTED`, highlights the extracted **Findings** block at the bottom of the card.
       - **Tab 2: `Handoff Input Prompt`**:
         - Shows the exact formatted prompt (`input_prompt`) that the orchestrator sent into this stage.

---

### 4.5 View 3: Run History Browser (`frontend/js/runs.js`)

Activated via the `"Run History"` top tab. Persisted runs and turns can be reopened at any time, including across server restarts.

- **Left Column (`340px`) — Filterable History List**:
  - Sub-filter tabs: `"Pipeline Runs"` | `"All Session Turns"`.
  - **Pipeline Runs List**:
    - Each card shows Pipeline Name, Status Badge (`COMPLETED`, `FAILED`, `STOPPED`, `WAITING_APPROVAL`), Task snippet (first 80 chars), Loop count (`1 rework loop`), started timestamp, and total duration.
  - **All Session Turns List**:
    - Each card shows Session Name, Provider & Role badges, `Turn #N`, prompt snippet, status, duration, and timestamp.
- **Right Column (`1fr`) — Historical Run / Turn Inspector**:
  - Selecting a Pipeline Run renders the full **Per-Stage Timeline** (identical component to Panel 2 of the Pipelines view), allowing the user to expand any stage's Handoff Prompt, full Tool Call log (loaded on demand via `GET /api/turns/{turn_id}`), final reply, Reviewer verdict, and timings.
  - Selecting a standalone Session Turn renders its full prompt, persisted `NormalizedEvent` stream (thinking, tool inputs/outputs, text reply), and diagnostics.
  - Includes a `"Open Session Chat →"` button to jump straight to that session and continue chatting.

---

## 5. Summary of User Interaction Flows

| User Goal | Exact UI Action & Backend Call |
| :--- | :--- |
| **Create a session** | Click `"+ New Session"` -> pick Provider, Model, Role (auto-sets Permission), CWD -> `POST /api/sessions`. |
| **Chat multi-turn** | Select session in sidebar -> type prompt -> `"Send Turn"` -> `POST /api/sessions/{id}/turns` -> watch SSE `turn_event` stream live. |
| **Stop a turn** | Click `"■ Stop Turn"` in composer or `"■"` on sidebar card -> `POST /api/sessions/{id}/stop`. |
| **Override permissions** | Change the Permission dropdown in Session Header (`Read-Only` / `Edit Files` / `Full Access`) -> `PATCH /api/sessions/{id}`. |
| **Run a pipeline** | Open `Pipelines` tab -> bind stages to sessions, toggle per-stage approval, set max review loops -> enter task -> click `"▶ Run Pipeline"` -> `POST /api/pipelines/{id}/runs`. |
| **Approve a paused stage** | In the amber Approval Banner, optionally enter a note and click `"✓ Approve & Continue"` -> `POST /api/pipeline-runs/{id}/approve`. |
| **Inspect past runs** | Open `Run History` tab -> click any past pipeline run or turn -> inspect prompts, tool calls, verdicts, and timings. |
