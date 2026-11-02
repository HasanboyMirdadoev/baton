"""End-to-end Team Mode flows and API smoke tests (fake CLIs, real git worktrees)."""
from __future__ import annotations

from pathlib import Path
import stat
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from fastapi.testclient import TestClient
import pytest

from backend.config import load_config
from backend.db import Database
from backend.main import MOCKUP_CSP, create_app
from tests.conftest import FIXTURES_DIR, git_cmd

WAIT_TIMEOUT_SEC = 40.0


def _poll(predicate: Callable[[], Any], timeout_sec: float = WAIT_TIMEOUT_SEC, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError(f"{what} not met within {timeout_sec}s")


def _detail(client: TestClient, run_id: str) -> Dict[str, Any]:
    res = client.get(f"/api/team-runs/{run_id}")
    assert res.status_code == 200, res.text
    return res.json()


def _wait(client: TestClient, run_id: str, stage: str, status: Optional[str] = None) -> Dict[str, Any]:
    def _check() -> Optional[Dict[str, Any]]:
        d = _detail(client, run_id)
        run = d["run"]
        if run["stage_state"] == stage and (status is None or run["status"] == status):
            return d
        if run["status"] in ("failed", "completed", "stopped") and run["stage_state"] != stage:
            raise AssertionError(f"run ended {run['status']}/{run['stage_state']}: {run.get('error_message')}")
        return None

    return _poll(_check, what=f"stage {stage}")


def _start(client: TestClient, repo: Path, prompt: str = "Add a feature", **extra: Any) -> str:
    body: Dict[str, Any] = {"task_prompt": prompt, "target_repo": str(repo)}
    body.update(extra)
    res = client.post("/api/team-runs", json=body)
    assert res.status_code == 202, res.text
    return res.json()["run"]["id"]


def _post(client: TestClient, run_id: str, action: str, body: Optional[Dict[str, Any]] = None) -> Any:
    return client.post(f"/api/team-runs/{run_id}/{action}", json=body or {})


def _triples(detail: Dict[str, Any]) -> List[tuple]:
    return [(m["from_role"], m["to_role"], m["kind"]) for m in detail["messages"] if m["kind"] != "user_action"]


def _branches(repo: Path) -> List[str]:
    out = git_cmd(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [b.strip() for b in out.splitlines() if b.strip()]


# ---------------------------------------------------------------------------
# UI Designer / mockup path
# ---------------------------------------------------------------------------


def test_mockup_request_changes_then_approve(client: TestClient, git_repo: Path, app_config: Any) -> None:
    run_id = _start(client, git_repo, "[TEAM_WITH_MOCKUP] Add a theme toggle")
    d = _wait(client, run_id, "waiting_mockup_approval", "waiting_user")
    run = d["run"]
    assert run["needs_ui_mockup"] is True
    assert run["mockup_version"] == 1
    assert run["mockup_approved"] is False
    assert _triples(d) == [
        ("pm", "ui_designer", "deliverable"),
        ("ui_designer", "pm", "deliverable"),
        ("pm", "user", "approval_request"),
    ]
    wt = Path(run["worktree_path"])
    assert (wt / ".baton" / "mockup.html").is_file()
    assert (wt / ".baton" / "mockup_v1.html").is_file()

    # Mockup is served with the sandboxing CSP, latest and by version.
    res = client.get(f"/api/team-runs/{run_id}/mockup")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["content-security-policy"] == MOCKUP_CSP
    assert "sandbox allow-scripts" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["cache-control"] == "no-store"
    assert "<h1>Mockup</h1>" in res.text
    assert client.get(f"/api/team-runs/{run_id}/mockup?v=1").status_code == 200
    assert client.get(f"/api/team-runs/{run_id}/mockup?v=7").status_code == 404

    # Wrong touchpoints are conflicts; blank feedback on a rejection is 422.
    assert _post(client, run_id, "answer", {"answers": ["x"]}).status_code == 409
    assert _post(client, run_id, "signoff", {"action": "merge"}).status_code == 409
    assert _post(client, run_id, "mockup-review", {"approved": False, "feedback": "  "}).status_code == 422

    res = _post(client, run_id, "mockup-review", {"approved": False, "feedback": "Move the toggle right"})
    assert res.status_code == 200, res.text
    d = _poll(
        lambda: (lambda x: x if x["run"]["mockup_version"] == 2
                 and x["run"]["stage_state"] == "waiting_mockup_approval" else None)(_detail(client, run_id)),
        what="second mockup",
    )
    assert (wt / ".baton" / "mockup_v2.html").is_file()
    designer_turns = [t for t in Database(app_config.db_path).list_turns_for_team_run(run_id)
                      if t["role"] == "ui_designer"]
    assert len(designer_turns) == 2
    assert designer_turns[1]["prompt"].startswith("# Mockup Changes Requested")
    assert "Move the toggle right" in designer_turns[1]["prompt"]

    res = _post(client, run_id, "mockup-review", {"approved": True})
    assert res.status_code == 200, res.text
    d = _wait(client, run_id, "waiting_final_signoff", "waiting_user")
    assert d["run"]["mockup_approved"] is True
    assert _post(client, run_id, "mockup-review", {"approved": True}).status_code == 409
    assert _post(client, run_id, "signoff", {"action": "discard"}).status_code == 200


def test_pm_rejects_mockup_once_before_asking_user(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo, "[TEAM_WITH_MOCKUP] [TEAM_MOCKUP_REJECT_ONCE] Add a toggle")
    d = _wait(client, run_id, "waiting_mockup_approval", "waiting_user")
    assert d["run"]["mockup_version"] == 2
    assert ("pm", "ui_designer", "reject") in _triples(d)
    assert _post(client, run_id, "stop").status_code == 200
    assert _post(client, run_id, "signoff", {"action": "discard"}).status_code == 200


def test_auto_approve_mockup_skips_user_approval(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo, "[TEAM_WITH_MOCKUP] Add a toggle", auto_approve_mockup=True)
    d = _wait(client, run_id, "waiting_final_signoff", "waiting_user")
    run = d["run"]
    assert run["auto_approve_mockup"] is True
    assert run["mockup_approved"] is True
    assert run["mockup_version"] == 1
    assert not any(m["kind"] == "user_action" for m in d["messages"])
    assert _post(client, run_id, "signoff", {"action": "discard"}).status_code == 200


# ---------------------------------------------------------------------------
# Checkers: rework loop, limit pause, resume
# ---------------------------------------------------------------------------


def test_ui_checker_reject_triggers_one_rework_loop(client: TestClient, git_repo: Path, app_config: Any) -> None:
    run_id = _start(client, git_repo, "[TEAM_UI_REJECT_ONCE] Add a feature")
    d = _wait(client, run_id, "waiting_final_signoff", "waiting_user")
    run = d["run"]
    assert run["rework_loop_count"] == 1
    assert run["ui_check_verdict"] == "APPROVED" and run["code_review_verdict"] == "APPROVED"
    assert run["last_findings"] == []
    turns = Database(app_config.db_path).list_turns_for_team_run(run_id)
    impl = [t for t in turns if t["role"] == "implementer"]
    assert len(impl) == 2
    assert impl[1]["prompt"].startswith("# Verification & Review Feedback")
    # Both checkers re-ran in the second pass.
    assert len([t for t in turns if t["role"] == "ui_checker"]) == 2
    assert len([t for t in turns if t["role"] == "reviewer"]) == 2
    assert _post(client, run_id, "signoff", {"action": "discard"}).status_code == 200


def test_rework_limit_pauses_then_resume_and_force_signoff(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo, "[TEAM_ALWAYS_REJECT] Add a feature", max_rework_loops=1)
    d = _wait(client, run_id, "paused_limit", "waiting_user")
    run = d["run"]
    assert run["rework_loop_count"] == 1
    assert run["interrupted_stage_state"] == "implementing"
    assert run["code_review_verdict"] == "CHANGES_REQUESTED"
    assert "Maximum rework loops (1)" in run["error_message"]
    assert any("ALWAYS_REJECT" in f for f in run["last_findings"])

    # Wrong touchpoints while paused.
    assert _post(client, run_id, "answer", {"answers": ["x"]}).status_code == 409
    assert _post(client, run_id, "mockup-review", {"approved": True}).status_code == 409

    res = _post(client, run_id, "resume", {"add_rework_loops": 1})
    assert res.status_code == 200, res.text
    d = _poll(
        lambda: (lambda x: x if x["run"]["stage_state"] == "paused_limit"
                 and x["run"]["rework_loop_count"] == 2 else None)(_detail(client, run_id)),
        what="second pause",
    )
    assert d["run"]["max_rework_loops"] == 2

    res = _post(client, run_id, "resume", {"force_signoff": True})
    assert res.status_code == 200, res.text
    d = _wait(client, run_id, "waiting_final_signoff", "waiting_user")
    assert d["run"]["final_summary"]
    assert _post(client, run_id, "resume", {}).status_code == 409
    assert _post(client, run_id, "signoff", {"action": "discard"}).status_code == 200


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


def _scripted_claude(tmp_path: Path, respond_src: str) -> Path:
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


def test_stop_during_turn_then_keep_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_repo: Path) -> None:
    script = _scripted_claude(
        tmp_path,
        '''
import time
def respond(role, sys_prompt, prompt, state, original):
    if role == "architect":
        time.sleep(60)
    return original(role, sys_prompt, prompt, state)
''',
    )
    monkeypatch.setenv("CLAUDE_BIN", str(script))
    with TestClient(create_app(load_config())) as client:
        run_id = _start(client, git_repo)
        _poll(lambda: _detail(client, run_id)["run"]["stage_state"] == "architecture"
              and _detail(client, run_id)["run"]["turn_count"] >= 2, what="architect running")
        time.sleep(0.3)
        t0 = time.monotonic()
        res = _post(client, run_id, "stop")
        assert res.status_code == 200, res.text
        assert time.monotonic() - t0 < 15
        run = res.json()["run"]
        assert run["status"] == "stopped" and run["stage_state"] == "stopped"
        assert run["finished_at"]
        wt = Path(run["worktree_path"])
        assert wt.is_dir()  # left for inspection
        sessions = _detail(client, run_id)["sessions"]
        assert all(e["session"]["status"] != "running" for e in sessions)

        assert _post(client, run_id, "stop").status_code == 200  # idempotent
        assert _post(client, run_id, "resume", {}).status_code == 409
        assert client.get(f"/api/team-runs/{run_id}/diff").status_code == 200

        res = _post(client, run_id, "signoff", {"action": "keep_branch"})
        assert res.status_code == 200, res.text
        assert res.json()["run"]["resolution"] == "kept_branch"
        assert res.json()["run"]["status"] == "stopped"
        assert f"baton/{run_id}" in _branches(git_repo)
        assert not wt.exists()
        assert _post(client, run_id, "signoff", {"action": "discard"}).status_code == 409


# ---------------------------------------------------------------------------
# Sign-off: keep_branch and discard
# ---------------------------------------------------------------------------


def test_keep_branch_commits_work_on_branch_without_touching_checkout(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo)
    run = _wait(client, run_id, "waiting_final_signoff", "waiting_user")["run"]
    wt = Path(run["worktree_path"])
    res = _post(client, run_id, "signoff", {"action": "keep_branch", "commit_message": "Keep me"})
    assert res.status_code == 200, res.text
    done = res.json()["run"]
    assert done["status"] == "completed" and done["resolution"] == "kept_branch"
    assert not wt.exists()
    assert not (git_repo / "feature_output.txt").exists()
    branch = f"baton/{run_id}"
    assert branch in _branches(git_repo)
    assert git_cmd(git_repo, "show", f"{branch}:feature_output.txt").strip() == "feature implementation complete"
    assert git_cmd(git_repo, "log", "--format=%s", "-n", "1", branch).strip() == "Keep me"
    # .baton/ artifacts were never committed.
    assert ".baton" not in git_cmd(git_repo, "ls-tree", "-r", "--name-only", branch)


def test_discard_removes_worktree_and_branch(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo)
    run = _wait(client, run_id, "waiting_final_signoff", "waiting_user")["run"]
    wt = Path(run["worktree_path"])
    head_before = git_cmd(git_repo, "rev-parse", "HEAD")
    res = _post(client, run_id, "signoff", {"action": "discard"})
    assert res.status_code == 200, res.text
    assert res.json()["run"]["resolution"] == "discarded"
    assert not wt.exists()
    assert f"baton/{run_id}" not in _branches(git_repo)
    assert git_cmd(git_repo, "rev-parse", "HEAD") == head_before
    assert git_cmd(git_repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_merge_refused_when_checkout_is_dirty(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo)
    _wait(client, run_id, "waiting_final_signoff", "waiting_user")
    (git_repo / "README.md").write_text("local edit\n", encoding="utf-8")
    res = _post(client, run_id, "signoff", {"action": "merge"})
    assert res.status_code == 409
    assert "uncommitted changes" in res.json()["detail"]
    run = _detail(client, run_id)["run"]
    assert run["stage_state"] == "waiting_final_signoff" and run["resolution"] is None
    git_cmd(git_repo, "checkout", "--", "README.md")
    assert _post(client, run_id, "signoff", {"action": "merge"}).status_code == 200
    assert (git_repo / "feature_output.txt").exists()


# ---------------------------------------------------------------------------
# Target validation (section 12.3)
# ---------------------------------------------------------------------------


def test_non_git_folder_rejected_with_422(client: TestClient, non_git_dir: Path) -> None:
    res = client.post("/api/team-runs", json={"task_prompt": "x", "target_repo": str(non_git_dir)})
    assert res.status_code == 422
    assert "not a git repository" in res.json()["detail"]
    assert client.get("/api/team-runs").json()["runs"] == []


def test_repo_without_commits_rejected_with_422(client: TestClient, empty_git_repo: Path) -> None:
    res = client.post("/api/team-runs", json={"task_prompt": "x", "target_repo": str(empty_git_repo)})
    assert res.status_code == 422
    assert "git init" in res.json()["detail"]
    assert _branches(empty_git_repo) == []


def test_missing_target_folder_rejected(client: TestClient, tmp_path: Path) -> None:
    res = client.post("/api/team-runs", json={"task_prompt": "x", "target_repo": str(tmp_path / "nope")})
    assert res.status_code in (400, 422)


# ---------------------------------------------------------------------------
# API smoke tests for every team endpoint
# ---------------------------------------------------------------------------


def _member(role: str, **over: Any) -> Dict[str, Any]:
    m = {"role": role, "agent_name": f"My {role}", "provider": "claude", "model": "sonnet",
         "permission_level": "readonly", "custom_prompt": ""}
    m.update(over)
    return m


def test_team_config_endpoints(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert {r["id"] for r in meta["team_roles"]} == {
        "pm", "ui_designer", "architect", "planner", "implementer", "ui_checker", "reviewer"}
    impl = [m for m in meta["default_team_members"] if m["role"] == "implementer"][0]
    assert impl["permission_level"] == "full"

    teams = client.get("/api/teams").json()["teams"]
    assert len(teams) == 1 and teams[0]["is_default"] is True
    default_id = teams[0]["id"]
    assert len(teams[0]["members"]) == 7
    assert client.get(f"/api/teams/{default_id}").json()["id"] == default_id
    assert client.get("/api/teams/missing").status_code == 404
    # The last team cannot be deleted.
    assert client.delete(f"/api/teams/{default_id}").status_code == 409

    res = client.post("/api/teams", json={
        "name": "Lean", "max_rework_loops": 0, "auto_approve_mockup": True,
        "members": [_member("reviewer", custom_prompt="Be strict")],
    })
    assert res.status_code == 201, res.text
    team = res.json()
    assert team["max_rework_loops"] == 0 and team["auto_approve_mockup"] is True
    by_role = {m["role"]: m for m in team["members"]}
    assert len(by_role) == 7  # omitted roles filled from defaults
    assert by_role["reviewer"]["custom_prompt"] == "Be strict"
    assert by_role["implementer"]["permission_level"] == "full"

    bad = client.post("/api/teams", json={"name": "Bad", "members": [_member("intern")]})
    assert bad.status_code in (400, 422)
    assert client.post("/api/teams", json={"name": "", "members": []}).status_code == 422
    assert client.post("/api/teams", json={"name": "X", "max_rework_loops": 11}).status_code == 422

    res = client.put(f"/api/teams/{team['id']}", json={"name": "Lean 2", "members": [_member("pm", model="opus")]})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Lean 2"
    assert {m["role"]: m for m in res.json()["members"]}["pm"]["model"] == "opus"
    assert client.put("/api/teams/missing", json={"name": "x"}).status_code == 404

    assert client.delete(f"/api/teams/{team['id']}").json() == {"deleted": True, "team_id": team["id"]}
    assert client.delete(f"/api/teams/{team['id']}").status_code == 404


def test_team_run_endpoints_smoke(client: TestClient, git_repo: Path) -> None:
    team_id = client.get("/api/teams").json()["teams"][0]["id"]
    res = client.post(f"/api/teams/{team_id}/runs", json={"task_prompt": "Add a feature", "target_repo": str(git_repo)})
    assert res.status_code == 202, res.text
    body = res.json()
    run_id = body["run"]["id"]
    assert set(body) == {"run", "sessions", "messages"}
    assert body["run"]["team_id"] == team_id
    assert "pending_prompt" not in body["run"]

    assert client.post("/api/teams/missing/runs", json={"task_prompt": "x", "target_repo": str(git_repo)}).status_code == 404
    assert client.post("/api/team-runs", json={"task_prompt": "", "target_repo": str(git_repo)}).status_code == 422

    # A team with active runs cannot be deleted (needs a second team so it isn't the last).
    other = client.post("/api/teams", json={"name": "Other"}).json()
    assert client.delete(f"/api/teams/{team_id}").status_code == 409

    d = _wait(client, run_id, "waiting_final_signoff", "waiting_user")
    assert [r["id"] for r in client.get("/api/team-runs").json()["runs"]] == [run_id]
    assert d["run"]["status"] == "waiting_user"

    diff = client.get(f"/api/team-runs/{run_id}/diff").json()
    assert {"run_id", "base_branch", "base_commit", "branch_name", "files", "diff_text"} <= set(diff)
    assert client.get(f"/api/team-runs/{run_id}/mockup").status_code == 404  # no mockup in this run

    # Wrong-state actions are 409, bad payloads 422.
    assert _post(client, run_id, "answer", {"answers": ["x"]}).status_code == 409
    assert _post(client, run_id, "mockup-review", {"approved": True}).status_code == 409
    assert _post(client, run_id, "resume", {}).status_code == 409
    assert _post(client, run_id, "signoff", {"action": "squash"}).status_code == 422

    # Unknown run ids are 404 on every run endpoint.
    for method, path, payload in [
        ("get", "", None), ("get", "/diff", None), ("get", "/mockup", None),
        ("post", "/answer", {"answers": ["x"]}), ("post", "/mockup-review", {"approved": True}),
        ("post", "/resume", {}), ("post", "/signoff", {"action": "discard"}),
        ("post", "/stop", {}), ("delete", "", None),
    ]:
        url = f"/api/team-runs/does-not-exist{path}"
        r = getattr(client, method)(url, json=payload) if payload is not None else getattr(client, method)(url)
        assert r.status_code == 404, (method, path, r.status_code)

    assert _post(client, run_id, "signoff", {"action": "merge", "commit_message": "Ship it"}).status_code == 200
    assert (git_repo / "feature_output.txt").exists()
    assert client.delete(f"/api/teams/{other['id']}").status_code == 200
    res = client.delete(f"/api/team-runs/{run_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert client.get("/api/team-runs").json()["runs"] == []
