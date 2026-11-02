from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from fastapi.testclient import TestClient
import pytest

from backend.config import AppConfig, load_config
from backend.db import Database
from backend.main import create_app
from backend.models import (
    ConflictError,
    UnprocessableError,
    ValidationError,
)
from backend.pubsub import EventBus
from backend.session_manager import SessionManager
from backend.team_engine import TeamEngine
from backend.team_protocol import (
    build_format_repair_prompt,
    parse_team_message,
)
from backend.worktree_manager import (
    FORBIDDEN_PORTS,
    allocate_safe_port,
    finalize_workspace,
    get_workspace_diff,
    prepare_workspace,
)
from tests.conftest import FIXTURES_DIR, git_cmd

WAIT_TIMEOUT_SEC = 40.0

ROLES = [
    "pm",
    "ui_designer",
    "architect",
    "planner",
    "implementer",
    "ui_checker",
    "reviewer",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block(payload: str) -> str:
    return f"```baton-msg\n{payload}\n```"


def _poll(predicate: Callable[[], Any], timeout_sec: float = WAIT_TIMEOUT_SEC, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout_sec
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.03)
    raise AssertionError(f"{what} not met within {timeout_sec}s")


def _get_detail(client: TestClient, run_id: str) -> Dict[str, Any]:
    res = client.get(f"/api/team-runs/{run_id}")
    assert res.status_code == 200, res.text
    return res.json()


def _wait_run(
    client: TestClient,
    run_id: str,
    stage_state: Optional[str] = None,
    status: Optional[str] = None,
    timeout_sec: float = WAIT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """Polls until the run matches `stage_state` and/or `status`; returns the full detail."""
    box: Dict[str, Any] = {}

    def _check() -> bool:
        detail = _get_detail(client, run_id)
        box["detail"] = detail
        run = detail["run"]
        matches = (stage_state is None or run["stage_state"] == stage_state) and (
            status is None or run["status"] == status
        )
        if matches:
            return True
        if run["status"] in ("failed", "stopped", "completed"):
            # Terminal state that is not the one we are waiting for: fail fast.
            raise AssertionError(
                f"Run {run_id} ended in status={run['status']} stage_state={run['stage_state']} "
                f"(wanted stage_state={stage_state} status={status}); error={run.get('error_message')}"
            )
        return False

    try:
        _poll(_check, timeout_sec, f"run {stage_state or ''}/{status or ''}")
    except AssertionError:
        run = box.get("detail", {}).get("run", {})
        raise AssertionError(
            f"Run {run_id} did not reach stage_state={stage_state} status={status}; "
            f"current status={run.get('status')} stage_state={run.get('stage_state')} "
            f"error={run.get('error_message')}"
        )
    return box["detail"]


def _start_run(
    client: TestClient,
    repo: Path,
    prompt: str = "Add a feature",
    **extra: Any,
) -> str:
    body: Dict[str, Any] = {"task_prompt": prompt, "target_repo": str(repo)}
    body.update(extra)
    res = client.post("/api/team-runs", json=body)
    assert res.status_code == 202, res.text
    return res.json()["run"]["id"]


def _msg_triples(detail: Dict[str, Any], include_user: bool = False) -> List[Tuple[str, str, str]]:
    return [
        (m["from_role"], m["to_role"], m["kind"])
        for m in detail["messages"]
        if include_user or m["kind"] != "user_action"
    ]


def _role_turns(db: Database, run_id: str, role: str) -> List[Dict[str, Any]]:
    return [t for t in db.list_turns_for_team_run(run_id) if t.get("role") == role]


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _post(client: TestClient, run_id: str, action: str, body: Optional[Dict[str, Any]] = None) -> Any:
    return client.post(f"/api/team-runs/{run_id}/{action}", json=body or {})


# Expected message flow of a default run (no UI Designer, no rework, no questions).
DEFAULT_FLOW = [
    ("pm", "architect", "deliverable"),
    ("architect", "planner", "deliverable"),
    ("planner", "implementer", "deliverable"),
    ("implementer", "checkers", "deliverable"),
    ("ui_checker", "pm", "approve"),
    ("reviewer", "pm", "approve"),
    ("pm", "user", "done"),
]


@pytest.fixture
def team_client(app_config: AppConfig) -> Iterator[TestClient]:
    app = create_app(app_config)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def team_db(app_config: AppConfig) -> Database:
    """Second connection to the app's SQLite file (for state inspection / simulated crashes)."""
    return Database(app_config.db_path)


# ---------------------------------------------------------------------------
# 1. parse_team_message
# ---------------------------------------------------------------------------


def test_parse_valid_pm_deliverable_strips_block_from_body() -> None:
    text = (
        "# Requirements\n\nBuild the thing.\n\n"
        + _block(
            '{"kind": "Deliverable", "to": " Architect ", "needs_ui_mockup": false, '
            '"summary": "Requirements ready"}'
        )
    )
    parsed = parse_team_message(text, sender_role="pm", stage_state="pm_triage")
    assert parsed.valid, parsed.error
    assert parsed.kind == "deliverable"
    assert parsed.to == "architect"
    assert parsed.needs_ui_mockup is False
    assert parsed.summary == "Requirements ready"
    assert parsed.body_markdown == "# Requirements\n\nBuild the thing."
    assert "baton-msg" not in parsed.body_markdown


def test_parse_valid_messages_for_each_kind() -> None:
    app_run = (
        '"app_run": {"requires_ui_check": true, "start_cmd": "python3 -m http.server {port}", '
        '"app_url": "http://127.0.0.1:{port}"}'
    )
    cases = [
        ("pm", "pm_triage", '{"kind":"question","to":"user","summary":"s","questions":["Q1?"]}'),
        ("pm", "pm_triage", '{"kind":"deliverable","to":"ui_designer","needs_ui_mockup":true,"summary":"s"}'),
        ("pm", "pm_mockup_review", '{"kind":"approval_request","to":"user","summary":"s"}'),
        ("pm", "pm_mockup_review", '{"kind":"reject","to":"ui_designer","summary":"s","findings":["Fix it"]}'),
        ("pm", "pm_answer_relay", '{"kind":"answer","to":"architect","summary":"s"}'),
        ("pm", "pm_final_summary", '{"kind":"done","to":"user","summary":"s"}'),
        ("ui_designer", "ui_design", '{"kind":"deliverable","to":"pm","summary":"s"}'),
        ("architect", "architecture", '{"kind":"question","to":"pm","summary":"s","questions":["Q?"]}'),
        ("architect", "architecture", '{"kind":"deliverable","to":"planner","summary":"s",' + app_run + "}"),
        ("planner", "planning", '{"kind":"deliverable","to":"implementer","summary":"s",' + app_run + "}"),
        ("implementer", "implementing", '{"kind":"deliverable","to":"checkers","summary":"s","files_changed":["a.py"]}'),
        ("ui_checker", "checking", '{"kind":"approve","to":"pm","summary":"s"}'),
        ("reviewer", "checking", '{"kind":"reject","to":"implementer","summary":"s","findings":["Bad"]}'),
    ]
    for role, stage, payload in cases:
        parsed = parse_team_message("Body\n\n" + _block(payload), sender_role=role, stage_state=stage)
        assert parsed.valid, f"{role}/{stage}: {parsed.error}"


def test_parse_populates_structured_fields() -> None:
    app_run_text = _block(
        '{"kind":"deliverable","to":"planner","summary":"arch done","app_run":'
        '{"start_cmd":"python3 -m http.server {port}","app_url":"http://127.0.0.1:{port}"}}'
    )
    parsed = parse_team_message(app_run_text, sender_role="architect", stage_state="architecture")
    assert parsed.valid, parsed.error
    assert parsed.app_run is not None
    assert parsed.app_run["start_cmd"] == "python3 -m http.server {port}"
    # Defaults are filled in by the AppRunContract.
    assert parsed.app_run["healthcheck_path"] == "/"
    assert parsed.app_run["stop_signal"] == "SIGTERM"
    assert parsed.app_run["requires_ui_check"] is True

    q = parse_team_message(
        _block('{"kind":"question","to":"user","summary":"s","questions":[" A? ","B?"]}'),
        sender_role="pm",
        stage_state="pm_triage",
    )
    assert q.questions == ["A?", "B?"]

    r = parse_team_message(
        _block('{"kind":"reject","to":"implementer","summary":"s","findings":["- one","- two"]}'),
        sender_role="reviewer",
        stage_state="checking",
    )
    assert r.findings == ["- one", "- two"]

    a = parse_team_message(
        _block('{"kind":"approval_request","to":"user","summary":"s"}'),
        sender_role="pm",
        stage_state="pm_mockup_review",
    )
    assert a.mockup_path == ".baton/mockup.html"


def test_parse_takes_last_block_and_json_fallback_fence() -> None:
    text = (
        _block('{"kind":"question","to":"user","summary":"old","questions":["x?"]}')
        + "\n\nActually, never mind.\n\n"
        + _block('{"kind":"deliverable","to":"architect","needs_ui_mockup":false,"summary":"new"}')
    )
    parsed = parse_team_message(text, sender_role="pm", stage_state="pm_triage")
    assert parsed.valid, parsed.error
    assert parsed.summary == "new"
    assert "Actually, never mind." in parsed.body_markdown

    fallback = (
        "Done.\n\n```json\n"
        '{"kind": "approve", "to": "pm", "summary": "fine"}\n'
        "```"
    )
    parsed_fb = parse_team_message(fallback, sender_role="reviewer", stage_state="checking")
    assert parsed_fb.valid, parsed_fb.error
    assert parsed_fb.kind == "approve"


@pytest.mark.parametrize("reply", ["", "Just prose with no block at all.", "```python\nprint('x')\n```"])
def test_parse_missing_block(reply: str) -> None:
    parsed = parse_team_message(reply, sender_role="pm", stage_state="pm_triage")
    assert not parsed.valid
    assert parsed.error and "baton-msg" in parsed.error


def test_parse_missing_block_keeps_body_for_repair() -> None:
    parsed = parse_team_message("  My long analysis.  ", sender_role="architect", stage_state="architecture")
    assert not parsed.valid
    assert parsed.body_markdown == "My long analysis."


@pytest.mark.parametrize(
    "payload",
    [
        '{"kind": "deliverable", "to": "architect", "summary": ',  # truncated
        "{kind: deliverable}",  # not JSON
        "[1, 2, 3]",  # JSON but not an object
        "",  # empty block
    ],
)
def test_parse_malformed_json(payload: str) -> None:
    parsed = parse_team_message(
        f"Body\n\n```baton-msg\n{payload}\n```", sender_role="pm", stage_state="pm_triage"
    )
    assert not parsed.valid
    assert parsed.error
    assert parsed.body_markdown == "Body"


@pytest.mark.parametrize(
    "role,stage,payload,error_fragment",
    [
        # Role cannot emit this kind at all.
        ("architect", "architecture", '{"kind":"done","to":"user","summary":"s"}', "cannot emit kind"),
        ("reviewer", "checking", '{"kind":"deliverable","to":"pm","summary":"s"}', "cannot emit kind"),
        ("implementer", "implementing", '{"kind":"approve","to":"pm","summary":"s"}', "cannot emit kind"),
        ("ui_designer", "ui_design", '{"kind":"approval_request","to":"user","summary":"s"}', "cannot emit kind"),
        # Kind is fine for the role but the target is not.
        ("reviewer", "checking", '{"kind":"approve","to":"user","summary":"s"}', "approve"),
        ("ui_checker", "checking", '{"kind":"reject","to":"pm","summary":"s","findings":["x"]}', "reject"),
        ("planner", "planning", '{"kind":"question","to":"user","summary":"s","questions":["q"]}', "questions"),
        ("pm", "pm_triage", '{"kind":"question","to":"architect","summary":"s","questions":["q"]}', "user"),
        # Role + kind + target allowed in general, but not in this stage.
        ("implementer", "pm_triage", '{"kind":"deliverable","to":"checkers","summary":"s"}', "not allowed in stage"),
        ("pm", "pm_triage", '{"kind":"done","to":"user","summary":"s"}', "not allowed in stage"),
        ("reviewer", "planning", '{"kind":"approve","to":"pm","summary":"s"}', "not allowed in stage"),
        # Unknown kind / unknown role.
        ("pm", "pm_triage", '{"kind":"shout","to":"user","summary":"s"}', "Unsupported kind"),
        ("intern", "pm_triage", '{"kind":"done","to":"user","summary":"s"}', "Unknown team role"),
    ],
)
def test_parse_wrong_kind_or_target_for_role(
    role: str, stage: str, payload: str, error_fragment: str
) -> None:
    parsed = parse_team_message(_block(payload), sender_role=role, stage_state=stage)
    assert not parsed.valid
    assert error_fragment.lower() in (parsed.error or "").lower()


@pytest.mark.parametrize(
    "role,stage,payload,error_fragment",
    [
        ("pm", "pm_triage", '{"to":"architect","summary":"s","needs_ui_mockup":false}', "kind"),
        ("pm", "pm_triage", '{"kind":"deliverable","summary":"s","needs_ui_mockup":false}', "'to'"),
        ("pm", "pm_triage", '{"kind":"deliverable","to":"architect","needs_ui_mockup":false}', "summary"),
        ("pm", "pm_triage", '{"kind":"deliverable","to":"architect","summary":"  "}', "summary"),
        ("pm", "pm_triage", '{"kind":"deliverable","to":"architect","summary":"s"}', "needs_ui_mockup"),
        ("pm", "pm_triage", '{"kind":"deliverable","to":"architect","summary":"s","needs_ui_mockup":"no"}', "needs_ui_mockup"),
        # needs_ui_mockup disagrees with the target.
        ("pm", "pm_triage", '{"kind":"deliverable","to":"architect","summary":"s","needs_ui_mockup":true}', "ui_designer"),
        ("pm", "pm_triage", '{"kind":"deliverable","to":"ui_designer","summary":"s","needs_ui_mockup":false}', "architect"),
        ("pm", "pm_triage", '{"kind":"question","to":"user","summary":"s"}', "questions"),
        ("pm", "pm_triage", '{"kind":"question","to":"user","summary":"s","questions":[]}', "questions"),
        ("pm", "pm_triage", '{"kind":"question","to":"user","summary":"s","questions":["", "  "]}', "questions"),
        ("pm", "pm_triage", '{"kind":"question","to":"user","summary":"s","questions":"one?"}', "questions"),
        ("reviewer", "checking", '{"kind":"reject","to":"implementer","summary":"s"}', "findings"),
        ("reviewer", "checking", '{"kind":"reject","to":"implementer","summary":"s","findings":[]}', "findings"),
        ("architect", "architecture", '{"kind":"deliverable","to":"planner","summary":"s"}', "app_run"),
        ("planner", "planning", '{"kind":"deliverable","to":"implementer","summary":"s","app_run":"npm start"}', "app_run"),
        (
            "architect",
            "architecture",
            '{"kind":"deliverable","to":"planner","summary":"s","app_run":{"app_url":"http://x"}}',
            "app_run",
        ),
        ("pm", "pm_answer_relay", '{"kind":"answer","to":"user","summary":"s"}', "answer"),
    ],
)
def test_parse_missing_or_invalid_required_fields(
    role: str, stage: str, payload: str, error_fragment: str
) -> None:
    parsed = parse_team_message(_block(payload), sender_role=role, stage_state=stage)
    assert not parsed.valid
    assert error_fragment.lower() in (parsed.error or "").lower()


def test_parse_answer_must_match_asking_role() -> None:
    payload = _block('{"kind":"answer","to":"planner","summary":"s"}')
    bad = parse_team_message(payload, sender_role="pm", stage_state="pm_answer_relay", asking_role="architect")
    assert not bad.valid
    assert "asking_role" in (bad.error or "")

    good = parse_team_message(payload, sender_role="pm", stage_state="pm_answer_relay", asking_role="planner")
    assert good.valid, good.error


def test_format_repair_prompt_mentions_error_and_stage() -> None:
    parsed = parse_team_message("no block", sender_role="architect", stage_state="architecture")
    prompt = build_format_repair_prompt(parsed, stage_state="architecture", role="architect")
    assert prompt.startswith("# Format Error")
    assert parsed.error in prompt
    assert "architecture" in prompt
    assert "```baton-msg" in prompt


# ---------------------------------------------------------------------------
# 2. Full team runs (fake CLIs, real git worktree)
# ---------------------------------------------------------------------------


def test_full_run_without_ui_designer_then_merge(
    team_client: TestClient, git_repo: Path, team_db: Database
) -> None:
    run_id = _start_run(team_client, git_repo, "Add feature_output.txt")
    detail = _wait_run(team_client, run_id, "waiting_final_signoff", "waiting_user")
    run = detail["run"]

    # Message flow: no UI Designer involved, checkers ran in the same pass.
    triples = _msg_triples(detail)
    assert triples[:4] == DEFAULT_FLOW[:4]
    assert sorted(triples[4:6]) == sorted(DEFAULT_FLOW[4:6])
    assert triples[6] == DEFAULT_FLOW[6]
    assert len(triples) == 7
    assert [m["seq"] for m in detail["messages"]] == list(range(1, 8))
    assert not any("ui_designer" in (m["from_role"], m["to_role"]) for m in detail["messages"])

    # Run bookkeeping.
    assert run["needs_ui_mockup"] is False
    assert run["mockup_version"] == 0
    assert run["rework_loop_count"] == 0
    assert run["ui_check_verdict"] == "APPROVED"
    assert run["code_review_verdict"] == "APPROVED"
    assert run["turn_count"] == 7
    assert run["final_summary"]
    assert run["app_run"]["start_cmd"]
    assert run["branch_name"] == f"baton/{run_id}"
    assert run["base_branch"] == "main"
    assert run["base_commit"] == git_cmd(git_repo, "rev-parse", "HEAD").strip()
    assert 18100 <= run["allocated_port"] <= 18999
    assert run["allocated_port"] not in FORBIDDEN_PORTS and run["allocated_port"] != 8765

    # 7 role sessions, all bound to the run's worktree.
    assert sorted(s["role"] for s in detail["sessions"]) == sorted(ROLES)
    for entry in detail["sessions"]:
        sess = team_db.get_session(entry["session"]["id"])
        assert sess is not None
        assert sess["team_run_id"] == run_id
        assert sess["working_dir"] == run["worktree_path"]
        assert sess["name"].startswith("[Team]")
    perms = {e["role"]: e["session"]["permission_level"] for e in detail["sessions"]}
    assert perms["implementer"] == "full"  # section 12.2
    assert perms["reviewer"] == "readonly"

    # .baton/ artifacts were persisted in the worktree; nothing leaked into the target repo.
    wt = Path(run["worktree_path"])
    assert wt.is_dir()
    for name in ("requirements.md", "architecture.md", "plan.md", "baton_browser_helper.py"):
        assert (wt / ".baton" / name).is_file(), name
    assert (wt / "feature_output.txt").read_text().strip() == "feature implementation complete"
    assert not (git_repo / "feature_output.txt").exists()
    assert not (git_repo / ".baton").exists()

    # Diff endpoint reports the new file and hides .baton/.
    diff = team_client.get(f"/api/team-runs/{run_id}/diff")
    assert diff.status_code == 200
    diff_body = diff.json()
    assert diff_body["run_id"] == run_id
    assert diff_body["branch_name"] == f"baton/{run_id}"
    assert [f["path"] for f in diff_body["files"]] == ["feature_output.txt"]
    assert diff_body["files"][0]["status"] == "A"
    assert diff_body["files"][0]["additions"] == 1
    assert "feature_output.txt" in diff_body["diff_text"]
    assert ".baton" not in diff_body["diff_text"]

    # Sign-off: merge.
    res = team_client.post(
        f"/api/team-runs/{run_id}/signoff",
        json={"action": "merge", "commit_message": "Add feature output"},
    )
    assert res.status_code == 200, res.text
    done = res.json()["run"]
    assert done["status"] == "completed"
    assert done["stage_state"] == "completed"
    assert done["resolution"] == "merged"
    assert done["finished_at"]

    assert (git_repo / "feature_output.txt").read_text().strip() == "feature implementation complete"
    assert not (git_repo / ".baton").exists()
    assert "Add feature output" in git_cmd(git_repo, "log", "--format=%s", "-n", "5")
    assert git_cmd(git_repo, "branch", "--list", f"baton/{run_id}").strip() == ""
    assert not wt.exists()
    assert git_cmd(git_repo, "status", "--porcelain").strip() == ""


def _scripted_claude(tmp_path: Path, respond_src: str) -> Path:
    """
    Writes a `claude` wrapper that reuses tests/fixtures/fake_claude.py but lets the test
    override the team reply via `respond(role, sys_prompt, prompt, state, original)`.
    """
    script = tmp_path / "scripted_claude.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(FIXTURES_DIR)!r})\n"
        "import fake_claude\n"
        f"{respond_src}\n"
        "_orig = fake_claude._generate_team_response\n"
        "fake_claude._generate_team_response = "
        "lambda role, sp, p, st: respond(role, sp, p, st, _orig)\n"
        "sys.exit(fake_claude.main())\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _scripted_client(monkeypatch: pytest.MonkeyPatch, script: Path) -> TestClient:
    monkeypatch.setenv("CLAUDE_BIN", str(script))
    return TestClient(create_app(load_config()))


def test_malformed_reply_gets_exactly_one_repair_reprompt(
    team_client: TestClient, git_repo: Path, team_db: Database
) -> None:
    run_id = _start_run(team_client, git_repo, "[TEAM_MALFORMED_ONCE] Add a feature")
    detail = _wait_run(team_client, run_id, "waiting_final_signoff", "waiting_user")

    # The run still produced the normal message flow (repairs are not routed messages).
    triples = _msg_triples(detail)
    assert len(triples) == 7
    assert triples[0] == DEFAULT_FLOW[0] and triples[-1] == DEFAULT_FLOW[-1]

    # Every role that was served a block-less first reply got precisely one repair prompt.
    turns = team_db.list_turns_for_team_run(run_id)
    repair_turns = [t for t in turns if t["prompt"].startswith("# Format Error")]
    repaired_roles = sorted(t["role"] for t in repair_turns)
    assert repaired_roles == sorted(set(repaired_roles))  # never twice for one role
    assert {"pm", "architect", "planner", "implementer"} <= set(repaired_roles)
    assert len(turns) == 7 + len(repair_turns)
    assert detail["run"]["turn_count"] == len(turns)

    pm_turns = _role_turns(team_db, run_id, "pm")
    assert pm_turns[0]["prompt"].startswith("# Team Run Task")
    assert pm_turns[1]["prompt"].startswith("# Format Error")
    assert "Error:" in pm_turns[1]["prompt"] and "pm_triage" in pm_turns[1]["prompt"]


def test_repair_turn_keeps_original_deliverable_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_repo: Path
) -> None:
    script = _scripted_claude(
        tmp_path,
        '''
def respond(role, sys_prompt, prompt, state, original):
    real = original(role, sys_prompt, prompt, state)
    if role == "pm" and "# Format Error" not in prompt and "# Team Run Task" in prompt:
        return "UNIQUE-REQUIREMENTS-BODY: build it with tests."
    if role == "pm" and "# Format Error" in prompt:
        return real[real.index("```baton-msg"):]  # block only, no prose
    return real
''',
    )
    with _scripted_client(monkeypatch, script) as client:
        run_id = _start_run(client, git_repo)
        detail = _wait_run(client, run_id, "waiting_final_signoff", "waiting_user")
        wt = Path(detail["run"]["worktree_path"])
        pm_msg = detail["messages"][0]
        assert pm_msg["from_role"] == "pm" and pm_msg["to_role"] == "architect"
        assert "UNIQUE-REQUIREMENTS-BODY" in pm_msg["content_text"]
        assert "UNIQUE-REQUIREMENTS-BODY" in (wt / ".baton" / "requirements.md").read_text()


def test_format_repair_failure_marks_run_failed_after_one_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_repo: Path
) -> None:
    script = _scripted_claude(
        tmp_path,
        '''
def respond(role, sys_prompt, prompt, state, original):
    return "I did some work but forgot the machine-readable block."
''',
    )
    app_config = load_config()
    with _scripted_client(monkeypatch, script) as client:
        run_id = _start_run(client, git_repo)
        res = _wait_run(client, run_id, "failed", "failed")
        run = res["run"]
        assert run["error_message"].startswith(
            "Role 'pm' failed to emit a valid baton-msg block after 1 retry"
        )
        assert run["finished_at"]

        db = Database(app_config.db_path)
        pm_turns = _role_turns(db, run_id, "pm")
        assert len(pm_turns) == 2  # original + exactly one repair, never a third
        assert pm_turns[1]["prompt"].startswith("# Format Error")
        assert run["turn_count"] == 2
        assert res["messages"] == []  # nothing was routed
        # The worktree stays for inspection; the user can still discard it.
        assert Path(run["worktree_path"]).is_dir()
        discard = client.post(f"/api/team-runs/{run_id}/signoff", json={"action": "discard"})
        assert discard.status_code == 200, discard.text
        assert discard.json()["run"]["resolution"] == "discarded"
        assert not Path(run["worktree_path"]).exists()


