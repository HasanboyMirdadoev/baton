from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from backend.db import Database
from backend.models import ConflictError, PipelineRunStatus, StageRunStatus, ValidationError
from backend.pipeline_engine import (
    PipelineEngine,
    parse_reviewer_verdict,
    validate_pipeline_stages,
)


def _create_four_role_sessions(db: Database, workspace_dir: Path) -> Dict[str, Dict[str, Any]]:
    designer = db.create_session(
        name="Designer",
        provider="antigravity",
        model="gemini-4-argon-high",
        role="designer",
        custom_prompt="",
        permission_level="readonly",
        working_dir=str(workspace_dir),
    )
    planner = db.create_session(
        name="Planner",
        provider="claude",
        model="sonnet",
        role="planner",
        custom_prompt="",
        permission_level="readonly",
        working_dir=str(workspace_dir),
    )
    implementer = db.create_session(
        name="Implementer",
        provider="antigravity",
        model="gemini-4-argon-high",
        role="implementer",
        custom_prompt="",
        permission_level="edit",
        working_dir=str(workspace_dir),
    )
    reviewer = db.create_session(
        name="Code Reviewer",
        provider="claude",
        model="sonnet",
        role="reviewer",
        custom_prompt="",
        permission_level="readonly",
        working_dir=str(workspace_dir),
    )
    return {
        "designer": designer,
        "planner": planner,
        "implementer": implementer,
        "reviewer": reviewer,
    }


async def _wait_for_run_status(
    db: Database,
    run_id: str,
    target_statuses: Set[str],
    timeout_sec: float = 10.0,
) -> Dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        run = db.get_pipeline_run(run_id)
        if run is not None and run["status"] in target_statuses:
            return run
        await asyncio.sleep(0.02)
    run = db.get_pipeline_run(run_id)
    raise AssertionError(
        f"Run {run_id} did not reach {target_statuses} within {timeout_sec}s (current={run})"
    )


def test_parse_reviewer_verdict_variants() -> None:
    # 1. Exact APPROVED
    v1 = parse_reviewer_verdict("Everything looks solid.\nVERDICT: APPROVED")
    assert v1.verdict == "APPROVED"
    assert v1.findings_text == ""

    # 2. Markdown bold APPROVED
    v2 = parse_reviewer_verdict("Checked all files.\n**VERDICT: APPROVED**\n")
    assert v2.verdict == "APPROVED"

    # 3. CHANGES_REQUESTED with FINDINGS:
    v3 = parse_reviewer_verdict(
        "Found issues:\nVERDICT: CHANGES_REQUESTED\nFINDINGS:\n- [a.py:1] Fix import\n- [b.py:2] Add type hint"
    )
    assert v3.verdict == "CHANGES_REQUESTED"
    assert "- [a.py:1] Fix import" in v3.findings_text
    assert "- [b.py:2] Add type hint" in v3.findings_text

    # 4. CHANGES_REQUESTED without FINDINGS: header (falls back to trailing text)
    v4 = parse_reviewer_verdict(
        "VERDICT: CHANGES_REQUESTED\n- Missing null check in handler"
    )
    assert v4.verdict == "CHANGES_REQUESTED"
    assert "- Missing null check in handler" in v4.findings_text

    # 5. Multiple verdicts (takes the last one)
    v5 = parse_reviewer_verdict(
        "Earlier note: VERDICT: CHANGES_REQUESTED\nFinal check:\nVERDICT: APPROVED"
    )
    assert v5.verdict == "APPROVED"

    # 6. MISSING verdict
    v6 = parse_reviewer_verdict("Looks good to me, ship it!")
    assert v6.verdict == "MISSING"


def test_validate_pipeline_stages_rejects_invalid_configs(
    db: Database, workspace_dir: Path
) -> None:
    roles = _create_four_role_sessions(db, workspace_dir)

    # Empty stages
    with pytest.raises(ValidationError):
        validate_pipeline_stages(db, [])

    # Reviewer without preceding Implementer
    with pytest.raises(ValidationError):
        validate_pipeline_stages(
            db,
            [
                {"name": "Design", "session_id": roles["designer"]["id"]},
                {"name": "Review", "session_id": roles["reviewer"]["id"]},
            ],
        )

    # Valid when Implementer precedes Reviewer
    validate_pipeline_stages(
        db,
        [
            {"name": "Implement", "session_id": roles["implementer"]["id"]},
            {"name": "Review", "session_id": roles["reviewer"]["id"]},
        ],
    )


