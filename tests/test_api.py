from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient


def _poll_until(predicate, timeout_sec: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("Condition not met within timeout")


def test_root_html_and_meta_endpoint(client: TestClient) -> None:
    res_html = client.get("/")
    assert res_html.status_code == 200
    assert "Agent Relay" in res_html.text

    res_meta = client.get("/api/meta")
    assert res_meta.status_code == 200
    meta = res_meta.json()
    assert "default_working_dir" in meta
    assert len(meta["providers"]) == 2
    assert len(meta["roles"]) == 5
    assert len(meta["permissions"]) == 3


def test_sessions_and_turns_api_lifecycle(
    client: TestClient, workspace_dir: Path
) -> None:
    # Invalid working directory returns 400
    bad_res = client.post(
        "/api/sessions",
        json={
            "name": "Bad Dir",
            "provider": "claude",
            "model": "sonnet",
            "role": "planner",
            "working_dir": "/System",
        },
    )
    assert bad_res.status_code == 400

    # Create session (omitting permission_level uses role default 'readonly')
    create_res = client.post(
        "/api/sessions",
        json={
            "name": "API Planner",
            "provider": "claude",
            "model": "sonnet",
            "role": "planner",
            "working_dir": str(workspace_dir),
        },
    )
    assert create_res.status_code == 201
    session = create_res.json()
    session_id = session["id"]
    assert session["permission_level"] == "readonly"

    # List sessions
    list_res = client.get("/api/sessions")
    assert list_res.status_code == 200
    assert any(s["id"] == session_id for s in list_res.json()["sessions"])

    # Patch session
    patch_res = client.patch(
        f"/api/sessions/{session_id}",
        json={"name": "API Planner Updated", "permission_level": "edit"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["name"] == "API Planner Updated"
    assert patch_res.json()["permission_level"] == "edit"

    # Start a turn
    turn_res = client.post(
        f"/api/sessions/{session_id}/turns",
        json={"prompt": "Outline the API routes"},
    )
    assert turn_res.status_code == 202
    turn_id = turn_res.json()["turn_id"]

    # Wait for turn completion
    def _turn_done() -> bool:
        r = client.get(f"/api/turns/{turn_id}")
        return r.status_code == 200 and r.json()["turn"]["status"] == "completed"

    _poll_until(_turn_done)

    # Verify GET /api/turns and GET /api/turns/{turn_id}
    all_turns_res = client.get("/api/turns")
    assert all_turns_res.status_code == 200
    assert any(t["id"] == turn_id for t in all_turns_res.json()["turns"])

    turn_detail = client.get(f"/api/turns/{turn_id}").json()
    assert turn_detail["turn"]["status"] == "completed"
    assert turn_detail["turn"]["session_name"] == "API Planner Updated"
    assert turn_detail["turn"]["provider"] == "claude"
    assert turn_detail["turn"]["model"] == "sonnet"
    assert turn_detail["turn"]["role"] == "planner"
    assert len(turn_detail["events"]) >= 4

    # Verify GET /api/sessions/{id} includes turn and captured provider_session_id
    sess_detail = client.get(f"/api/sessions/{session_id}").json()
    assert sess_detail["session"]["provider_session_id"] == "fake-claude-uuid-1"
    assert len(sess_detail["turns"]) == 1

    # Start a [SLOW] turn and stop it via POST /api/sessions/{id}/stop
    slow_turn_res = client.post(
        f"/api/sessions/{session_id}/turns",
        json={"prompt": "Wait [SLOW]"},
    )
    assert slow_turn_res.status_code == 202
    stop_res = client.post(f"/api/sessions/{session_id}/stop")
    assert stop_res.status_code == 200
    assert stop_res.json()["stopped"] is True

    # Delete session
    del_res = client.delete(f"/api/sessions/{session_id}")
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True


def test_pipelines_and_runs_api_lifecycle(
    client: TestClient, workspace_dir: Path
) -> None:
    # Create Implementer and Reviewer sessions
    imp_sess = client.post(
        "/api/sessions",
        json={
            "name": "Impl Session",
            "provider": "antigravity",
            "model": "gemini-4-argon-high",
            "role": "implementer",
            "working_dir": str(workspace_dir),
        },
    ).json()
    rev_sess = client.post(
        "/api/sessions",
        json={
            "name": "Rev Session",
            "provider": "claude",
            "model": "sonnet",
            "role": "reviewer",
            "working_dir": str(workspace_dir),
        },
    ).json()

    # Creating a pipeline with Reviewer before Implementer returns 400
    invalid_pipe = client.post(
        "/api/pipelines",
        json={
            "name": "Invalid Pipe",
            "description": "",
            "max_review_loops": 2,
            "stages": [
                {"name": "Review Only", "session_id": rev_sess["id"], "require_approval": False}
            ],
        },
    )
    assert invalid_pipe.status_code == 400

    # Create valid pipeline with approval on stage 1
    pipe_res = client.post(
        "/api/pipelines",
        json={
            "name": "API Pipeline",
            "description": "Implement then Review",
            "max_review_loops": 2,
            "stages": [
                {"name": "Implement", "session_id": imp_sess["id"], "require_approval": True},
                {"name": "Review", "session_id": rev_sess["id"], "require_approval": False},
            ],
        },
    )
    assert pipe_res.status_code == 201
    pipeline = pipe_res.json()
    pipeline_id = pipeline["id"]

    # Deleting a session bound to a pipeline returns 409 Conflict
    del_bound = client.delete(f"/api/sessions/{imp_sess['id']}")
    assert del_bound.status_code == 409

    # Update pipeline via PUT
    put_res = client.put(
        f"/api/pipelines/{pipeline_id}",
        json={
            "name": "API Pipeline Updated",
            "description": "Updated description",
            "max_review_loops": 1,
            "stages": [
                {"name": "Implement", "session_id": imp_sess["id"], "require_approval": True},
                {"name": "Review", "session_id": rev_sess["id"], "require_approval": False},
            ],
        },
    )
    assert put_res.status_code == 200
    assert put_res.json()["name"] == "API Pipeline Updated"

    # List pipelines
    assert len(client.get("/api/pipelines").json()["pipelines"]) == 1

    # Start pipeline run
    run_res = client.post(
        f"/api/pipelines/{pipeline_id}/runs",
        json={"task_prompt": "Write hello.py"},
    )
    assert run_res.status_code == 202
    run_id = run_res.json()["run"]["id"]

    # Wait until run pauses in waiting_approval
    def _is_waiting() -> bool:
        r = client.get(f"/api/pipeline-runs/{run_id}").json()
        return r["run"]["status"] == "waiting_approval"

    _poll_until(_is_waiting)

    # Concurrent run while pipeline is paused in waiting_approval returns 409 Conflict
    concurrent_res = client.post(
        f"/api/pipelines/{pipeline_id}/runs",
        json={"task_prompt": "Second run while waiting"},
    )
    assert concurrent_res.status_code == 409

    # Approve pipeline run with note
    approve_res = client.post(
        f"/api/pipeline-runs/{run_id}/approve",
        json={"note": "Approved via API test"},
    )
    assert approve_res.status_code == 200

    # Wait until run completes
    def _is_completed() -> bool:
        r = client.get(f"/api/pipeline-runs/{run_id}").json()
        return r["run"]["status"] in ("completed", "failed")

    _poll_until(_is_completed)

    detail = client.get(f"/api/pipeline-runs/{run_id}").json()
    assert detail["run"]["status"] == "completed"
    assert len(detail["stage_runs"]) == 2
    assert "Approved via API test" in detail["stage_runs"][1]["input_prompt"]

    # List pipeline runs
    runs_list = client.get("/api/pipeline-runs").json()["runs"]
    assert any(r["id"] == run_id for r in runs_list)

    # Start a slow pipeline run and stop it via POST /api/pipeline-runs/{run_id}/stop
    slow_run_res = client.post(
        f"/api/pipelines/{pipeline_id}/runs",
        json={"task_prompt": "Slow run [SLOW]"},
    )
    assert slow_run_res.status_code == 202
    slow_run_id = slow_run_res.json()["run"]["id"]

    stop_run_res = client.post(f"/api/pipeline-runs/{slow_run_id}/stop")
    assert stop_run_res.status_code == 200
    assert stop_run_res.json()["run"]["status"] == "stopped"
    assert stop_run_res.json()["stage_runs"][0]["status"] == "stopped"

    # Delete pipeline now that no runs are active
    del_pipe = client.delete(f"/api/pipelines/{pipeline_id}")
    assert del_pipe.status_code == 200
    assert del_pipe.json()["deleted"] is True