def test_denied_action_gets_dedicated_repair_prompt_then_run_proceeds(
    team_client: TestClient, git_repo: Path, team_db: Database
) -> None:
    # Every role's first turn ends like headless agy/claude does when a command is auto-denied:
    # empty reply + denied_actions (claude roles: "Bash"; agy roles: "RunCommand").
    run_id = _start_run(team_client, git_repo, "[TEAM_DENIED_ONCE] Add a feature")
    detail = _wait_run(team_client, run_id, "waiting_final_signoff", "waiting_user")
    assert _msg_triples(detail) == DEFAULT_FLOW

    turns = team_db.list_turns_for_team_run(run_id)
    assert not [t for t in turns if t["prompt"].startswith("# Format Error")]  # not the generic repair
    denial_turns = [t for t in turns if t["prompt"].startswith("# Action Denied")]
    assert {t["role"] for t in denial_turns} == {
        "pm", "architect", "planner", "implementer", "ui_checker", "reviewer",
    }
    assert len(turns) == 7 + len(denial_turns)

    pm_turns = _role_turns(team_db, run_id, "pm")
    assert pm_turns[0]["denied_actions"] == ["Bash"] and pm_turns[0]["response_text"] == ""
    assert "`Bash`" in pm_turns[1]["prompt"]
    assert "DENIED" in pm_turns[1]["prompt"] and "read-only shell commands" in pm_turns[1]["prompt"]
    assert "```baton-msg" in pm_turns[1]["prompt"] and "pm_triage" in pm_turns[1]["prompt"]
    impl_turns = _role_turns(team_db, run_id, "implementer")
    assert impl_turns[0]["denied_actions"] == ["RunCommand"]
    assert "`RunCommand`" in impl_turns[1]["prompt"]
    assert detail["run"]["turn_count"] == len(turns)