def test_pipeline_four_stage_happy_path_and_rework_loop(
    db: Database,
    pipeline_engine: PipelineEngine,
    workspace_dir: Path,
) -> None:
    async def _run() -> None:
        roles = _create_four_role_sessions(db, workspace_dir)
        pipeline = db.create_pipeline(
            name="4-Stage Pipeline",
            description="Design -> Plan -> Implement -> Review",
            max_review_loops=2,
            stages=[
                {"name": "Design", "session_id": roles["designer"]["id"], "require_approval": False},
                {"name": "Plan", "session_id": roles["planner"]["id"], "require_approval": False},
                {"name": "Implement", "session_id": roles["implementer"]["id"], "require_approval": False},
                {"name": "Review", "session_id": roles["reviewer"]["id"], "require_approval": False},
            ],
        )

        # 1. Happy path (4 stage runs, 0 rework loops)
        started = await pipeline_engine.start_pipeline_run(
            pipeline["id"], "Add healthcheck endpoint"
        )
        run_id = started["run"]["id"]
        final_run = await _wait_for_run_status(
            db, run_id, {PipelineRunStatus.COMPLETED.value, PipelineRunStatus.FAILED.value}
        )
        assert final_run["status"] == PipelineRunStatus.COMPLETED.value
        assert final_run["review_loop_count"] == 0
        stage_runs = db.list_stage_runs(run_id)
        assert len(stage_runs) == 4
        assert stage_runs[-1]["verdict"] == "APPROVED"

        # 2. Rework loop path ([CHANGES_REQUESTED] on fresh Reviewer session)
        roles2 = _create_four_role_sessions(db, workspace_dir)
        pipeline2 = db.create_pipeline(
            name="Rework Pipeline",
            description="",
            max_review_loops=2,
            stages=[
                {"name": "Design", "session_id": roles2["designer"]["id"], "require_approval": False},
                {"name": "Plan", "session_id": roles2["planner"]["id"], "require_approval": False},
                {"name": "Implement", "session_id": roles2["implementer"]["id"], "require_approval": False},
                {"name": "Review", "session_id": roles2["reviewer"]["id"], "require_approval": False},
            ],
        )
        started2 = await pipeline_engine.start_pipeline_run(
            pipeline2["id"], "Implement feature with [CHANGES_REQUESTED]"
        )
        run2_id = started2["run"]["id"]
        final_run2 = await _wait_for_run_status(
            db, run2_id, {PipelineRunStatus.COMPLETED.value, PipelineRunStatus.FAILED.value}
        )
        assert final_run2["status"] == PipelineRunStatus.COMPLETED.value
        assert final_run2["review_loop_count"] == 1

        sr2 = db.list_stage_runs(run2_id)
        # 4 initial stages + 1 Implementer rework + 1 Reviewer re-review = 6 stage runs
        assert len(sr2) == 6
        assert sr2[3]["status"] == StageRunStatus.CHANGES_REQUESTED.value
        assert sr2[3]["verdict"] == "CHANGES_REQUESTED"
        assert "Fix bug in foo.py" in (sr2[3]["findings"] or "")
        assert sr2[4]["role"] == "implementer"
        assert sr2[4]["iteration"] == 1
        assert "# Code Review Feedback (Rework Loop 1 of 2)" in sr2[4]["input_prompt"]
        assert sr2[5]["role"] == "reviewer"
        assert sr2[5]["iteration"] == 1
        assert "# Re-Review Request (Rework Loop 1 of 2)" in sr2[5]["input_prompt"]
        assert sr2[5]["verdict"] == "APPROVED"

    asyncio.run(_run())


def test_pipeline_loop_limit_exhaustion_and_missing_verdict(
    db: Database,
    pipeline_engine: PipelineEngine,
    workspace_dir: Path,
) -> None:
    async def _run() -> None:
        # 1. [ALWAYS_REJECT] with max_review_loops=2 fails after 2 rework loops
        roles = _create_four_role_sessions(db, workspace_dir)
        pipeline = db.create_pipeline(
            name="Exhaustion Pipeline",
            description="",
            max_review_loops=2,
            stages=[
                {"name": "Implement", "session_id": roles["implementer"]["id"], "require_approval": False},
                {"name": "Review", "session_id": roles["reviewer"]["id"], "require_approval": False},
            ],
        )
        started = await pipeline_engine.start_pipeline_run(
            pipeline["id"], "Task that will [ALWAYS_REJECT]"
        )
        run_id = started["run"]["id"]
        final_run = await _wait_for_run_status(
            db, run_id, {PipelineRunStatus.FAILED.value, PipelineRunStatus.COMPLETED.value}
        )
        assert final_run["status"] == PipelineRunStatus.FAILED.value
        assert final_run["review_loop_count"] == 2
        assert "Maximum review loops (2) reached" in (final_run["error_message"] or "")

        # 2. [MISSING_VERDICT] fails cleanly
        roles_mv = _create_four_role_sessions(db, workspace_dir)
        pipeline_mv = db.create_pipeline(
            name="Missing Verdict Pipeline",
            description="",
            max_review_loops=2,
            stages=[
                {"name": "Implement", "session_id": roles_mv["implementer"]["id"], "require_approval": False},
                {"name": "Review", "session_id": roles_mv["reviewer"]["id"], "require_approval": False},
            ],
        )
        started_mv = await pipeline_engine.start_pipeline_run(
            pipeline_mv["id"], "Task with [MISSING_VERDICT]"
        )
        mv_id = started_mv["run"]["id"]
        final_mv = await _wait_for_run_status(
            db, mv_id, {PipelineRunStatus.FAILED.value, PipelineRunStatus.COMPLETED.value}
        )
        assert final_mv["status"] == PipelineRunStatus.FAILED.value
        assert "VERDICT:" in (final_mv["error_message"] or "")

    asyncio.run(_run())


