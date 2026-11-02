from __future__ import annotations

from pathlib import Path
import sqlite3

from backend.db import Database

OLD_SCHEMA_DDL = """
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


def test_migration_from_pre_team_schema(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(OLD_SCHEMA_DDL)
    conn.execute(
        """
        INSERT INTO sessions (
            id, name, provider, model, role, custom_prompt,
            permission_level, working_dir, provider_session_id,
            status, turn_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sess-legacy-1",
            "Legacy Session",
            "claude",
            "sonnet",
            "planner",
            "",
            "readonly",
            "/tmp/legacy",
            None,
            "idle",
            1,
            "2026-11-01T10:00:00Z",
            "2026-11-01T10:00:00Z",
        ),
    )
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
            "turn-legacy-1",
            "sess-legacy-1",
            1,
            "Old prompt",
            "Old response",
            "done",
            None,
            0,
            0,
            "[]",
            None,
            None,
            "2026-11-01T10:00:01Z",
            "2026-11-01T10:00:05Z",
            4000,
        ),
    )
    conn.commit()
    conn.close()

    # Open with Database — must not raise, must run migrations
    db = Database(db_file)

    # 1. Existing row must be kept
    session = db.get_session("sess-legacy-1")
    assert session is not None
    assert session["name"] == "Legacy Session"
    assert session["team_run_id"] is None

    turn = db.get_turn("turn-legacy-1")
    assert turn is not None
    assert turn["prompt"] == "Old prompt"
    assert turn["team_run_id"] is None

    # 2. New columns must be added to sessions and turns
    check_conn = sqlite3.connect(str(db_file))
    check_conn.row_factory = sqlite3.Row
    s_cols = {r["name"] for r in check_conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "team_run_id" in s_cols
    t_cols = {r["name"] for r in check_conn.execute("PRAGMA table_info(turns)").fetchall()}
    assert "team_run_id" in t_cols

    # 3. New tables must exist
    tables = {r["name"] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"teams", "team_members", "team_runs", "team_run_sessions", "team_messages"}.issubset(tables)

    # 4. Migrated index must exist
    indexes = {r["name"] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_turns_team_run_id" in indexes
    check_conn.close()

    # 5. Default team should be seeded
    teams = db.list_teams()
    assert len(teams) >= 1


def test_migration_adds_new_team_run_columns_to_existing_team_db(tmp_path: Path) -> None:
    """A database created by an earlier team-mode build (team_runs without work_subdir /
    pending_prompt) must open cleanly, keep its rows, and gain both columns."""
    from backend.db import SCHEMA_DDL

    old_ddl = "\n".join(
        line
        for line in SCHEMA_DDL.splitlines()
        if not line.strip().startswith(("work_subdir ", "pending_prompt "))
    )
    assert "pending_prompt" not in old_ddl and "work_subdir" not in old_ddl

    db_file = tmp_path / "team_v1.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(old_ddl)
    conn.execute(
        """
        INSERT INTO team_runs (id, team_name, task_prompt, target_repo, worktree_path,
                               status, stage_state, started_at)
        VALUES ('run-old', 'T', 'Old task', '/tmp/repo', '/tmp/wt', 'waiting_user',
                'paused_limit', '2026-11-01T10:00:00Z')
        """
    )
    conn.commit()
    conn.close()

    db = Database(db_file)
    db.recover_orphaned_states()

    check_conn = sqlite3.connect(str(db_file))
    check_conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in check_conn.execute("PRAGMA table_info(team_runs)").fetchall()}
    check_conn.close()
    assert {"work_subdir", "pending_prompt"} <= cols

    run = db.get_team_run("run-old")
    assert run is not None
    assert run["task_prompt"] == "Old task"
    assert run["work_subdir"] == ""
    assert run["work_dir"] == "/tmp/wt"
    assert "pending_prompt" not in run
    assert db.get_team_run_pending_prompt("run-old") is None

    db.update_team_run("run-old", pending_prompt="# Next prompt")
    assert db.get_team_run_pending_prompt("run-old") == "# Next prompt"
    db.update_team_run("run-old", pending_prompt=None)
    assert db.get_team_run_pending_prompt("run-old") is None

    # Re-opening is idempotent (no duplicate ALTERs).
    Database(db_file)


def test_pre_team_schema_gets_new_team_run_columns(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy2.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(OLD_SCHEMA_DDL)
    conn.commit()
    conn.close()
    Database(db_file)
    check_conn = sqlite3.connect(str(db_file))
    cols = {r[1] for r in check_conn.execute("PRAGMA table_info(team_runs)").fetchall()}
    check_conn.close()
    assert {"work_subdir", "pending_prompt"} <= cols