def test_denial_repair_does_not_consume_the_format_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_repo: Path
) -> None:
    # PM: turn 1 denied -> denial repair answered WITHOUT a block -> the normal format repair
    # is still available and succeeds.
    script = _scripted_claude(
        tmp_path,
        '''
def respond(role, sys_prompt, prompt, state, original):
    if role == "pm" and "# Action Denied" in prompt:
        return "Understood, carrying on without the shell (forgot the block)."
    return original(role, sys_prompt, prompt, state)
''',
    )
    app_config = load_config()
    with _scripted_client(monkeypatch, script) as client:
        run_id = _start_run(client, git_repo, "[TEAM_DENIED_ONCE] Add a feature")
        _wait_run(client, run_id, "waiting_final_signoff", "waiting_user")
        pm_turns = _role_turns(Database(app_config.db_path), run_id, "pm")
        assert pm_turns[0]["prompt"].startswith("# Team Run Task")
        assert pm_turns[1]["prompt"].startswith("# Action Denied")
        assert pm_turns[2]["prompt"].startswith("# Format Error")


def test_repeated_denial_fails_run_naming_the_denied_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_repo: Path
) -> None:
    app_config = load_config()
    with TestClient(create_app(app_config)) as client:
        run_id = _start_run(client, git_repo, "[TEAM_DENIED_ALWAYS] Add a feature")
        res = _wait_run(client, run_id, "failed", "failed")
        run = res["run"]
        assert "Role 'pm' was denied `Bash`" in run["error_message"]
        assert "after the denial retry" in run["error_message"]
        assert "read-only" in run["error_message"]
        pm_turns = _role_turns(Database(app_config.db_path), run_id, "pm")
        assert len(pm_turns) == 2  # original + exactly one denial repair, never a third
        assert pm_turns[1]["prompt"].startswith("# Action Denied")
        assert pm_turns[1]["denied_actions"] == ["Bash"]
        assert res["messages"] == []


