from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient

from backend.db import Database


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
    assert "Baton" in res_html.text or "Agent Relay" in res_html.text

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


def test_forward_turn_api_happy_path_and_errors(
    client: TestClient, db: Database, workspace_dir: Path
) -> None:
    designer_sess = client.post(
        "/api/sessions",
        json={
            "name": "Arch Designer",
            "provider": "antigravity",
            "model": "gemini-4-argon-high",
            "role": "designer",
            "working_dir": str(workspace_dir),
        },
    ).json()
    planner_sess = client.post(
        "/api/sessions",
        json={
            "name": "Lead Planner",
            "provider": "claude",
            "model": "sonnet",
            "role": "planner",
            "working_dir": str(workspace_dir),
        },
    ).json()

    # Run a turn on designer_sess and wait for completion
    t1_res = client.post(
        f"/api/sessions/{designer_sess['id']}/turns",
        json={"prompt": "Create the system architecture"},
    )
    assert t1_res.status_code == 202
    t1_id = t1_res.json()["turn_id"]

    _poll_until(
        lambda: client.get(f"/api/turns/{t1_id}").json()["turn"]["status"] == "completed"
    )
    t1_detail = client.get(f"/api/turns/{t1_id}").json()["turn"]

    # 1. 404 on unknown turn or unknown target session
    assert (
        client.post(
            "/api/turns/unknown-turn-id/forward",
            json={"target_session_id": planner_sess["id"]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/turns/{t1_id}/forward",
            json={"target_session_id": "unknown-session-id"},
        ).status_code
        == 404
    )

    # 2. 422 when forwarding to the same session
    same_sess_res = client.post(
        f"/api/turns/{t1_id}/forward",
        json={"target_session_id": designer_sess["id"]},
    )
    assert same_sess_res.status_code == 422

    # 3. Happy path forward (default instructions when omitted/empty)
    fwd_res = client.post(
        f"/api/turns/{t1_id}/forward",
        json={"target_session_id": planner_sess["id"]},
    )
    assert fwd_res.status_code == 202
    fwd_body = fwd_res.json()
    assert set(fwd_body.keys()) == {"turn_id", "session_id", "turn_number", "status"}
    assert fwd_body["session_id"] == planner_sess["id"]
    assert fwd_body["turn_number"] == 1
    fwd_turn_id = fwd_body["turn_id"]

    _poll_until(
        lambda: client.get(f"/api/turns/{fwd_turn_id}").json()["turn"]["status"]
        == "completed"
    )
    fwd_turn = client.get(f"/api/turns/{fwd_turn_id}").json()["turn"]
    expected_prompt = (
        "# Handoff from Arch Designer (Designer)\n\n"
        "Proceed with your role using the output below.\n\n"
        "---\n\n"
        "# Output from Arch Designer (Designer, turn #1)\n\n"
        f"{t1_detail['response_text']}"
    )
    assert fwd_turn["prompt"] == expected_prompt

    # 4. Forward with custom instructions
    fwd2_res = client.post(
        f"/api/turns/{t1_id}/forward",
        json={
            "target_session_id": planner_sess["id"],
            "instructions": "Break this into 3 milestones.",
        },
    )
    assert fwd2_res.status_code == 202
    fwd2_turn_id = fwd2_res.json()["turn_id"]
    _poll_until(
        lambda: client.get(f"/api/turns/{fwd2_turn_id}").json()["turn"]["status"]
        == "completed"
    )
    fwd2_turn = client.get(f"/api/turns/{fwd2_turn_id}").json()["turn"]
    assert "Break this into 3 milestones." in fwd2_turn["prompt"]

    # 5. 409 when target session is busy, and 422 when source turn is incomplete
    slow_res = client.post(
        f"/api/sessions/{planner_sess['id']}/turns",
        json={"prompt": "Long running task [SLOW]"},
    )
    assert slow_res.status_code == 202
    slow_turn_id = slow_res.json()["turn_id"]

    # Forwarding completed t1_id to busy planner_sess -> 409
    busy_target_res = client.post(
        f"/api/turns/{t1_id}/forward",
        json={"target_session_id": planner_sess["id"]},
    )
    assert busy_target_res.status_code == 409

    # Forwarding incomplete slow_turn_id to idle designer_sess -> 422
    incomplete_src_res = client.post(
        f"/api/turns/{slow_turn_id}/forward",
        json={"target_session_id": designer_sess["id"]},
    )
    assert incomplete_src_res.status_code == 422

    # Stop the slow turn; stopped turn also returns 422 when forwarded
    client.post(f"/api/sessions/{planner_sess['id']}/stop")
    stopped_src_res = client.post(
        f"/api/turns/{slow_turn_id}/forward",
        json={"target_session_id": designer_sess["id"]},
    )
    assert stopped_src_res.status_code == 422

    # Completed turn with empty response_text returns 422
    empty_turn = db.create_turn(
        session_id=designer_sess["id"], prompt="Empty reply test"
    )
    db.update_turn(empty_turn["id"], status="completed", response_text="   ")
    db.update_session(designer_sess["id"], status="idle")
    empty_src_res = client.post(
        f"/api/turns/{empty_turn['id']}/forward",
        json={"target_session_id": planner_sess["id"]},
    )
    assert empty_src_res.status_code == 422


def test_pipeline_run_start_stage_api_and_validation(
    client: TestClient, workspace_dir: Path
) -> None:
    des_s = client.post(
        "/api/sessions",
        json={
            "name": "Des",
            "provider": "antigravity",
            "model": "gemini-4-argon-high",
            "role": "designer",
            "working_dir": str(workspace_dir),
        },
    ).json()
    plan_s = client.post(
        "/api/sessions",
        json={
            "name": "Plan",
            "provider": "claude",
            "model": "sonnet",
            "role": "planner",
            "working_dir": str(workspace_dir),
        },
    ).json()
    imp_s = client.post(
        "/api/sessions",
        json={
            "name": "Imp",
            "provider": "antigravity",
            "model": "gemini-4-argon-high",
            "role": "implementer",
            "working_dir": str(workspace_dir),
        },
    ).json()
    rev_s = client.post(
        "/api/sessions",
        json={
            "name": "Rev",
            "provider": "claude",
            "model": "sonnet",
            "role": "reviewer",
            "working_dir": str(workspace_dir),
        },
    ).json()

    pipe = client.post(
        "/api/pipelines",
        json={
            "name": "4-Stage Pipe",
            "description": "",
            "max_review_loops": 2,
            "stages": [
                {"name": "Design", "session_id": des_s["id"], "require_approval": False},
                {"name": "Plan", "session_id": plan_s["id"], "require_approval": False},
                {"name": "Implement", "session_id": imp_s["id"], "require_approval": False},
                {"name": "Review", "session_id": rev_s["id"], "require_approval": False},
            ],
        },
    ).json()
    pipe_id = pipe["id"]

    # Validation errors -> 422 / 404
    assert (
        client.post(
            f"/api/pipelines/{pipe_id}/runs",
            json={"task_prompt": ""},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/pipelines/{pipe_id}/runs",
            json={"task_prompt": "ok", "start_stage_index": 0, "initial_input": "bad"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/pipelines/{pipe_id}/runs",
            json={"task_prompt": "ok", "start_stage_index": 10, "initial_input": "x"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/pipelines/{pipe_id}/runs",
            json={"task_prompt": "ok", "start_stage_index": 2},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/pipelines/{pipe_id}/runs",
            json={
                "task_prompt": "ok",
                "start_stage_index": 2,
                "initial_input_turn_id": "missing-turn",
            },
        ).status_code
        == 404
    )

    # Start at stage 2 via API with initial_input
    run_res = client.post(
        f"/api/pipelines/{pipe_id}/runs",
        json={
            "task_prompt": "",
            "start_stage_index": 2,
            "initial_input": "Implement feature X directly",
            "initial_input_turn_id": None,
        },
    )
    assert run_res.status_code == 202
    run_id = run_res.json()["run"]["id"]
    assert run_res.json()["run"]["start_stage_index"] == 2

    _poll_until(
        lambda: client.get(f"/api/pipeline-runs/{run_id}").json()["run"]["status"]
        in ("completed", "failed")
    )
    detail = client.get(f"/api/pipeline-runs/{run_id}").json()
    assert detail["run"]["status"] == "completed"
    assert detail["run"]["start_stage_index"] == 2
    assert [sr["stage_order"] for sr in detail["stage_runs"]] == [2, 3]
