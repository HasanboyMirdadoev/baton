from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Union
import uuid

from backend.models import NotFoundError, utc_now_iso

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    custom_prompt TEXT NOT NULL DEFAULT '',
    permission_level TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    provider_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    turn_count INTEGER NOT NULL DEFAULT 0,
    team_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    response_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    error_message TEXT,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    failed_tool_count INTEGER NOT NULL DEFAULT 0,
    denied_actions_json TEXT NOT NULL DEFAULT '[]',
    pipeline_run_id TEXT,
    stage_run_id TEXT,
    team_run_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_turns_session_id ON turns(session_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_turns_pipeline_run_id ON turns(pipeline_run_id);

CREATE TABLE IF NOT EXISTS pipelines (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    max_review_loops INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_stages (
    id TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    stage_order INTEGER NOT NULL,
    name TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
    require_approval INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pipeline_stages_pipeline ON pipeline_stages(pipeline_id, stage_order);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    pipeline_name TEXT NOT NULL,
    task_prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    start_stage_index INTEGER NOT NULL DEFAULT 0,
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
    id TEXT PRIMARY KEY,
    pipeline_run_id TEXT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    stage_order INTEGER NOT NULL,
    stage_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    require_approval INTEGER NOT NULL DEFAULT 0,
    turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
    input_prompt TEXT NOT NULL DEFAULT '',
    output_text TEXT NOT NULL DEFAULT '',
    verdict TEXT,
    findings TEXT,
    approval_note TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_run_id ON pipeline_stage_runs(pipeline_run_id, started_at);

CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    max_rework_loops INTEGER NOT NULL DEFAULT 3,
    max_question_rounds INTEGER NOT NULL DEFAULT 4,
    max_total_turns INTEGER NOT NULL DEFAULT 30,
    turn_timeout_sec INTEGER NOT NULL DEFAULT 600,
    run_timeout_sec INTEGER NOT NULL DEFAULT 3600,
    auto_approve_mockup INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_members (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    permission_level TEXT NOT NULL,
    custom_prompt TEXT NOT NULL DEFAULT '',
    UNIQUE(team_id, role)
);
CREATE INDEX IF NOT EXISTS idx_team_members_team_id ON team_members(team_id);

CREATE TABLE IF NOT EXISTS team_runs (
    id TEXT PRIMARY KEY,
    team_id TEXT REFERENCES teams(id) ON DELETE SET NULL,
    team_name TEXT NOT NULL,
    task_prompt TEXT NOT NULL,
    target_repo TEXT NOT NULL,
    is_git_repo INTEGER NOT NULL DEFAULT 1,
    base_branch TEXT NOT NULL DEFAULT 'main',
    base_commit TEXT NOT NULL DEFAULT '',
    worktree_path TEXT NOT NULL DEFAULT '',
    work_subdir TEXT NOT NULL DEFAULT '',
    branch_name TEXT NOT NULL DEFAULT '',
    allocated_port INTEGER NOT NULL DEFAULT 18100,
    status TEXT NOT NULL DEFAULT 'pending',
    stage_state TEXT NOT NULL DEFAULT 'pm_triage',
    interrupted_stage_state TEXT,
    needs_ui_mockup INTEGER,
    auto_approve_mockup INTEGER NOT NULL DEFAULT 0,
    asking_role TEXT,
    pending_questions_json TEXT NOT NULL DEFAULT '[]',
    qa_history_json TEXT NOT NULL DEFAULT '[]',
    app_run_json TEXT NOT NULL DEFAULT '{}',
    rework_loop_count INTEGER NOT NULL DEFAULT 0,
    max_rework_loops INTEGER NOT NULL DEFAULT 3,
    question_round_count INTEGER NOT NULL DEFAULT 0,
    max_question_rounds INTEGER NOT NULL DEFAULT 4,
    turn_count INTEGER NOT NULL DEFAULT 0,
    max_total_turns INTEGER NOT NULL DEFAULT 30,
    turn_timeout_sec INTEGER NOT NULL DEFAULT 600,
    run_timeout_sec INTEGER NOT NULL DEFAULT 3600,
    active_elapsed_ms INTEGER NOT NULL DEFAULT 0,
    mockup_version INTEGER NOT NULL DEFAULT 0,
    mockup_approved INTEGER NOT NULL DEFAULT 0,
    ui_check_verdict TEXT,
    code_review_verdict TEXT,
    last_findings_json TEXT NOT NULL DEFAULT '[]',
    final_summary TEXT NOT NULL DEFAULT '',
    pending_prompt TEXT,
    resolution TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_team_runs_started_at ON team_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS team_run_sessions (
    team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    PRIMARY KEY (team_run_id, role)
);

CREATE TABLE IF NOT EXISTS team_messages (
    id TEXT PRIMARY KEY,
    team_run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    kind TEXT NOT NULL,
    stage_state TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content_text TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_messages_run_seq ON team_messages(team_run_id, seq ASC);
"""


def _turn_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    denied_json = d.pop("denied_actions_json", "[]")
    try:
        d["denied_actions"] = json.loads(denied_json) if denied_json else []
    except Exception:
        d["denied_actions"] = []
    return d


def _stage_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["require_approval"] = bool(d.get("require_approval", 0))
    return d


def _stage_run_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["require_approval"] = bool(d.get("require_approval", 0))
    return d


_UNSET = object()

DEFAULT_TEAM_MEMBERS: List[Dict[str, str]] = [
    {
        "role": "pm",
        "agent_name": "PM",
        "provider": "claude",
        "model": "sonnet",
        "permission_level": "readonly",
        "custom_prompt": "",
    },
    {
        "role": "ui_designer",
        "agent_name": "UI Designer",
        "provider": "antigravity",
        "model": "gemini-4-argon-high",
        "permission_level": "edit",
        "custom_prompt": "",
    },
    {
        "role": "architect",
        "agent_name": "Code Architect",
        "provider": "claude",
        "model": "opus",
        "permission_level": "readonly",
        "custom_prompt": "",
    },
    {
        "role": "planner",
        "agent_name": "Planner",
        "provider": "claude",
        "model": "sonnet",
        "permission_level": "readonly",
        "custom_prompt": "",
    },
    {
        "role": "implementer",
        "agent_name": "Implementer",
        "provider": "antigravity",
        "model": "gemini-4-argon-high",
        "permission_level": "full",
        "custom_prompt": "",
    },
    {
        "role": "ui_checker",
        "agent_name": "UI Checker",
        "provider": "antigravity",
        "model": "gemini-3.8-flash-medium",
        "permission_level": "full",
        "custom_prompt": "",
    },
    {
        "role": "reviewer",
        "agent_name": "Code Reviewer",
        "provider": "claude",
        "model": "sonnet",
        "permission_level": "readonly",
        "custom_prompt": "",
    },
]


def _team_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["is_default"] = bool(d.get("is_default", 0))
    d["auto_approve_mockup"] = bool(d.get("auto_approve_mockup", 0))
    return d


def _team_run_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    # The next stage prompt can be large; it is engine-internal and read via
    # Database.get_team_run_pending_prompt(), so keep it out of API/SSE payloads.
    d.pop("pending_prompt", None)
    work_subdir = str(d.get("work_subdir") or "").strip("/")
    d["work_subdir"] = work_subdir
    wt = str(d.get("worktree_path") or "")
    d["work_dir"] = os.path.join(wt, work_subdir) if (wt and work_subdir) else wt
    d["is_git_repo"] = bool(d.get("is_git_repo", 1))
    d["auto_approve_mockup"] = bool(d.get("auto_approve_mockup", 0))
    d["mockup_approved"] = bool(d.get("mockup_approved", 0))
    needs_ui = d.get("needs_ui_mockup")
    d["needs_ui_mockup"] = bool(needs_ui) if needs_ui is not None else None

    pending_json = d.pop("pending_questions_json", "[]")
    try:
        d["pending_questions"] = json.loads(pending_json) if pending_json else []
    except Exception:
        d["pending_questions"] = []

    qa_json = d.pop("qa_history_json", "[]")
    try:
        d["qa_history"] = json.loads(qa_json) if qa_json else []
    except Exception:
        d["qa_history"] = []

    app_run_json = d.pop("app_run_json", "{}")
    try:
        d["app_run"] = json.loads(app_run_json) if app_run_json else {}
    except Exception:
        d["app_run"] = {}

    findings_json = d.pop("last_findings_json", "[]")
    try:
        d["last_findings"] = json.loads(findings_json) if findings_json else []
    except Exception:
        d["last_findings"] = []

    return d


def _team_message_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    payload_json = d.pop("payload_json", "{}")
    try:
        d["payload"] = json.loads(payload_json) if payload_json else {}
    except Exception:
        d["payload"] = {}
    return d


class Database:
    def __init__(self, db_path: Union[Path, str]) -> None:
        """Stores db_path and calls self.init_db()."""
        self.db_path = Path(db_path) if isinstance(db_path, str) else db_path
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init_db(self) -> None:
        """Executes schema DDL from ARCHITECTURE.md §9 and runs additive column migrations."""
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.executescript(SCHEMA_DDL)

            cursor = conn.execute("PRAGMA table_info(pipeline_runs)")
            columns = {row["name"] for row in cursor.fetchall()}
            if "start_stage_index" not in columns:
                conn.execute(
                    "ALTER TABLE pipeline_runs ADD COLUMN start_stage_index INTEGER NOT NULL DEFAULT 0"
                )

            cursor = conn.execute("PRAGMA table_info(sessions)")
            s_columns = {row["name"] for row in cursor.fetchall()}
            if "team_run_id" not in s_columns:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN team_run_id TEXT"
                )

            cursor = conn.execute("PRAGMA table_info(turns)")
            t_columns = {row["name"] for row in cursor.fetchall()}
            if "team_run_id" not in t_columns:
                conn.execute(
                    "ALTER TABLE turns ADD COLUMN team_run_id TEXT"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_team_run_id ON turns(team_run_id);"
            )

            # team_runs may predate these columns (databases created by earlier team-mode builds).
            cursor = conn.execute("PRAGMA table_info(team_runs)")
            tr_columns = {row["name"] for row in cursor.fetchall()}
            if "work_subdir" not in tr_columns:
                conn.execute(
                    "ALTER TABLE team_runs ADD COLUMN work_subdir TEXT NOT NULL DEFAULT ''"
                )
            if "pending_prompt" not in tr_columns:
                conn.execute("ALTER TABLE team_runs ADD COLUMN pending_prompt TEXT")

            self._seed_default_team(conn)

    def _seed_default_team(self, conn: sqlite3.Connection) -> None:
        """Seeds the default 7-role engineering team if no teams exist."""
        cursor = conn.execute("SELECT COUNT(*) AS count FROM teams")
        row = cursor.fetchone()
        if row and row["count"] > 0:
            return

        team_id = str(uuid.uuid4())
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO teams (
                id, name, description, is_default,
                max_rework_loops, max_question_rounds, max_total_turns,
                turn_timeout_sec, run_timeout_sec, auto_approve_mockup,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                team_id,
                "Default Engineering Team",
                "Standard 7-agent engineering team (PM, UI Designer, Architect, Planner, Implementer, UI Checker, Code Reviewer)",
                1,
                3,
                4,
                30,
                600,
                3600,
                0,
                now,
                now,
            ),
        )

        for member in DEFAULT_TEAM_MEMBERS:
            conn.execute(
                """
                INSERT INTO team_members (
                    id, team_id, role, agent_name, provider, model, permission_level, custom_prompt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    team_id,
                    member["role"],
                    member["agent_name"],
                    member["provider"],
                    member["model"],
                    member["permission_level"],
                    member.get("custom_prompt", ""),
                ),
            )

    def recover_orphaned_states(self) -> None:
        """Implements startup crash recovery per ARCHITECTURE.md §9 and TEAM_MODE.md §6.3."""
        now = utc_now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE turns
                SET status = 'error',
                    error_message = 'Server restarted while turn was in progress',
                    finished_at = COALESCE(finished_at, ?)
                WHERE status = 'running'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE sessions
                SET status = 'idle',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE pipeline_runs
                SET status = 'failed',
                    error_message = 'Server restarted while pipeline was active',
                    finished_at = COALESCE(finished_at, ?)
                WHERE status IN ('running', 'waiting_approval')
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE pipeline_stage_runs
                SET status = 'failed',
                    error_message = 'Server restarted while pipeline was active',
                    finished_at = COALESCE(finished_at, ?)
                WHERE status IN ('running', 'waiting_approval')
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE team_runs
                SET status = 'waiting_user',
                    interrupted_stage_state = stage_state,
                    stage_state = 'interrupted',
                    error_message = 'Server restarted while team run was active. Click Resume to continue from this stage.'
                WHERE status IN ('pending', 'running')
                """
            )

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
        team_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        session_id = str(uuid.uuid4())
        now = utc_now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, name, provider, model, role,
                    custom_prompt, permission_level, working_dir,
                    provider_session_id, status, turn_count,
                    team_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    name,
                    provider,
                    model,
                    role,
                    custom_prompt,
                    permission_level,
                    working_dir,
                    None,
                    "idle",
                    0,
                    team_run_id,
                    now,
                    now,
                ),
            )
        session = self.get_session(session_id)
        assert session is not None
        return session

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM sessions ORDER BY created_at ASC")
            return [dict(r) for r in cursor.fetchall()]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(row)

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
        team_run_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        fields: List[str] = []
        params: List[Any] = []

        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if permission_level is not None:
            fields.append("permission_level = ?")
            params.append(permission_level)
        if model is not None:
            fields.append("model = ?")
            params.append(model)
        if custom_prompt is not None:
            fields.append("custom_prompt = ?")
            params.append(custom_prompt)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if provider_session_id is not None:
            fields.append("provider_session_id = ?")
            params.append(provider_session_id)
        if turn_count is not None:
            fields.append("turn_count = ?")
            params.append(turn_count)
        if team_run_id is not None:
            fields.append("team_run_id = ?")
            params.append(team_run_id)

        if not fields:
            return self.get_session(session_id)

        fields.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(session_id)

        sql = f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?"
        with self._get_connection() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                return None
        return self.get_session(session_id)

    def update_session_provider_id(self, session_id: str, provider_session_id: str) -> Optional[Dict[str, Any]]:
        return self.update_session(session_id, provider_session_id=provider_session_id)

    def is_session_bound_to_pipeline(self, session_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT 1 FROM pipeline_stages WHERE session_id = ? LIMIT 1", (session_id,))
            return cursor.fetchone() is not None

    def delete_session(self, session_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cursor.rowcount > 0

    # --- Turns ---

    def create_turn(
        self,
        session_id: str,
        prompt: str,
        pipeline_run_id: Optional[str] = None,
        stage_run_id: Optional[str] = None,
        team_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Atomically increments sessions.turn_count (turn_number = new turn_count),
        sets sessions.status = 'running', inserts turn with status='running', and returns turn dict.
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT turn_count, team_run_id FROM sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Session '{session_id}' not found")
            new_turn_number = row["turn_count"] + 1
            effective_team_run_id = (
                team_run_id if team_run_id is not None else row["team_run_id"]
            )
            now = utc_now_iso()
            conn.execute(
                "UPDATE sessions SET turn_count = ?, status = 'running', updated_at = ? WHERE id = ?",
                (new_turn_number, now, session_id),
            )
            turn_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO turns (
                    id, session_id, turn_number, prompt, response_text,
                    status, error_message, tool_call_count, failed_tool_count,
                    denied_actions_json, pipeline_run_id, stage_run_id,
                    team_run_id, started_at, finished_at, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    new_turn_number,
                    prompt,
                    "",
                    "running",
                    None,
                    0,
                    0,
                    "[]",
                    pipeline_run_id,
                    stage_run_id,
                    effective_team_run_id,
                    now,
                    None,
                    None,
                ),
            )
            if stage_run_id is not None:
                conn.execute(
                    "UPDATE pipeline_stage_runs SET turn_id = ? WHERE id = ?",
                    (turn_id, stage_run_id),
                )
        turn = self.get_turn(turn_id)
        assert turn is not None
        return turn

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
    ) -> Optional[Dict[str, Any]]:
        denied_json = json.dumps(denied_actions if denied_actions is not None else [])
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE turns
                SET status = ?,
                    response_text = ?,
                    error_message = ?,
                    tool_call_count = ?,
                    failed_tool_count = ?,
                    denied_actions_json = ?,
                    finished_at = ?,
                    duration_ms = ?
                WHERE id = ?
                """,
                (
                    status,
                    response_text,
                    error_message,
                    tool_call_count,
                    failed_tool_count,
                    denied_json,
                    finished_at,
                    duration_ms,
                    turn_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    t.*,
                    s.name AS session_name,
                    s.provider,
                    s.model,
                    s.role
                FROM turns t
                LEFT JOIN sessions s ON t.session_id = s.id
                WHERE t.id = ?
                """,
                (turn_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _turn_row_to_dict(row)

    def list_turns_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY turn_number ASC",
                (session_id,),
            )
            return [_turn_row_to_dict(row) for row in cursor.fetchall()]

    def list_all_turns(self, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Lists turns across all sessions ordered by started_at DESC, joined with
        session_name, provider, model, and role for the Run History 'All Session Turns' view.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    t.*,
                    s.name AS session_name,
                    s.provider,
                    s.model,
                    s.role
                FROM turns t
                JOIN sessions s ON t.session_id = s.id
                ORDER BY t.started_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [_turn_row_to_dict(row) for row in cursor.fetchall()]

    # --- Pipelines ---

    def create_pipeline(
        self,
        name: str,
        description: str,
        max_review_loops: int,
        stages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        pipeline_id = str(uuid.uuid4())
        now = utc_now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO pipelines (id, name, description, max_review_loops, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pipeline_id, name, description, max_review_loops, now, now),
            )
            for idx, stage in enumerate(stages):
                stage_id = str(uuid.uuid4())
                stage_order = idx
                stage_name = stage["name"]
                session_id = stage["session_id"]
                require_approval = 1 if stage.get("require_approval") else 0
                conn.execute(
                    """
                    INSERT INTO pipeline_stages (id, pipeline_id, stage_order, name, session_id, require_approval)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (stage_id, pipeline_id, stage_order, stage_name, session_id, require_approval),
                )
        pipeline = self.get_pipeline(pipeline_id)
        assert pipeline is not None
        return pipeline

    def update_pipeline(
        self,
        pipeline_id: str,
        name: str,
        description: str,
        max_review_loops: int,
        stages: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_iso()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE pipelines
                SET name = ?, description = ?, max_review_loops = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, description, max_review_loops, now, pipeline_id),
            )
            if cursor.rowcount == 0:
                return None
            conn.execute("DELETE FROM pipeline_stages WHERE pipeline_id = ?", (pipeline_id,))
            for idx, stage in enumerate(stages):
                stage_id = str(uuid.uuid4())
                stage_order = idx
                stage_name = stage["name"]
                session_id = stage["session_id"]
                require_approval = 1 if stage.get("require_approval") else 0
                conn.execute(
                    """
                    INSERT INTO pipeline_stages (id, pipeline_id, stage_order, name, session_id, require_approval)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (stage_id, pipeline_id, stage_order, stage_name, session_id, require_approval),
                )
        return self.get_pipeline(pipeline_id)

    def get_pipeline(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            pipeline = dict(row)
            stage_cursor = conn.execute(
                "SELECT * FROM pipeline_stages WHERE pipeline_id = ? ORDER BY stage_order ASC",
                (pipeline_id,),
            )
            pipeline["stages"] = [_stage_row_to_dict(r) for r in stage_cursor.fetchall()]
            return pipeline

    def list_pipelines(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM pipelines ORDER BY created_at ASC")
            pipelines = [dict(r) for r in cursor.fetchall()]
            for pipeline in pipelines:
                stage_cursor = conn.execute(
                    "SELECT * FROM pipeline_stages WHERE pipeline_id = ? ORDER BY stage_order ASC",
                    (pipeline["id"],),
                )
                pipeline["stages"] = [_stage_row_to_dict(r) for r in stage_cursor.fetchall()]
            return pipelines

    def has_active_pipeline_runs(self, pipeline_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT 1 FROM pipeline_runs
                WHERE pipeline_id = ? AND status IN ('running', 'waiting_approval', 'pending')
                LIMIT 1
                """,
                (pipeline_id,),
            )
            return cursor.fetchone() is not None

    def delete_pipeline(self, pipeline_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM pipelines WHERE id = ?", (pipeline_id,))
            return cursor.rowcount > 0

    # --- Pipeline Runs & Stage Runs ---

    def create_pipeline_run(
        self,
        pipeline_id: str,
        pipeline_name: str,
        task_prompt: str,
        max_review_loops: int,
        status: str = "running",
        start_stage_index: int = 0,
    ) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = utc_now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_runs (
                    id, pipeline_id, pipeline_name, task_prompt,
                    status, start_stage_index, current_stage_order, review_loop_count,
                    max_review_loops, error_message, started_at,
                    finished_at, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    pipeline_id,
                    pipeline_name,
                    task_prompt,
                    status,
                    start_stage_index,
                    start_stage_index,
                    0,
                    max_review_loops,
                    None,
                    now,
                    None,
                    None,
                ),
            )
        run = self.get_pipeline_run(run_id)
        assert run is not None
        return run

    def update_pipeline_run(
        self,
        run_id: str,
        status: Optional[str] = None,
        current_stage_order: Optional[int] = None,
        review_loop_count: Optional[int] = None,
        error_message: Optional[str] = None,
        finished_at: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        fields: List[str] = []
        params: List[Any] = []

        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if current_stage_order is not None:
            fields.append("current_stage_order = ?")
            params.append(current_stage_order)
        if review_loop_count is not None:
            fields.append("review_loop_count = ?")
            params.append(review_loop_count)
        if error_message is not None:
            fields.append("error_message = ?")
            params.append(error_message)
        if finished_at is not None:
            fields.append("finished_at = ?")
            params.append(finished_at)
        if duration_ms is not None:
            fields.append("duration_ms = ?")
            params.append(duration_ms)

        if not fields:
            return self.get_pipeline_run(run_id)

        params.append(run_id)
        sql = f"UPDATE pipeline_runs SET {', '.join(fields)} WHERE id = ?"
        with self._get_connection() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                return None
        return self.get_pipeline_run(run_id)

    def get_pipeline_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    def list_pipeline_runs(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM pipeline_runs ORDER BY started_at DESC")
            return [dict(r) for r in cursor.fetchall()]

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
    ) -> Dict[str, Any]:
        stage_run_id = str(uuid.uuid4())
        start = started_at or utc_now_iso()
        req_appr_int = 1 if require_approval else 0
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_stage_runs (
                    id, pipeline_run_id, stage_id, stage_order, stage_name,
                    session_id, session_name, provider, model, role,
                    iteration, require_approval, turn_id, input_prompt, output_text,
                    verdict, findings, approval_note, status, error_message,
                    started_at, finished_at, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage_run_id,
                    pipeline_run_id,
                    stage_id,
                    stage_order,
                    stage_name,
                    session_id,
                    session_name,
                    provider,
                    model,
                    role,
                    iteration,
                    req_appr_int,
                    None,
                    input_prompt,
                    "",
                    None,
                    None,
                    None,
                    status,
                    None,
                    start,
                    None,
                    None,
                ),
            )
        stage_run = self.get_stage_run(stage_run_id)
        assert stage_run is not None
        return stage_run

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
    ) -> Optional[Dict[str, Any]]:
        fields: List[str] = []
        params: List[Any] = []

        if turn_id is not None:
            fields.append("turn_id = ?")
            params.append(turn_id)
        if output_text is not None:
            fields.append("output_text = ?")
            params.append(output_text)
        if verdict is not None:
            fields.append("verdict = ?")
            params.append(verdict)
        if findings is not None:
            fields.append("findings = ?")
            params.append(findings)
        if approval_note is not None:
            fields.append("approval_note = ?")
            params.append(approval_note)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if error_message is not None:
            fields.append("error_message = ?")
            params.append(error_message)
        if started_at is not None:
            fields.append("started_at = ?")
            params.append(started_at)
        if finished_at is not None:
            fields.append("finished_at = ?")
            params.append(finished_at)
        if duration_ms is not None:
            fields.append("duration_ms = ?")
            params.append(duration_ms)

        if not fields:
            return self.get_stage_run(stage_run_id)

        params.append(stage_run_id)
        sql = f"UPDATE pipeline_stage_runs SET {', '.join(fields)} WHERE id = ?"
        with self._get_connection() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                return None
        return self.get_stage_run(stage_run_id)

    def get_stage_run(self, stage_run_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM pipeline_stage_runs WHERE id = ?", (stage_run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return _stage_run_row_to_dict(row)

    def list_stage_runs(self, pipeline_run_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM pipeline_stage_runs
                WHERE pipeline_run_id = ?
                ORDER BY started_at ASC, rowid ASC
                """,
                (pipeline_run_id,),
            )
            return [_stage_run_row_to_dict(row) for row in cursor.fetchall()]

    # --- Teams ---

    def create_team(
        self,
        name: str,
        description: str = "",
        is_default: bool = False,
        max_rework_loops: int = 3,
        max_question_rounds: int = 4,
        max_total_turns: int = 30,
        turn_timeout_sec: int = 600,
        run_timeout_sec: int = 3600,
        auto_approve_mockup: bool = False,
        members: Optional[List[Dict[str, Any]]] = None,
        team_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = team_id or str(uuid.uuid4())
        now = utc_now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO teams (
                    id, name, description, is_default,
                    max_rework_loops, max_question_rounds, max_total_turns,
                    turn_timeout_sec, run_timeout_sec, auto_approve_mockup,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tid,
                    name,
                    description,
                    1 if is_default else 0,
                    max_rework_loops,
                    max_question_rounds,
                    max_total_turns,
                    turn_timeout_sec,
                    run_timeout_sec,
                    1 if auto_approve_mockup else 0,
                    now,
                    now,
                ),
            )
            if members:
                for m in members:
                    conn.execute(
                        """
                        INSERT INTO team_members (
                            id, team_id, role, agent_name, provider, model, permission_level, custom_prompt
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            m.get("id") or str(uuid.uuid4()),
                            tid,
                            m["role"],
                            m.get("agent_name", m["role"]),
                            m["provider"],
                            m["model"],
                            m.get("permission_level", "readonly"),
                            m.get("custom_prompt", ""),
                        ),
                    )
        team = self.get_team(tid)
        assert team is not None
        return team

    def get_team(self, team_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            team = _team_row_to_dict(row)
            m_cursor = conn.execute(
                "SELECT * FROM team_members WHERE team_id = ? ORDER BY rowid ASC",
                (team_id,),
            )
            team["members"] = [dict(m) for m in m_cursor.fetchall()]
            return team

    def get_default_team(self) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT id FROM teams WHERE is_default = 1 LIMIT 1")
            row = cursor.fetchone()
            if row is None:
                cursor = conn.execute("SELECT id FROM teams ORDER BY created_at ASC LIMIT 1")
                row = cursor.fetchone()
            if row is None:
                return None
            return self.get_team(row["id"])

    def list_teams(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM teams ORDER BY is_default DESC, created_at ASC"
            )
            teams = [_team_row_to_dict(r) for r in cursor.fetchall()]
            for team in teams:
                m_cursor = conn.execute(
                    "SELECT * FROM team_members WHERE team_id = ? ORDER BY rowid ASC",
                    (team["id"],),
                )
                team["members"] = [dict(m) for m in m_cursor.fetchall()]
            return teams

    def update_team(
        self,
        team_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        is_default: Optional[bool] = None,
        max_rework_loops: Optional[int] = None,
        max_question_rounds: Optional[int] = None,
        max_total_turns: Optional[int] = None,
        turn_timeout_sec: Optional[int] = None,
        run_timeout_sec: Optional[int] = None,
        auto_approve_mockup: Optional[bool] = None,
        members: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        fields: List[str] = []
        params: List[Any] = []

        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if description is not None:
            fields.append("description = ?")
            params.append(description)
        if is_default is not None:
            fields.append("is_default = ?")
            params.append(1 if is_default else 0)
        if max_rework_loops is not None:
            fields.append("max_rework_loops = ?")
            params.append(max_rework_loops)
        if max_question_rounds is not None:
            fields.append("max_question_rounds = ?")
            params.append(max_question_rounds)
        if max_total_turns is not None:
            fields.append("max_total_turns = ?")
            params.append(max_total_turns)
        if turn_timeout_sec is not None:
            fields.append("turn_timeout_sec = ?")
            params.append(turn_timeout_sec)
        if run_timeout_sec is not None:
            fields.append("run_timeout_sec = ?")
            params.append(run_timeout_sec)
        if auto_approve_mockup is not None:
            fields.append("auto_approve_mockup = ?")
            params.append(1 if auto_approve_mockup else 0)

        now = utc_now_iso()
        with self._get_connection() as conn:
            if fields:
                fields.append("updated_at = ?")
                params.append(now)
                params.append(team_id)
                cursor = conn.execute(
                    f"UPDATE teams SET {', '.join(fields)} WHERE id = ?", params
                )
                if cursor.rowcount == 0:
                    return None
            else:
                cursor = conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,))
                if cursor.fetchone() is None:
                    return None

            if members is not None:
                conn.execute("DELETE FROM team_members WHERE team_id = ?", (team_id,))
                for m in members:
                    conn.execute(
                        """
                        INSERT INTO team_members (
                            id, team_id, role, agent_name, provider, model, permission_level, custom_prompt
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            m.get("id") or str(uuid.uuid4()),
                            team_id,
                            m["role"],
                            m.get("agent_name", m["role"]),
                            m["provider"],
                            m["model"],
                            m.get("permission_level", "readonly"),
                            m.get("custom_prompt", ""),
                        ),
                    )
                conn.execute(
                    "UPDATE teams SET updated_at = ? WHERE id = ?", (now, team_id)
                )

        return self.get_team(team_id)

    def delete_team(self, team_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
            return cursor.rowcount > 0

    def has_active_team_runs(self, team_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT 1 FROM team_runs
                WHERE team_id = ? AND status IN ('pending', 'running', 'waiting_user')
                LIMIT 1
                """,
                (team_id,),
            )
            return cursor.fetchone() is not None

    def count_teams(self) -> int:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) AS count FROM teams")
            row = cursor.fetchone()
            return int(row["count"]) if row else 0

    # --- Team Runs ---

    def create_team_run(
        self,
        task_prompt: str,
        target_repo: str,
        team_id: Optional[str] = None,
        team_name: str = "",
        is_git_repo: bool = True,
        base_branch: str = "main",
        base_commit: str = "",
        worktree_path: str = "",
        branch_name: str = "",
        allocated_port: int = 18100,
        status: str = "pending",
        work_subdir: str = "",
        stage_state: str = "pm_triage",
        interrupted_stage_state: Optional[str] = None,
        needs_ui_mockup: Optional[bool] = None,
        auto_approve_mockup: bool = False,
        asking_role: Optional[str] = None,
        pending_questions: Optional[List[str]] = None,
        qa_history: Optional[List[Dict[str, Any]]] = None,
        app_run: Optional[Dict[str, Any]] = None,
        rework_loop_count: int = 0,
        max_rework_loops: int = 3,
        question_round_count: int = 0,
        max_question_rounds: int = 4,
        turn_count: int = 0,
        max_total_turns: int = 30,
        turn_timeout_sec: int = 600,
        run_timeout_sec: int = 3600,
        active_elapsed_ms: int = 0,
        mockup_version: int = 0,
        mockup_approved: bool = False,
        ui_check_verdict: Optional[str] = None,
        code_review_verdict: Optional[str] = None,
        last_findings: Optional[List[str]] = None,
        final_summary: str = "",
        resolution: Optional[str] = None,
        error_message: Optional[str] = None,
        started_at: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        rid = run_id or str(uuid.uuid4())
        start = started_at or utc_now_iso()
        p_q_json = json.dumps(pending_questions if pending_questions is not None else [])
        qa_json = json.dumps(qa_history if qa_history is not None else [])
        app_json = json.dumps(app_run if app_run is not None else {})
        findings_json = json.dumps(last_findings if last_findings is not None else [])

        resolved_team_name = team_name
        if not resolved_team_name and team_id:
            team = self.get_team(team_id)
            if team:
                resolved_team_name = team["name"]
        if not resolved_team_name:
            resolved_team_name = "Default Engineering Team"

        needs_ui_val: Optional[int] = None
        if needs_ui_mockup is not None:
            needs_ui_val = 1 if needs_ui_mockup else 0

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO team_runs (
                    id, team_id, team_name, task_prompt, target_repo,
                    is_git_repo, base_branch, base_commit, worktree_path, branch_name,
                    allocated_port, status, stage_state, interrupted_stage_state,
                    needs_ui_mockup, auto_approve_mockup, asking_role,
                    pending_questions_json, qa_history_json, app_run_json,
                    rework_loop_count, max_rework_loops, question_round_count, max_question_rounds,
                    turn_count, max_total_turns, turn_timeout_sec, run_timeout_sec,
                    active_elapsed_ms, mockup_version, mockup_approved,
                    ui_check_verdict, code_review_verdict, last_findings_json,
                    final_summary, resolution, error_message, started_at,
                    finished_at, duration_ms
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    rid,
                    team_id,
                    resolved_team_name,
                    task_prompt,
                    target_repo,
                    1 if is_git_repo else 0,
                    base_branch,
                    base_commit,
                    worktree_path,
                    branch_name,
                    allocated_port,
                    status,
                    stage_state,
                    interrupted_stage_state,
                    needs_ui_val,
                    1 if auto_approve_mockup else 0,
                    asking_role,
                    p_q_json,
                    qa_json,
                    app_json,
                    rework_loop_count,
                    max_rework_loops,
                    question_round_count,
                    max_question_rounds,
                    turn_count,
                    max_total_turns,
                    turn_timeout_sec,
                    run_timeout_sec,
                    active_elapsed_ms,
                    mockup_version,
                    1 if mockup_approved else 0,
                    ui_check_verdict,
                    code_review_verdict,
                    findings_json,
                    final_summary,
                    resolution,
                    error_message,
                    start,
                    None,
                    None,
                ),
            )
        if work_subdir:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE team_runs SET work_subdir = ? WHERE id = ?",
                    (work_subdir.strip("/"), rid),
                )
        run = self.get_team_run(rid)
        assert run is not None
        return run

    def get_team_run_pending_prompt(self, run_id: str) -> Optional[str]:
        """Returns the persisted prompt for the run's next stage turn (None if none)."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT pending_prompt FROM team_runs WHERE id = ?", (run_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            value = row["pending_prompt"]
            return str(value) if value else None

    def get_team_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM team_runs WHERE id = ?", (run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return _team_run_row_to_dict(row)

    def update_team_run(
        self,
        run_id: str,
        team_id: Union[Optional[str], object] = _UNSET,
        team_name: Optional[str] = None,
        task_prompt: Optional[str] = None,
        target_repo: Optional[str] = None,
        is_git_repo: Optional[bool] = None,
        base_branch: Optional[str] = None,
        base_commit: Optional[str] = None,
        worktree_path: Optional[str] = None,
        branch_name: Optional[str] = None,
        allocated_port: Optional[int] = None,
        status: Optional[str] = None,
        stage_state: Optional[str] = None,
        interrupted_stage_state: Union[Optional[str], object] = _UNSET,
        needs_ui_mockup: Union[Optional[bool], object] = _UNSET,
        auto_approve_mockup: Optional[bool] = None,
        asking_role: Union[Optional[str], object] = _UNSET,
        pending_questions: Optional[List[str]] = None,
        qa_history: Optional[List[Dict[str, Any]]] = None,
        app_run: Optional[Dict[str, Any]] = None,
        rework_loop_count: Optional[int] = None,
        max_rework_loops: Optional[int] = None,
        question_round_count: Optional[int] = None,
        max_question_rounds: Optional[int] = None,
        turn_count: Optional[int] = None,
        max_total_turns: Optional[int] = None,
        turn_timeout_sec: Optional[int] = None,
        run_timeout_sec: Optional[int] = None,
        active_elapsed_ms: Optional[int] = None,
        mockup_version: Optional[int] = None,
        mockup_approved: Optional[bool] = None,
        ui_check_verdict: Union[Optional[str], object] = _UNSET,
        code_review_verdict: Union[Optional[str], object] = _UNSET,
        last_findings: Optional[List[str]] = None,
        final_summary: Optional[str] = None,
        resolution: Union[Optional[str], object] = _UNSET,
        error_message: Union[Optional[str], object] = _UNSET,
        finished_at: Union[Optional[str], object] = _UNSET,
        duration_ms: Union[Optional[int], object] = _UNSET,
        pending_prompt: Union[Optional[str], object] = _UNSET,
    ) -> Optional[Dict[str, Any]]:
        fields: List[str] = []
        params: List[Any] = []

        if team_id is not _UNSET:
            fields.append("team_id = ?")
            params.append(team_id)
        if team_name is not None:
            fields.append("team_name = ?")
            params.append(team_name)
        if task_prompt is not None:
            fields.append("task_prompt = ?")
            params.append(task_prompt)
        if target_repo is not None:
            fields.append("target_repo = ?")
            params.append(target_repo)
        if is_git_repo is not None:
            fields.append("is_git_repo = ?")
            params.append(1 if is_git_repo else 0)
        if base_branch is not None:
            fields.append("base_branch = ?")
            params.append(base_branch)
        if base_commit is not None:
            fields.append("base_commit = ?")
            params.append(base_commit)
        if worktree_path is not None:
            fields.append("worktree_path = ?")
            params.append(worktree_path)
        if branch_name is not None:
            fields.append("branch_name = ?")
            params.append(branch_name)
        if allocated_port is not None:
            fields.append("allocated_port = ?")
            params.append(allocated_port)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if stage_state is not None:
            fields.append("stage_state = ?")
            params.append(stage_state)
        if interrupted_stage_state is not _UNSET:
            fields.append("interrupted_stage_state = ?")
            params.append(interrupted_stage_state)
        if needs_ui_mockup is not _UNSET:
            fields.append("needs_ui_mockup = ?")
            if needs_ui_mockup is True:
                params.append(1)
            elif needs_ui_mockup is False:
                params.append(0)
            else:
                params.append(None)
        if auto_approve_mockup is not None:
            fields.append("auto_approve_mockup = ?")
            params.append(1 if auto_approve_mockup else 0)
        if asking_role is not _UNSET:
            fields.append("asking_role = ?")
            params.append(asking_role)
        if pending_questions is not None:
            fields.append("pending_questions_json = ?")
            params.append(json.dumps(pending_questions))
        if qa_history is not None:
            fields.append("qa_history_json = ?")
            params.append(json.dumps(qa_history))
        if app_run is not None:
            fields.append("app_run_json = ?")
            params.append(json.dumps(app_run))
        if rework_loop_count is not None:
            fields.append("rework_loop_count = ?")
            params.append(rework_loop_count)
        if max_rework_loops is not None:
            fields.append("max_rework_loops = ?")
            params.append(max_rework_loops)
        if question_round_count is not None:
            fields.append("question_round_count = ?")
            params.append(question_round_count)
        if max_question_rounds is not None:
            fields.append("max_question_rounds = ?")
            params.append(max_question_rounds)
        if turn_count is not None:
            fields.append("turn_count = ?")
            params.append(turn_count)
        if max_total_turns is not None:
            fields.append("max_total_turns = ?")
            params.append(max_total_turns)
        if turn_timeout_sec is not None:
            fields.append("turn_timeout_sec = ?")
            params.append(turn_timeout_sec)
        if run_timeout_sec is not None:
            fields.append("run_timeout_sec = ?")
            params.append(run_timeout_sec)
        if active_elapsed_ms is not None:
            fields.append("active_elapsed_ms = ?")
            params.append(active_elapsed_ms)
        if mockup_version is not None:
            fields.append("mockup_version = ?")
            params.append(mockup_version)
        if mockup_approved is not None:
            fields.append("mockup_approved = ?")
            params.append(1 if mockup_approved else 0)
        if ui_check_verdict is not _UNSET:
            fields.append("ui_check_verdict = ?")
            params.append(ui_check_verdict)
        if code_review_verdict is not _UNSET:
            fields.append("code_review_verdict = ?")
            params.append(code_review_verdict)
        if last_findings is not None:
            fields.append("last_findings_json = ?")
            params.append(json.dumps(last_findings))
        if final_summary is not None:
            fields.append("final_summary = ?")
            params.append(final_summary)
        if resolution is not _UNSET:
            fields.append("resolution = ?")
            params.append(resolution)
        if error_message is not _UNSET:
            fields.append("error_message = ?")
            params.append(error_message)
        if finished_at is not _UNSET:
            fields.append("finished_at = ?")
            params.append(finished_at)
        if duration_ms is not _UNSET:
            fields.append("duration_ms = ?")
            params.append(duration_ms)
        if pending_prompt is not _UNSET:
            fields.append("pending_prompt = ?")
            params.append(pending_prompt)

        if not fields:
            return self.get_team_run(run_id)

        params.append(run_id)
        sql = f"UPDATE team_runs SET {', '.join(fields)} WHERE id = ?"
        with self._get_connection() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                return None
        return self.get_team_run(run_id)

    def list_team_runs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if limit is not None:
                cursor = conn.execute(
                    "SELECT * FROM team_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM team_runs ORDER BY started_at DESC"
                )
            return [_team_run_row_to_dict(r) for r in cursor.fetchall()]

    def delete_team_run(self, run_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM team_runs WHERE id = ?", (run_id,))
            return cursor.rowcount > 0

    def bind_team_run_session(self, team_run_id: str, role: str, session_id: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO team_run_sessions (team_run_id, role, session_id)
                VALUES (?, ?, ?)
                ON CONFLICT(team_run_id, role) DO UPDATE SET session_id = excluded.session_id
                """,
                (team_run_id, role, session_id),
            )
            conn.execute(
                "UPDATE sessions SET team_run_id = ? WHERE id = ?",
                (team_run_id, session_id),
            )

    def get_team_run_sessions(self, team_run_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT trs.role, trs.session_id, s.*
                FROM team_run_sessions trs
                JOIN sessions s ON trs.session_id = s.id
                WHERE trs.team_run_id = ?
                ORDER BY trs.rowid ASC
                """,
                (team_run_id,),
            )
            rows = cursor.fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                s_dict = dict(row)
                role = s_dict["role"]
                sess_id = s_dict["session_id"]
                results.append({
                    "role": role,
                    "session_id": sess_id,
                    "session": s_dict,
                })
            return results

    def get_team_run_session_id(self, team_run_id: str, role: str) -> Optional[str]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT session_id FROM team_run_sessions WHERE team_run_id = ? AND role = ?",
                (team_run_id, role),
            )
            row = cursor.fetchone()
            return row["session_id"] if row else None

    # --- Team Messages ---

    def create_team_message(
        self,
        team_run_id: str,
        from_role: str,
        to_role: str,
        kind: str,
        stage_state: str,
        summary: str = "",
        content_text: str = "",
        payload: Optional[Dict[str, Any]] = None,
        turn_id: Optional[str] = None,
        seq: Optional[int] = None,
        created_at: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        mid = message_id or str(uuid.uuid4())
        now = created_at or utc_now_iso()
        p_json = json.dumps(payload if payload is not None else {})
        with self._get_connection() as conn:
            assigned_seq = seq
            if assigned_seq is None:
                cur = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM team_messages WHERE team_run_id = ?",
                    (team_run_id,),
                )
                assigned_seq = cur.fetchone()["next_seq"]

            conn.execute(
                """
                INSERT INTO team_messages (
                    id, team_run_id, seq, turn_id, from_role, to_role,
                    kind, stage_state, summary, content_text, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid,
                    team_run_id,
                    assigned_seq,
                    turn_id,
                    from_role,
                    to_role,
                    kind,
                    stage_state,
                    summary,
                    content_text,
                    p_json,
                    now,
                ),
            )
        msg = self.get_team_message(mid)
        assert msg is not None
        return msg

    def get_team_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM team_messages WHERE id = ?", (message_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _team_message_row_to_dict(row)

    def get_team_message_by_seq(self, team_run_id: str, seq: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM team_messages WHERE team_run_id = ? AND seq = ?",
                (team_run_id, seq),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _team_message_row_to_dict(row)

    def list_team_messages(self, team_run_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM team_messages WHERE team_run_id = ? ORDER BY seq ASC",
                (team_run_id,),
            )
            return [_team_message_row_to_dict(r) for r in cursor.fetchall()]

    def list_turns_for_team_run(self, team_run_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    t.*,
                    s.name AS session_name,
                    s.provider,
                    s.model,
                    s.role
                FROM turns t
                LEFT JOIN sessions s ON t.session_id = s.id
                WHERE t.team_run_id = ?
                ORDER BY t.started_at ASC, t.rowid ASC
                """,
                (team_run_id,),
            )
            return [_turn_row_to_dict(row) for row in cursor.fetchall()]