def test_ui_designer_agy_denial_names_run_command_and_recovers(
    team_client: TestClient, git_repo: Path, team_db: Database
) -> None:
    # The reported scenario: an agy role (UI Designer) is denied RunCommand and answers nothing.
    run_id = _start_run(
        team_client, git_repo, "[TEAM_DENIED_ONCE] [TEAM_WITH_MOCKUP] Add a settings page",
        auto_approve_mockup=True,
    )
    detail = _wait_run(team_client, run_id, "waiting_final_signoff", "waiting_user")
    designer_turns = _role_turns(team_db, run_id, "ui_designer")
    assert designer_turns[0]["denied_actions"] == ["RunCommand"]
    assert designer_turns[0]["response_text"] == ""
    assert designer_turns[1]["prompt"].startswith("# Action Denied")
    assert "`RunCommand`" in designer_turns[1]["prompt"]
    assert ("ui_designer", "pm", "deliverable") in _msg_triples(detail)


def test_invalid_transition_also_triggers_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_repo: Path
) -> None:
    # Valid JSON, but PM tries to hand off to the planner (not allowed in pm_triage).
    script = _scripted_claude(
        tmp_path,
        '''
def respond(role, sys_prompt, prompt, state, original):
    if role == "pm" and "# Format Error" not in prompt and "# Team Run Task" in prompt:
        return (
            "Skipping ahead.\\n\\n```baton-msg\\n"
            '{"kind": "deliverable", "to": "planner", "needs_ui_mockup": false, "summary": "skip"}\\n'
            "```"
        )
    return original(role, sys_prompt, prompt, state)
''',
    )
    app_config = load_config()
    with _scripted_client(monkeypatch, script) as client:
        run_id = _start_run(client, git_repo)
        detail = _wait_run(client, run_id, "waiting_final_signoff", "waiting_user")
        pm_turns = _role_turns(Database(app_config.db_path), run_id, "pm")
        assert pm_turns[1]["prompt"].startswith("# Format Error")
        assert "planner" in pm_turns[1]["prompt"]
        assert detail["messages"][0]["to_role"] == "architect"
        client.post(f"/api/team-runs/{run_id}/signoff", json={"action": "discard"})