def test_pipeline_approval_pause_and_stop(
    db: Database,
    pipeline_engine: PipelineEngine,
    workspace_dir: Path,
) -> None:
    async def _run() -> None:
        # 1. Approval pause and resume with note
        roles = _create_four_role_sessions(db, workspace_dir)
        pipeline = db.create_pipeline(
            name="Approval Pipeline",
            description="",
            max_review_loops=1,
            stages=[
                {"name": "Plan", "session_id": roles["planner"]["id"], "require_approval": True},
                {"name": "Implement", "session_id": roles["implementer"]["id"], "require_approval": False},
            ],
        )
        started = await pipeline_engine.start_pipeline_run(
            pipeline["id"], "Build module with approval gate"
        )
        run_id = started["run"]["id"]
        waiting_run = await _wait_for_run_status(
            db, run_id, {PipelineRunStatus.WAITING_APPROVAL.value}
        )
        assert waiting_run["status"] == PipelineRunStatus.WAITING_APPROVAL.value

        # Starting a concurrent run on a paused (waiting_approval) pipeline must be rejected
        with pytest.raises(ConflictError):
            await pipeline_engine.start_pipeline_run(
                pipeline["id"], "Concurrent run while paused"
            )

        await pipeline_engine.approve_pipeline_run(run_id, note="Looks good, keep it simple")
        completed_run = await _wait_for_run_status(
            db, run_id, {PipelineRunStatus.COMPLETED.value, PipelineRunStatus.FAILED.value}
        )
        assert completed_run["status"] == PipelineRunStatus.COMPLETED.value

        stage_runs = db.list_stage_runs(run_id)
        assert len(stage_runs) == 2
        assert stage_runs[0]["approval_note"] == "Looks good, keep it simple"
        assert "## User Approval Note\nLooks good, keep it simple" in stage_runs[1]["input_prompt"]

        # 2. Stop pipeline while [SLOW] stage is running
        roles_stop = _create_four_role_sessions(db, workspace_dir)
        pipeline_stop = db.create_pipeline(
            name="Stop Pipeline",
            description="",
            max_review_loops=1,
            stages=[
                {"name": "Slow Stage", "session_id": roles_stop["implementer"]["id"], "require_approval": False},
            ],
        )
        started_stop = await pipeline_engine.start_pipeline_run(
            pipeline_stop["id"], "Run slow stage [SLOW]"
        )
        stop_run_id = started_stop["run"]["id"]
        stopped_detail = await pipeline_engine.stop_pipeline_run(stop_run_id)
        assert stopped_detail["run"]["status"] == PipelineRunStatus.STOPPED.value
        assert len(stopped_detail["stage_runs"]) == 1
        assert stopped_detail["stage_runs"][0]["status"] == StageRunStatus.STOPPED.value
        assert stopped_detail["stage_runs"][0]["turn_id"] is not None

        # 3. Verify Database.create_turn links pipeline_stage_runs.turn_id immediately upon creation
        sr_direct = db.create_stage_run(
            pipeline_run_id=stop_run_id,
            stage_id=pipeline_stop["stages"][0]["id"],
            stage_order=0,
            stage_name="Direct Stage",
            session_id=roles_stop["planner"]["id"],
            session_name=roles_stop["planner"]["name"],
            provider=roles_stop["planner"]["provider"],
            model=roles_stop["planner"]["model"],
            role=roles_stop["planner"]["role"],
            iteration=0,
            require_approval=False,
            input_prompt="Test immediate turn_id linkage",
        )
        assert sr_direct["turn_id"] is None
        created_turn = db.create_turn(
            session_id=roles_stop["planner"]["id"],
            prompt="Test immediate turn_id linkage",
            pipeline_run_id=stop_run_id,
            stage_run_id=sr_direct["id"],
        )
        sr_after_create = db.get_stage_run(sr_direct["id"])
        assert sr_after_create is not None
        assert sr_after_create["turn_id"] == created_turn["id"]

    asyncio.run(_run())
