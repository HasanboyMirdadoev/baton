"""Regression tests for the Team Mode review findings (one section per finding)."""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

from fastapi.testclient import TestClient
import pytest

from backend.config import AppConfig, load_config
from backend.db import Database
from backend.main import MOCKUP_CSP, create_app
from backend.models import ConflictError
from backend.pubsub import EventBus
from backend.team_engine import TeamEngine
from backend.team_protocol import write_browser_helper
from backend.worktree_manager import (
    allocate_safe_port,
    cleanup_port_processes,
    finalize_workspace,
    get_workspace_diff,
    prepare_workspace,
)
from tests.conftest import FIXTURES_DIR, _cleanup_git_repo, git_cmd

WAIT_TIMEOUT_SEC = 40.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _poll(predicate: Callable[[], Any], timeout_sec: float = WAIT_TIMEOUT_SEC, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError(f"{what} not met within {timeout_sec}s")


async def _apoll(predicate: Callable[[], Any], timeout_sec: float = 10.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} not met within {timeout_sec}s")


def _run(client: TestClient, run_id: str) -> Dict[str, Any]:
    res = client.get(f"/api/team-runs/{run_id}")
    assert res.status_code == 200, res.text
    return res.json()["run"]


def _wait_stage(client: TestClient, run_id: str, stage: str, status: Optional[str] = None) -> Dict[str, Any]:
    def _check() -> Optional[Dict[str, Any]]:
        run = _run(client, run_id)
        if run["stage_state"] == stage and (status is None or run["status"] == status):
            return run
        if run["status"] in ("failed", "completed") and run["stage_state"] != stage:
            raise AssertionError(f"run ended {run['status']}/{run['stage_state']}: {run.get('error_message')}")
        return None

    return _poll(_check, what=f"stage {stage}")


def _start(client: TestClient, repo: Path, prompt: str = "Add a feature", **extra: Any) -> str:
    body: Dict[str, Any] = {"task_prompt": prompt, "target_repo": str(repo)}
    body.update(extra)
    res = client.post("/api/team-runs", json=body)
    assert res.status_code == 202, res.text
    return res.json()["run"]["id"]


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


def _slow_pm_script(tmp_path: Path, flag: Path) -> Path:
    """`claude` whose PM turns hang while `flag` exists (other roles answer normally)."""
    return _scripted_claude(
        tmp_path,
        f'''
import os, time
def respond(role, sys_prompt, prompt, state, original):
    if role == "pm" and os.path.exists({str(flag)!r}):
        time.sleep(60)
    return original(role, sys_prompt, prompt, state)
''',
    )


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_cmd(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    git_cmd(path, "add", "-A")
    git_cmd(path, "commit", "-q", "-m", "Initial commit")
    return path


@pytest.fixture
def spaced_repo(tmp_path: Path) -> Iterator[Path]:
    repo = _make_repo(tmp_path / "my repo")
    yield repo
    _cleanup_git_repo(repo)


def _branches(repo: Path) -> List[str]:
    out = git_cmd(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [b.strip() for b in out.splitlines() if b.strip()]


# ---------------------------------------------------------------------------
# #1 Sign-off never discards work when the commit fails
# ---------------------------------------------------------------------------


def test_merge_with_failing_pre_commit_hook_keeps_the_work(spaced_repo: Path, tmp_path: Path) -> None:
    hook = spaced_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    ws = prepare_workspace("run-hook", str(spaced_repo), str(tmp_path / "data dir"))
    wt = Path(ws["worktree_path"])
    (wt / "feature.py").write_text("print('implementer work')\n", encoding="utf-8")

    res = finalize_workspace(dict(ws, id="run-hook", task_prompt="x"), "merge", "Add feature")
    assert res["resolution"] == "merged"
    assert (spaced_repo / "feature.py").read_text() == "print('implementer work')\n"
    assert "Add feature" in git_cmd(spaced_repo, "log", "--format=%s", "-n", "3")
    assert not wt.exists()
    assert "baton/run-hook" not in _branches(spaced_repo)


@pytest.mark.parametrize("action", ["merge", "keep_branch"])
def test_commit_failure_raises_and_leaves_worktree_in_place(
    git_repo: Path, tmp_path: Path, action: str
) -> None:
    ws = prepare_workspace("run-lock", str(git_repo), str(tmp_path / "data"))
    wt = Path(ws["worktree_path"])
    (wt / "feature.py").write_text("work\n", encoding="utf-8")

    git_dir = git_cmd(wt, "rev-parse", "--absolute-git-dir").strip()
    lock = Path(git_dir) / "index.lock"
    lock.write_text("", encoding="utf-8")  # makes `git add` fail
    run = dict(ws, id="run-lock", task_prompt="x")
    with pytest.raises(ConflictError):
        finalize_workspace(run, action, "msg")
    assert (wt / "feature.py").read_text() == "work\n"
    assert not (git_repo / "feature.py").exists()

    lock.unlink()
    res = finalize_workspace(run, action, "msg")
    if action == "merge":
        assert (git_repo / "feature.py").exists()
    else:
        assert res["resolution"] == "kept_branch"
        assert git_cmd(git_repo, "show", "baton/run-lock:feature.py") == "work\n"
    assert not wt.exists()


# ---------------------------------------------------------------------------
# #2 Graceful server restart leaves an active run resumable
# ---------------------------------------------------------------------------


def test_graceful_shutdown_leaves_run_interrupted_and_resumable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_repo: Path
) -> None:
    flag = tmp_path / "pm_slow.flag"
    flag.write_text("1", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_BIN", str(_slow_pm_script(tmp_path, flag)))
    cfg = load_config()

    with TestClient(create_app(cfg)) as client:
        run_id = _start(client, git_repo)
        _poll(lambda: _run(client, run_id)["turn_count"] >= 1, what="PM turn started")
        time.sleep(0.3)
    # Lifespan shutdown ran (server restart). The run must not be 'stopped'.
    raw = Database(cfg.db_path).get_team_run(run_id)
    assert raw is not None and raw["status"] == "running", raw

    flag.unlink()
    with TestClient(create_app(cfg)) as client:
        run = _run(client, run_id)
        assert run["status"] == "waiting_user"
        assert run["stage_state"] == "interrupted"
        assert run["interrupted_stage_state"] == "pm_triage"
        assert "restarted" in (run["error_message"] or "")

        res = client.post(f"/api/team-runs/{run_id}/resume", json={})
        assert res.status_code == 200, res.text
        _wait_stage(client, run_id, "waiting_final_signoff", "waiting_user")
        assert client.post(f"/api/team-runs/{run_id}/signoff", json={"action": "discard"}).status_code == 200


# ---------------------------------------------------------------------------
# #3 Port cleanup only stops listeners started inside the run's worktree
# ---------------------------------------------------------------------------

_LISTENER_SRC = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n"
    "conns = []\n"
    "s.settimeout(0.2)\n"
    "end = time.time() + 60\n"
    "while time.time() < end:\n"
    "    try:\n"
    "        conns.append(s.accept()[0])\n"
    "    except OSError:\n"
    "        pass\n"
)
_CLIENT_SRC = (
    "import socket, sys, time\n"
    "c = socket.create_connection(('127.0.0.1', int(sys.argv[1])))\n"
    "time.sleep(60)\n"
)


def _spawn(src: str, port: int, cwd: Path) -> subprocess.Popen:  # type: ignore[type-arg]
    return subprocess.Popen(
        [sys.executable, "-c", src, str(port)],
        cwd=str(cwd),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def test_cleanup_kills_worktree_listener_but_not_clients_or_foreign_servers(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    port = allocate_safe_port()
    other_port = allocate_safe_port([port])
    procs: List[subprocess.Popen] = []  # type: ignore[type-arg]
    try:
        app = _spawn(_LISTENER_SRC, port, worktree)
        foreign = _spawn(_LISTENER_SRC, other_port, elsewhere)
        procs += [app, foreign]
        _poll(lambda: _listening(port) and _listening(other_port), 10, "listeners up")
        client = _spawn(_CLIENT_SRC, port, elsewhere)  # stands in for the user's browser
        procs.append(client)
        time.sleep(0.5)

        killed = cleanup_port_processes(str(worktree), port)
        assert killed == [app.pid]
        _poll(lambda: app.poll() is not None, 10, "app listener stopped")
        assert client.poll() is None, "a client connected to the port must never be signalled"

        # A server the user started outside the worktree is left alone.
        assert cleanup_port_processes(str(worktree), other_port) == []
        assert foreign.poll() is None
        # Without a worktree to match against, nothing is killed.
        assert cleanup_port_processes(other_port) == []
        assert foreign.poll() is None
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)


# ---------------------------------------------------------------------------
# #4 Allocator never hands out a port someone is listening on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family_host", [("v4", "0.0.0.0"), ("v6", "::")])
def test_allocator_skips_port_with_wildcard_listener(family_host: Any) -> None:
    family, host = family_host
    if family == "v6" and not socket.has_ipv6:
        pytest.skip("no IPv6")
    port = allocate_safe_port()
    fam = "socket.AF_INET6" if family == "v6" else "socket.AF_INET"
    src = (
        "import socket, sys, time\n"
        f"s = socket.socket({fam}); s.bind(({host!r}, int(sys.argv[1]))); s.listen()\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", src, str(port)], start_new_session=True)
    try:
        _poll(lambda: _listening(port) or proc.poll() is not None, 10, "listener up")
        if proc.poll() is not None:
            pytest.skip(f"could not bind {host}")
        with pytest.raises(RuntimeError):
            allocate_safe_port(start_port=port, end_port=port)
        assert allocate_safe_port(start_port=port, end_port=min(port + 30, 18999)) != port
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Fake SessionManager for engine-level race / prompt tests (#5, #6)
# ---------------------------------------------------------------------------

_APP_RUN = '"app_run":{"start_cmd":"python3 -m http.server {port}","app_url":"http://127.0.0.1:{port}"}'


def _blk(payload: str) -> str:
    return f"Body text.\n\n```baton-msg\n{payload}\n```"


DEFAULT_REPLIES = {
    "pm": _blk('{"kind":"deliverable","to":"architect","needs_ui_mockup":false,"summary":"req"}'),
    "pm_final": _blk('{"kind":"done","to":"user","summary":"all done"}'),
    "architect": _blk('{"kind":"deliverable","to":"planner","summary":"arch",' + _APP_RUN + "}"),
    "planner": _blk('{"kind":"deliverable","to":"implementer","summary":"plan",' + _APP_RUN + "}"),
    "implementer": _blk('{"kind":"deliverable","to":"checkers","summary":"impl"}'),
    "ui_checker": _blk('{"kind":"approve","to":"pm","summary":"ui ok"}'),
    "reviewer": _blk('{"kind":"approve","to":"pm","summary":"code ok"}'),
}


class FakeSessionManager:
    """Answers turns in-process; `script(role, prompt, call_no)` may override a reply."""

    def __init__(self, db: Database, script: Optional[Callable[..., Any]] = None) -> None:
        self.db = db
        self.script = script
        self.prompts: Dict[str, List[str]] = {}

    def is_session_running(self, session_id: str) -> bool:
        return False

    async def stop_session(self, session_id: str) -> bool:
        return True

    async def run_turn_and_wait(self, session_id: str, prompt: str, timeout_sec: Any = None,
                                team_run_id: Any = None, **_: Any) -> Dict[str, Any]:
        role = self.db.get_session(session_id)["role"]
        calls = self.prompts.setdefault(role, [])
        calls.append(prompt)
        if self.script is not None:
            out = self.script(role, prompt, len(calls))
            if asyncio.iscoroutine(out):
                out = await out
            if out is not None:
                return out
        key = "pm_final" if role == "pm" and "All Verification Checks Approved" in prompt else role
        return {"id": None, "status": "completed", "response_text": DEFAULT_REPLIES[key]}


def _timeout_turn() -> Dict[str, Any]:
    return {"id": None, "status": "timeout", "response_text": "", "error_message": "turn timed out"}


# ---------------------------------------------------------------------------
# #5 Resume while the other checker is still running relaunches the loop
# ---------------------------------------------------------------------------


def test_resume_during_checking_barrier_relaunches_state_machine(
    app_config: AppConfig, db: Database, git_repo: Path
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def script(role: str, prompt: str, n: int) -> Optional[Dict[str, Any]]:
            if role == "ui_checker" and n == 1:
                return _timeout_turn()
            if role == "reviewer" and n == 1:
                await release.wait()
            return None

        sm = FakeSessionManager(db, script)
        te = TeamEngine(db, sm, EventBus(), app_config)  # type: ignore[arg-type]
        run_id = (await te.start_team_run(task_prompt="t", target_repo=str(git_repo)))["run"]["id"]
        await _apoll(lambda: db.get_team_run(run_id)["stage_state"] == "paused_limit", what="paused")

        # While the reviewer is still busy, sign-off must refuse (agent still in the worktree).
        with pytest.raises(ConflictError):
            await te.signoff_run(run_id, "discard")

        await te.resume_run(run_id)
        assert db.get_team_run(run_id)["status"] == "running"
        release.set()

        await _apoll(
            lambda: db.get_team_run(run_id)["stage_state"] == "waiting_final_signoff",
            what="run reached final sign-off after resume",
        )
        assert len(sm.prompts["ui_checker"]) == 2
        await te.signoff_run(run_id, "discard")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #6 The next stage prompt survives a timeout and a server restart
# ---------------------------------------------------------------------------


def test_pending_prompt_survives_timeout_and_restart(
    app_config: AppConfig, db: Database, git_repo: Path
) -> None:
    question = "Should we use Postgres or SQLite?"

    def script(role: str, prompt: str, n: int) -> Optional[Dict[str, Any]]:
        if role == "architect" and n == 1:
            return {"id": None, "status": "completed", "response_text": _blk(
                '{"kind":"question","to":"pm","summary":"q","questions":["%s"]}' % question)}
        if role == "pm" and n == 2:
            return _timeout_turn()  # PM relay turn times out
        if role == "pm" and n == 3:
            return {"id": None, "status": "completed", "response_text": _blk(
                '{"kind":"answer","to":"architect","summary":"Use SQLite"}')}
        return None

    async def scenario() -> None:
        sm = FakeSessionManager(db, script)
        te = TeamEngine(db, sm, EventBus(), app_config)  # type: ignore[arg-type]
        run_id = (await te.start_team_run(task_prompt="t", target_repo=str(git_repo)))["run"]["id"]
        await _apoll(lambda: db.get_team_run(run_id)["stage_state"] == "paused_limit", what="paused")
        run = db.get_team_run(run_id)
        assert run["interrupted_stage_state"] == "pm_answer_relay"
        assert run["pending_questions"] == [question]
        assert "pending_prompt" not in run  # never leaked into API payloads
        assert question in (db.get_team_run_pending_prompt(run_id) or "")

        # Simulate a server restart: a brand-new engine with no in-memory state.
        te2 = TeamEngine(db, sm, EventBus(), app_config)  # type: ignore[arg-type]
        await te2.resume_run(run_id, note="Prefer the simplest option.")
        await _apoll(lambda: len(sm.prompts["pm"]) >= 3, what="PM relay retried")
        retried = sm.prompts["pm"][2]
        assert question in retried
        assert "## User Guidance on Resume\nPrefer the simplest option." in retried
        await _apoll(
            lambda: db.get_team_run(run_id)["stage_state"] == "waiting_final_signoff",
            what="final sign-off",
        )
        # The architect got the PM's answer as its second turn.
        assert sm.prompts["architect"][1].startswith("# Answer from PM")
        assert db.get_team_run_pending_prompt(run_id) is None
        await te2.signoff_run(run_id, "discard")

    asyncio.run(scenario())


def test_rework_prompt_survives_restart_at_rework_limit(
    app_config: AppConfig, db: Database, git_repo: Path
) -> None:
    reject = _blk('{"kind":"reject","to":"implementer","summary":"bad","findings":["Fix the UNIQUE-BUG"]}')

    def script(role: str, prompt: str, n: int) -> Optional[Dict[str, Any]]:
        if role == "reviewer" and n == 1:
            return {"id": None, "status": "completed", "response_text": reject}
        return None

    async def scenario() -> None:
        sm = FakeSessionManager(db, script)
        te = TeamEngine(db, sm, EventBus(), app_config)  # type: ignore[arg-type]
        run_id = (
            await te.start_team_run(task_prompt="t", target_repo=str(git_repo), max_rework_loops=0)
        )["run"]["id"]
        await _apoll(lambda: db.get_team_run(run_id)["stage_state"] == "paused_limit", what="paused")
        run = db.get_team_run(run_id)
        # #9: max_rework_loops=0 pauses at the first rejection instead of allowing 3 loops.
        assert run["max_rework_loops"] == 0
        assert run["rework_loop_count"] == 0
        assert "(0)" in run["error_message"]

        te2 = TeamEngine(db, sm, EventBus(), app_config)  # type: ignore[arg-type]
        await te2.resume_run(run_id, add_rework_loops=1)
        await _apoll(
            lambda: db.get_team_run(run_id)["stage_state"] == "waiting_final_signoff", what="final"
        )
        rework_prompt = sm.prompts["implementer"][1]
        assert rework_prompt.startswith("# Verification & Review Feedback")
        assert "UNIQUE-BUG" in rework_prompt
        await te2.signoff_run(run_id, "discard")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #7 Mockup CSP sandboxes the page even as a top-level document
# ---------------------------------------------------------------------------


def test_port_helpers_return_plain_ints() -> None:
    # Port values used to be an awaitable int subclass, which socket APIs reject
    # ("Int or String expected") when handed to create_connection / getaddrinfo.
    port = allocate_safe_port()
    assert type(port) is int
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
    assert type(cleanup_port_processes("/nonexistent", port)) is list


def test_mockup_csp_has_sandbox_directive() -> None:
    directives = [d.strip() for d in MOCKUP_CSP.split(";") if d.strip()]
    assert "sandbox allow-scripts" in directives
    assert "connect-src 'none'" in directives
    assert not any("allow-same-origin" in d for d in directives)


# ---------------------------------------------------------------------------
# #8 Sign-off runs at most once
# ---------------------------------------------------------------------------


def test_signoff_twice_is_rejected(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo)
    _wait_stage(client, run_id, "waiting_final_signoff", "waiting_user")
    assert client.post(f"/api/team-runs/{run_id}/signoff", json={"action": "keep_branch"}).status_code == 200
    again = client.post(f"/api/team-runs/{run_id}/signoff", json={"action": "discard"})
    assert again.status_code == 409
    assert "already signed off" in again.json()["detail"]
    # The kept branch survived the rejected second sign-off.
    assert f"baton/{run_id}" in _branches(git_repo)


def test_concurrent_signoff_is_serialized(app_config: AppConfig, db: Database, git_repo: Path) -> None:
    async def scenario() -> None:
        te = TeamEngine(db, FakeSessionManager(db), EventBus(), app_config)  # type: ignore[arg-type]
        run_id = (await te.start_team_run(task_prompt="t", target_repo=str(git_repo)))["run"]["id"]
        await _apoll(lambda: db.get_team_run(run_id)["stage_state"] == "waiting_final_signoff", what="final")
        results = await asyncio.gather(
            te.signoff_run(run_id, "merge"), te.signoff_run(run_id, "merge"), return_exceptions=True
        )
        assert sum(isinstance(r, ConflictError) for r in results) == 1
        assert db.get_team_run(run_id)["resolution"] == "merged"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #10 Detached HEAD: merge refused, keep_branch still works
# ---------------------------------------------------------------------------


def test_merge_refused_for_run_started_from_detached_head(git_repo: Path, tmp_path: Path) -> None:
    git_cmd(git_repo, "checkout", "-q", "--detach")
    ws = prepare_workspace("run-detached", str(git_repo), str(tmp_path / "data"))
    assert ws["base_branch"] == "HEAD"
    wt = Path(ws["worktree_path"])
    (wt / "feature.py").write_text("work\n", encoding="utf-8")
    run = dict(ws, id="run-detached", task_prompt="x")

    with pytest.raises(ConflictError, match="detached HEAD"):
        finalize_workspace(run, "merge", "msg")
    assert (wt / "feature.py").exists()
    assert "baton/run-detached" in _branches(git_repo)

    assert finalize_workspace(run, "keep_branch", "msg")["resolution"] == "kept_branch"
    assert git_cmd(git_repo, "show", "baton/run-detached:feature.py") == "work\n"


# ---------------------------------------------------------------------------
# #11 Deleting a run / a failed start cleans the user's repo
# ---------------------------------------------------------------------------


def test_delete_unresolved_run_removes_worktree_and_branch(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo)
    run = _wait_stage(client, run_id, "waiting_final_signoff", "waiting_user")
    wt = Path(run["worktree_path"])
    assert wt.is_dir() and f"baton/{run_id}" in _branches(git_repo)

    res = client.delete(f"/api/team-runs/{run_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert not wt.exists()
    assert f"baton/{run_id}" not in _branches(git_repo)
    assert git_cmd(git_repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert client.get(f"/api/team-runs/{run_id}").status_code == 404


def test_delete_kept_run_keeps_its_branch(client: TestClient, git_repo: Path) -> None:
    run_id = _start(client, git_repo)
    _wait_stage(client, run_id, "waiting_final_signoff", "waiting_user")
    assert client.post(f"/api/team-runs/{run_id}/signoff", json={"action": "keep_branch"}).status_code == 200
    assert client.delete(f"/api/team-runs/{run_id}").status_code == 200
    assert f"baton/{run_id}" in _branches(git_repo)


def test_failed_start_rolls_back_worktree_branch_and_rows(
    app_config: AppConfig, db: Database, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("bind failed")

    monkeypatch.setattr(db, "bind_team_run_session", boom)
    te = TeamEngine(db, FakeSessionManager(db), EventBus(), app_config)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="bind failed"):
        asyncio.run(te.start_team_run(task_prompt="t", target_repo=str(git_repo)))

    assert db.list_team_runs() == []
    assert not [b for b in _branches(git_repo) if b.startswith("baton/")]
    assert git_cmd(git_repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    wt_root = app_config.data_dir / "worktrees"
    assert not wt_root.exists() or list(wt_root.iterdir()) == []
    assert not [s for s in db.list_sessions() if s.get("team_run_id")]


# ---------------------------------------------------------------------------
# #12 Picking a subfolder of a repo keeps the agents in that subfolder
# ---------------------------------------------------------------------------


def test_subfolder_target_runs_agents_in_matching_worktree_subfolder(
    client: TestClient, git_repo: Path
) -> None:
    sub = git_repo / "services" / "api"
    sub.mkdir(parents=True)
    (sub / "app.py").write_text("print('api')\n", encoding="utf-8")
    git_cmd(git_repo, "add", "-A")
    git_cmd(git_repo, "commit", "-q", "-m", "Add api")

    run_id = _start(client, sub)
    run = _wait_stage(client, run_id, "waiting_final_signoff", "waiting_user")
    wt = Path(run["worktree_path"])
    work_dir = wt / "services" / "api"
    assert run["work_subdir"] == "services/api"
    assert Path(run["work_dir"]) == work_dir

    detail = client.get(f"/api/team-runs/{run_id}").json()
    assert {e["session"]["working_dir"] for e in detail["sessions"]} == {str(work_dir)}
    assert (work_dir / ".baton" / "requirements.md").is_file()
    assert (work_dir / "feature_output.txt").is_file()
    assert not (wt / "feature_output.txt").exists()

    diff = client.get(f"/api/team-runs/{run_id}/diff").json()
    assert [f["path"] for f in diff["files"]] == ["services/api/feature_output.txt"]

    assert client.post(f"/api/team-runs/{run_id}/signoff", json={"action": "merge"}).status_code == 200
    assert (sub / "feature_output.txt").is_file()
    assert not (sub / ".baton").exists()
    assert git_cmd(git_repo, "status", "--porcelain").strip() == ""


# ---------------------------------------------------------------------------
# #13 Reviewer gets the full diff as a file it can read
# ---------------------------------------------------------------------------


def test_reviewer_prompt_points_to_full_diff_file(client: TestClient, git_repo: Path, app_config: AppConfig) -> None:
    run_id = _start(client, git_repo)
    run = _wait_stage(client, run_id, "waiting_final_signoff", "waiting_user")
    review_diff = Path(run["worktree_path"]) / ".baton" / "review.diff"
    assert review_diff.is_file()
    assert "feature_output.txt" in review_diff.read_text()
    turns = Database(app_config.db_path).list_turns_for_team_run(run_id)
    reviewer_prompts = [t["prompt"] for t in turns if t["role"] == "reviewer"]
    assert reviewer_prompts and "`.baton/review.diff`" in reviewer_prompts[0]
    # review.diff is a Baton artifact: never part of the diff or the commit.
    diff = client.get(f"/api/team-runs/{run_id}/diff").json()
    assert "review.diff" not in diff["diff_text"]


# ---------------------------------------------------------------------------
# #14 UI checker helper does not hang a chatty app
# ---------------------------------------------------------------------------


def test_browser_helper_drains_chatty_app_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper_path = write_browser_helper(tmp_path)
    spec = importlib.util.spec_from_file_location("baton_browser_helper_test", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)  # type: ignore[union-attr]
    if importlib.util.find_spec("playwright") is not None:
        pytest.skip("playwright installed; fallback path not exercised")

    server = tmp_path / "chatty_server.py"
    server.write_text(
        "import http.server, os, sys\n"
        "sys.stdout.write('x' * 300000 + '\\n'); sys.stdout.flush()\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])),"
        " http.server.SimpleHTTPRequestHandler).serve_forever()\n",
        encoding="utf-8",
    )
    port = allocate_safe_port()
    monkeypatch.chdir(tmp_path)
    with helper.run_app_and_check(
        f"{sys.executable} {server}",
        f"http://127.0.0.1:{port}",
        allocated_port=port,
        startup_timeout_sec=10.0,
    ) as result:
        assert result["ready"] is True
    log = tmp_path / ".baton" / "app.log"
    assert log.stat().st_size >= 300000
    assert not _listening(port)


# ---------------------------------------------------------------------------
# #15 Diff never touches the worktree's real index
# ---------------------------------------------------------------------------


def test_workspace_diff_uses_private_index(git_repo: Path, tmp_path: Path) -> None:
    ws = prepare_workspace("run-idx", str(git_repo), str(tmp_path / "data"))
    wt = Path(ws["worktree_path"])
    (wt / "new file.txt").write_text("hello\n", encoding="utf-8")
    (wt / "README.md").write_text("# changed\n", encoding="utf-8")
    index_before = git_cmd(wt, "ls-files", "--stage")

    diff = get_workspace_diff(str(wt), ws["base_commit"])
    by_path = {f["path"]: f["status"] for f in diff["files"]}
    assert by_path == {"README.md": "M", "new file.txt": "A"}
    assert "new file.txt" in diff["diff_text"]
    assert git_cmd(wt, "ls-files", "--stage") == index_before
    assert "new file.txt" not in git_cmd(wt, "ls-files")
    finalize_workspace(dict(ws, id="run-idx"), "discard")