# ---------------------------------------------------------------------------
# 3. PM <-> user and specialist <-> PM questions
# ---------------------------------------------------------------------------


def test_pm_question_answered_by_user_via_answer_endpoint(
    team_client: TestClient, git_repo: Path, team_db: Database
) -> None:
    run_id = _start_run(team_client, git_repo, "[TEAM_ASK_USER] Persist the theme")
    detail = _wait_run(team_client, run_id, "waiting_user_answers", "waiting_user")
    run = detail["run"]
    assert run["pending_questions"] == ["Which storage key should we use?"]
    assert run["asking_role"] is None
    assert run["question_round_count"] == 1
    assert _msg_triples(detail) == [("pm", "user", "question")]

    # Invalid answers are rejected without changing state.
    empty = _post(team_client, run_id, "answer", {"answers": []})
    assert empty.status_code == 422
    blank = _post(team_client, run_id, "answer", {"answers": ["   "]})
    assert blank.status_code == 422
    assert _get_detail(team_client, run_id)["run"]["stage_state"] == "waiting_user_answers"

    res = _post(team_client, run_id, "answer", {"answers": ["Use the key baton_theme"]})
    assert res.status_code == 200, res.text
    assert res.json()["run"]["status"] in ("running", "waiting_user")

    final = _wait_run(team_client, run_id, "waiting_final_signoff", "waiting_user")
    run = final["run"]
    assert run["pending_questions"] == []
    assert run["qa_history"] == [
        {
            "round": 1,
            "from": "pm",
            "questions": ["Which storage key should we use?"],
            "answers": ["Use the key baton_theme"],
        }
    ]
    kinds = _msg_triples(final, include_user=True)
    assert kinds[0] == ("pm", "user", "question")
    assert kinds[1] == ("user", "pm", "user_action")
    assert kinds[2] == ("pm", "architect", "deliverable")
    assert kinds[3:6] == DEFAULT_FLOW[1:4]
    assert sorted(kinds[6:8]) == sorted(DEFAULT_FLOW[4:6])
    assert kinds[8] == DEFAULT_FLOW[6]

    pm_turns = _role_turns(team_db, run_id, "pm")
    assert pm_turns[1]["prompt"].startswith("# User Answers to Your Clarifying Questions")
    assert "Use the key baton_theme" in pm_turns[1]["prompt"]
    # The answers were persisted into the requirements artifact for later roles.
    req_md = (Path(run["worktree_path"]) / ".baton" / "requirements.md").read_text()
    assert "Use the key baton_theme" in req_md

    # Answering again is a state conflict.
    again = _post(team_client, run_id, "answer", {"answers": ["late"]})
    assert again.status_code == 409


