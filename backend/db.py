from __future__ import annotations

import json
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

    def recover_orphaned_states(self) -> None:
        """Implements startup crash recovery per ARCHITECTURE.md §9."""
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
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ) -> Dict[str, Any]:
        """
        Atomically increments sessions.turn_count (turn_number = new turn_count),
        sets sessions.status = 'running', inserts turn with status='running', and returns turn dict.
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT turn_count FROM sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Session '{session_id}' not found")
            new_turn_number = row["turn_count"] + 1
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
                    started_at, finished_at, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