def test_specialist_question_answered_by_pm_without_user(
    team_client: TestClient, git_repo: Path, team_db: Database
) -> None:
    run_id = _start_run(team_client, git_repo, "[TEAM_ARCH_QUESTION_ANSWERED_BY_PM] Add endpoint")
    # Never pauses for the user: goes straight to final sign-off.
    detail = _wait_run(team_client, run_id, "waiting_final_signoff", "waiting_user")
    triples = _msg_triples(detail)
    assert triples[:5] == [
        ("pm", "architect", "deliverable"),
        ("architect", "pm", "question"),
        ("pm", "architect", "answer"),
        ("architect", "planner", "deliverable"),
        ("planner", "implementer", "deliverable"),
    ]
    assert not any(m["kind"] == "user_action" for m in detail["messages"])
    assert not any(m["to_role"] == "user" and m["kind"] == "question" for m in detail["messages"])

    run = detail["run"]
    assert run["question_round_count"] == 1
    assert run["asking_role"] is None
    assert run["qa_history"] == []

    # PM got the question (T6), architect got the PM's answer (T7) on the same session.
    pm_prompts = [t["prompt"] for t in _role_turns(team_db, run_id, "pm")]
    assert any(p.startswith("# Question from") and "200 or 201" in p for p in pm_prompts)
    arch_turns = _role_turns(team_db, run_id, "architect")
    assert len(arch_turns) == 2
    assert arch_turns[1]["prompt"].startswith("# Answer from PM")
    assert arch_turns[0]["session_id"] == arch_turns[1]["session_id"]


def test_specialist_question_escalated_to_user_then_relayed_back(
    team_client: TestClient, git_repo: Path, team_db: Database
) -> None:
    run_id = _start_run(team_client, git_repo, "[TEAM_ARCH_QUESTION_ESCALATED] Add endpoint")
    detail = _wait_run(team_client, run_id, "waiting_user_answers", "waiting_user")
    run = detail["run"]
    assert run["asking_role"] == "architect"
    assert run["pending_questions"] == ["Should the endpoint return 200 or 201?"]
    assert _msg_triples(detail) == [
        ("pm", "architect", "deliverable"),
        ("architect", "pm", "question"),
        ("pm", "user", "question"),
    ]

    res = _post(team_client, run_id, "answer", {"answers": ["Return 201 on create."]})
    assert res.status_code == 200, res.text

    final = _wait_run(team_client, run_id, "waiting_final_signoff", "waiting_user")
    triples = _msg_triples(final, include_user=True)
    assert triples[:6] == [
        ("pm", "architect", "deliverable"),
        ("architect", "pm", "question"),
        ("pm", "user", "question"),
        ("user", "pm", "user_action"),
        ("pm", "architect", "answer"),
        ("architect", "planner", "deliverable"),
    ]
    run = final["run"]
    assert run["asking_role"] is None
    assert run["qa_history"][0]["from"] == "architect"
    assert run["qa_history"][0]["answers"] == ["Return 201 on create."]

    pm_turns = _role_turns(team_db, run_id, "pm")
    assert any(
        t["prompt"].startswith("# User Answers to Your Clarifying Questions")
        and "Relay the resolved answer to 'architect'" in t["prompt"]
        for t in pm_turns
    )
    arch_turns = _role_turns(team_db, run_id, "architect")
    assert arch_turns[-1]["prompt"].startswith("# Answer from PM")
