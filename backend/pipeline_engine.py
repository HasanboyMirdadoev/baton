from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from backend.db import Database
from backend.models import (
    ConflictError,
    NotFoundError,
    PipelineRunStatus,
    RoleId,
    SessionStatus,
    StageRunStatus,
    TurnStatus,
    ValidationError,
    utc_now_iso,
)
from backend.pubsub import EventBus
from backend.session_manager import SessionManager


@dataclass
class ReviewVerdict:
    verdict: str          # "APPROVED" | "CHANGES_REQUESTED" | "MISSING"
    findings_text: str    # Extracted findings bullet list or trailing text


_VERDICT_RE = re.compile(
    r"(?:^|\n)[ \t*`#_~>\-]*VERDICT[ \t*`#_~]*:[ \t*`_]*(APPROVED|CHANGES_REQUESTED)[ \t*`_]*(?=\r?\n|$)",
    re.IGNORECASE,
)
_FINDINGS_RE = re.compile(
    r"FINDINGS[ \t*`#_~]*:[ \t*`_]*\r?\n?(.*)",
    re.IGNORECASE | re.DOTALL,
)


def parse_reviewer_verdict(reply_text: str) -> ReviewVerdict:
    """
    Finds the LAST occurrence of `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED` in `reply_text`.
    - If not found, returns ReviewVerdict(verdict="MISSING", findings_text="").
    - If "APPROVED", returns ReviewVerdict(verdict="APPROVED", findings_text="").
    - If "CHANGES_REQUESTED", extracts text after `FINDINGS:` following the last verdict match
      (or all text following the verdict line if `FINDINGS:` header was omitted).
    """
    if not reply_text:
        return ReviewVerdict(verdict="MISSING", findings_text="")

    matches = list(_VERDICT_RE.finditer(reply_text))
    if not matches:
        return ReviewVerdict(verdict="MISSING", findings_text="")

    last_match = matches[-1]
    verdict_val = last_match.group(1).upper()
    if verdict_val == "APPROVED":
        return ReviewVerdict(verdict="APPROVED", findings_text="")

    tail = reply_text[last_match.end():]
    findings_match = _FINDINGS_RE.search(tail)
    if findings_match:
        findings_text = findings_match.group(1).strip()
    else:
        findings_text = tail.strip()

    return ReviewVerdict(verdict="CHANGES_REQUESTED", findings_text=findings_text)


def validate_pipeline_stages(db: Database, stages: Sequence[Any]) -> None:
    """
    Validates pipeline stage definitions:
    - Raises ValidationError if `stages` is empty.
    - Raises ValidationError if any stage's `session_id` does not exist in `db`.
    - Raises ValidationError if any stage is bound to a session with `role == "reviewer"`
      and there is no preceding stage (`idx < reviewer_idx`) bound to a session with `role == "implementer"`.
    """
    if not stages:
        raise ValidationError("Pipeline must contain at least one stage.")

    seen_implementer = False
    for idx, stage in enumerate(stages):
        if isinstance(stage, dict):
            session_id = stage.get("session_id")
            stage_name = stage.get("name") or f"Stage {idx + 1}"
        else:
            session_id = getattr(stage, "session_id", None)
            stage_name = getattr(stage, "name", None) or f"Stage {idx + 1}"

        if not session_id or not str(session_id).strip():
            raise ValidationError(f"Stage '{stage_name}' is missing a valid session_id.")

        session = db.get_session(str(session_id))
        if session is None:
            raise ValidationError(
                f"Stage '{stage_name}' references non-existent session '{session_id}'."
            )

        role = session.get("role")
        if role == RoleId.IMPLEMENTER.value or role == RoleId.IMPLEMENTER:
            seen_implementer = True
        elif role == RoleId.REVIEWER.value or role == RoleId.REVIEWER:
            if not seen_implementer:
                raise ValidationError(
                    f"Reviewer stage '{stage_name}' requires at least one preceding Implementer stage."
                )


def build_handoff_prompt(
    case: str = "A",
    task_prompt: str = "",
    prev_stage_name: str = "",
    prev_session_role: str = "",
    prev_stage_output: str = "",
    stage_index: int = 0,
    current_stage_name: str = "",
    loop_num: int = 1,
    max_review_loops: int = 2,
    findings_text: str = "",
    reviewer_stage_output: str = "",
    implementer_rework_output: str = "",
    approval_note: Optional[str] = None,
) -> str:
    """
    Constructs exact stage handoff prompts (Case A, B, C, D) from ARCHITECTURE.md §8.2.
    """
    note_block = (
        f"\n\n## User Approval Note\n{approval_note.strip()}"
        if (approval_note and approval_note.strip())
        else ""
    )
    case_key = case.upper().strip()

    if case_key == "A":
        return f"# Pipeline Task\n\n{task_prompt}{note_block}"

    if case_key == "B":
        return (
            f"# Pipeline Task (Original User Request)\n\n"
            f"{task_prompt}\n\n"
            f"---\n\n"
            f"# Output from Previous Stage ({prev_stage_name} — {prev_session_role})\n\n"
            f"{prev_stage_output}\n\n"
            f"---\n\n"
            f"# Your Instructions for Stage {stage_index + 1} ({current_stage_name})\n\n"
            f"Proceed with your role based on the task and the previous stage output above.{note_block}"
        )

    if case_key == "C":
        return (
            f"# Code Review Feedback (Rework Loop {loop_num} of {max_review_loops})\n\n"
            f"The Code Reviewer reviewed your implementation and requested changes.\n\n"
            f"## Reviewer Findings\n"
            f"{findings_text}\n\n"
            f"## Full Reviewer Report\n"
            f"{reviewer_stage_output}\n\n"
            f"---\n\n"
            f"Please fix every finding above in the working directory and reply with a summary of the fixes made.{note_block}"
        )

    if case_key == "D":
        return (
            f"# Re-Review Request (Rework Loop {loop_num} of {max_review_loops})\n\n"
            f"The Implementer has addressed your previous findings.\n\n"
            f"## Implementer Rework Report\n"
            f"{implementer_rework_output}\n\n"
            f"---\n\n"
            f"Please inspect the updated workspace changes and conclude your reply with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED` + `FINDINGS:`.{note_block}"
        )

    raise ValueError(f"Unknown handoff prompt case: {case!r}")


class PipelineEngine:
    def __init__(
        self, db: Database, session_manager: SessionManager, bus: EventBus
    ) -> None:
        self.db = db
        self.session_manager = session_manager
        self.bus = bus
        self._active_run_tasks: Dict[str, asyncio.Task[None]] = {}
        self._approval_events: Dict[str, asyncio.Event] = {}
        self._approval_notes: Dict[str, Optional[str]] = {}
        self._waiting_stage_run_ids: Dict[str, str] = {}
        self._active_stage_sessions: Dict[str, str] = {}
        self._stopped_runs: Set[str] = set()

    def validate_pipeline_stages(self, stages: Sequence[Any]) -> None:
        validate_pipeline_stages(self.db, stages)

    async def start_pipeline_run(
        self, pipeline_id: str, task_prompt: str
    ) -> Dict[str, Any]:
        """Creates a pipeline_run record, launches background execution task, returns run detail dict."""
        pipeline = self.db.get_pipeline(pipeline_id)
        if pipeline is None:
            raise NotFoundError(f"Pipeline {pipeline_id} not found")

        stages: List[Dict[str, Any]] = pipeline.get("stages") or []
        validate_pipeline_stages(self.db, stages)

        if self.db.has_active_pipeline_runs(pipeline_id):
            raise ConflictError(
                f"Pipeline '{pipeline['name']}' already has an active run"
            )

        for stage in stages:
            sess_id = stage["session_id"]
            sess = self.db.get_session(sess_id)
            if sess is None:
                raise ValidationError(f"Session {sess_id} not found")
            if (
                self.session_manager.is_session_running(sess_id)
                or sess.get("status") == SessionStatus.RUNNING.value
                or sess_id in self._active_stage_sessions.values()
            ):
                raise ConflictError(
                    f"Session '{sess.get('name', sess_id)}' is currently running a turn"
                )

        run = self.db.create_pipeline_run(
            pipeline_id=pipeline_id,
            pipeline_name=pipeline["name"],
            task_prompt=task_prompt,
            max_review_loops=int(pipeline.get("max_review_loops", 2)),
            status=PipelineRunStatus.RUNNING.value,
        )
        run_id: str = run["id"]

        await self.bus.publish("pipeline_run_updated", {"run": run})

        task = asyncio.create_task(self._run_pipeline_loop(run_id, pipeline))
        self._active_run_tasks[run_id] = task

        return {"run": run, "stage_runs": []}

    async def _wait_for_approval(
        self, run_id: str, stage_run_id: str
    ) -> Optional[str]:
        event = asyncio.Event()
        self._approval_events[run_id] = event
        self._approval_notes[run_id] = None
        self._waiting_stage_run_ids[run_id] = stage_run_id

        updated_sr = self.db.update_stage_run(
            stage_run_id,
            status=StageRunStatus.WAITING_APPROVAL.value,
        )
        updated_run = self.db.update_pipeline_run(
            run_id,
            status=PipelineRunStatus.WAITING_APPROVAL.value,
        )

        if updated_sr is not None:
            await self.bus.publish("pipeline_stage_updated", {"stage_run": updated_sr})
        if updated_run is not None:
            await self.bus.publish("pipeline_run_updated", {"run": updated_run})
        if updated_sr is not None:
            await self.bus.publish(
                "pipeline_waiting_approval",
                {"run_id": run_id, "stage_run": updated_sr},
            )

        try:
            await event.wait()
        finally:
            self._approval_events.pop(run_id, None)
            self._waiting_stage_run_ids.pop(run_id, None)

        note = self._approval_notes.pop(run_id, None)
        return note

    async def _mark_run_failed(
        self, run_id: str, error_message: str, run_start_monotonic: float
    ) -> None:
        now = utc_now_iso()
        duration_ms = int((time.monotonic() - run_start_monotonic) * 1000)
        updated_run = self.db.update_pipeline_run(
            run_id,
            status=PipelineRunStatus.FAILED.value,
            error_message=error_message,
            finished_at=now,
            duration_ms=duration_ms,
        )
        if updated_run is not None:
            await self.bus.publish("pipeline_run_updated", {"run": updated_run})

    async def _mark_run_stopped(
        self, run_id: str, run_start_monotonic: float
    ) -> None:
        now = utc_now_iso()
        duration_ms = int((time.monotonic() - run_start_monotonic) * 1000)
        updated_run = self.db.update_pipeline_run(
            run_id,
            status=PipelineRunStatus.STOPPED.value,
            finished_at=now,
            duration_ms=duration_ms,
        )
        if updated_run is not None:
            await self.bus.publish("pipeline_run_updated", {"run": updated_run})

    async def _execute_stage_turn(
        self,
        run_id: str,
        stage: Dict[str, Any],
        stage_order: int,
        session: Dict[str, Any],
        iteration: int,
        input_prompt: str,
        run_start_monotonic: float,
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], bool]:
        stage_start_monotonic = time.monotonic()
        stage_started_at = utc_now_iso()

        updated_run = self.db.update_pipeline_run(
            run_id,
            status=PipelineRunStatus.RUNNING.value,
            current_stage_order=stage_order,
        )
        stage_run = self.db.create_stage_run(
            pipeline_run_id=run_id,
            stage_id=stage["id"],
            stage_order=stage_order,
            stage_name=stage["name"],
            session_id=session["id"],
            session_name=session["name"],
            provider=session["provider"],
            model=session["model"],
            role=session["role"],
            iteration=iteration,
            require_approval=bool(stage.get("require_approval", False)),
            input_prompt=input_prompt,
            status=StageRunStatus.RUNNING.value,
            started_at=stage_started_at,
        )

        if updated_run is not None:
            await self.bus.publish("pipeline_run_updated", {"run": updated_run})
        await self.bus.publish("pipeline_stage_updated", {"stage_run": stage_run})

        if run_id in self._stopped_runs:
            now = utc_now_iso()
            stage_dur = int((time.monotonic() - stage_start_monotonic) * 1000)
            updated_sr = self.db.update_stage_run(
                stage_run["id"],
                status=StageRunStatus.STOPPED.value,
                finished_at=now,
                duration_ms=stage_dur,
            )
            if updated_sr is not None:
                await self.bus.publish(
                    "pipeline_stage_updated", {"stage_run": updated_sr}
                )
            await self._mark_run_stopped(run_id, run_start_monotonic)
            return updated_sr or stage_run, None, False

        self._active_stage_sessions[run_id] = session["id"]
        try:
            turn = await self.session_manager.run_turn_and_wait(
                session_id=session["id"],
                prompt=input_prompt,
                pipeline_run_id=run_id,
                stage_run_id=stage_run["id"],
            )
        except Exception as exc:
            self._active_stage_sessions.pop(run_id, None)
            now = utc_now_iso()
            stage_dur = int((time.monotonic() - stage_start_monotonic) * 1000)
            err_msg = str(exc)
            updated_sr = self.db.update_stage_run(
                stage_run["id"],
                status=StageRunStatus.FAILED.value,
                error_message=err_msg,
                finished_at=now,
                duration_ms=stage_dur,
            )
            if updated_sr is not None:
                await self.bus.publish(
                    "pipeline_stage_updated", {"stage_run": updated_sr}
                )
            await self._mark_run_failed(run_id, err_msg, run_start_monotonic)
            return updated_sr or stage_run, None, False
        finally:
            self._active_stage_sessions.pop(run_id, None)

        stage_finished_at = turn.get("finished_at") or utc_now_iso()
        stage_duration_ms = (
            turn.get("duration_ms")
            if turn.get("duration_ms") is not None
            else int((time.monotonic() - stage_start_monotonic) * 1000)
        )
        output_text = turn.get("response_text") or ""

        if (
            run_id in self._stopped_runs
            or turn.get("status") == TurnStatus.STOPPED.value
        ):
            updated_sr = self.db.update_stage_run(
                stage_run["id"],
                turn_id=turn["id"],
                output_text=output_text,
                status=StageRunStatus.STOPPED.value,
                finished_at=stage_finished_at,
                duration_ms=stage_duration_ms,
            )
            if updated_sr is not None:
                await self.bus.publish(
                    "pipeline_stage_updated", {"stage_run": updated_sr}
                )
            await self._mark_run_stopped(run_id, run_start_monotonic)
            return updated_sr or stage_run, turn, False

        if turn.get("status") != TurnStatus.COMPLETED.value:
            err_msg = (
                turn.get("error_message")
                or f"Stage '{stage['name']}' failed with status '{turn.get('status')}'"
            )
            updated_sr = self.db.update_stage_run(
                stage_run["id"],
                turn_id=turn["id"],
                output_text=output_text,
                status=StageRunStatus.FAILED.value,
                error_message=err_msg,
                finished_at=stage_finished_at,
                duration_ms=stage_duration_ms,
            )
            if updated_sr is not None:
                await self.bus.publish(
                    "pipeline_stage_updated", {"stage_run": updated_sr}
                )
            await self._mark_run_failed(run_id, err_msg, run_start_monotonic)
            return updated_sr or stage_run, turn, False

        stage_run["turn_id"] = turn["id"]
        return stage_run, turn, True

    async def _run_pipeline_loop(
        self, run_id: str, pipeline: Dict[str, Any]
    ) -> None:
        run_start_monotonic = time.monotonic()
        run = self.db.get_pipeline_run(run_id)
        if run is None:
            return

        stages: List[Dict[str, Any]] = pipeline.get("stages") or []
        max_review_loops = int(pipeline.get("max_review_loops", 2))
        task_prompt: str = run["task_prompt"]
        review_loop_count = 0
        pending_approval_note: Optional[str] = None

        prev_stage_name = ""
        prev_session_role = ""
        prev_stage_output = ""

        try:
            for idx, stage in enumerate(stages):
                if run_id in self._stopped_runs:
                    await self._mark_run_stopped(run_id, run_start_monotonic)
                    return

                session = self.db.get_session(stage["session_id"])
                if session is None:
                    await self._mark_run_failed(
                        run_id,
                        f"Session {stage['session_id']} not found",
                        run_start_monotonic,
                    )
                    return

                if idx == 0:
                    input_prompt = build_handoff_prompt(
                        case="A",
                        task_prompt=task_prompt,
                        approval_note=pending_approval_note,
                    )
                else:
                    input_prompt = build_handoff_prompt(
                        case="B",
                        task_prompt=task_prompt,
                        prev_stage_name=prev_stage_name,
                        prev_session_role=prev_session_role,
                        prev_stage_output=prev_stage_output,
                        stage_index=idx,
                        current_stage_name=stage["name"],
                        approval_note=pending_approval_note,
                    )
                pending_approval_note = None

                stage_run, turn, ok = await self._execute_stage_turn(
                    run_id=run_id,
                    stage=stage,
                    stage_order=idx,
                    session=session,
                    iteration=0,
                    input_prompt=input_prompt,
                    run_start_monotonic=run_start_monotonic,
                )
                if not ok or turn is None:
                    return

                output_text = turn.get("response_text") or ""
                stage_finished_at = turn.get("finished_at") or utc_now_iso()
                stage_duration_ms = turn.get("duration_ms")

                if session.get("role") == RoleId.REVIEWER.value:
                    while True:
                        verdict_obj = parse_reviewer_verdict(output_text)

                        if verdict_obj.verdict == "MISSING":
                            err_msg = (
                                "Reviewer reply did not contain a valid "
                                "'VERDICT: APPROVED' or 'VERDICT: CHANGES_REQUESTED' line."
                            )
                            updated_sr = self.db.update_stage_run(
                                stage_run["id"],
                                turn_id=turn["id"],
                                output_text=output_text,
                                verdict="MISSING",
                                status=StageRunStatus.FAILED.value,
                                error_message=err_msg,
                                finished_at=stage_finished_at,
                                duration_ms=stage_duration_ms,
                            )
                            if updated_sr is not None:
                                await self.bus.publish(
                                    "pipeline_stage_updated", {"stage_run": updated_sr}
                                )
                            await self._mark_run_failed(
                                run_id, err_msg, run_start_monotonic
                            )
                            return

                        if verdict_obj.verdict == "APPROVED":
                            updated_sr = self.db.update_stage_run(
                                stage_run["id"],
                                turn_id=turn["id"],
                                output_text=output_text,
                                verdict="APPROVED",
                                status=StageRunStatus.COMPLETED.value,
                                finished_at=stage_finished_at,
                                duration_ms=stage_duration_ms,
                            )
                            if updated_sr is not None:
                                await self.bus.publish(
                                    "pipeline_stage_updated", {"stage_run": updated_sr}
                                )

                            if stage.get("require_approval") and idx < len(stages) - 1:
                                pending_approval_note = await self._wait_for_approval(
                                    run_id, stage_run["id"]
                                )
                                if run_id in self._stopped_runs:
                                    await self._mark_run_stopped(
                                        run_id, run_start_monotonic
                                    )
                                    return

                            prev_stage_name = stage["name"]
                            prev_session_role = session["role"]
                            prev_stage_output = output_text
                            break

                        # verdict_obj.verdict == "CHANGES_REQUESTED"
                        updated_sr = self.db.update_stage_run(
                            stage_run["id"],
                            turn_id=turn["id"],
                            output_text=output_text,
                            verdict="CHANGES_REQUESTED",
                            findings=verdict_obj.findings_text,
                            status=StageRunStatus.CHANGES_REQUESTED.value,
                            finished_at=stage_finished_at,
                            duration_ms=stage_duration_ms,
                        )
                        if updated_sr is not None:
                            await self.bus.publish(
                                "pipeline_stage_updated", {"stage_run": updated_sr}
                            )

                        if review_loop_count >= max_review_loops:
                            err_msg = (
                                f"Maximum review loops ({max_review_loops}) reached; "
                                f"Reviewer still requested changes."
                            )
                            await self._mark_run_failed(
                                run_id, err_msg, run_start_monotonic
                            )
                            return

                        review_loop_count += 1
                        updated_run = self.db.update_pipeline_run(
                            run_id, review_loop_count=review_loop_count
                        )
                        if updated_run is not None:
                            await self.bus.publish(
                                "pipeline_run_updated", {"run": updated_run}
                            )

                        if stage.get("require_approval"):
                            pending_approval_note = await self._wait_for_approval(
                                run_id, stage_run["id"]
                            )
                            if run_id in self._stopped_runs:
                                await self._mark_run_stopped(
                                    run_id, run_start_monotonic
                                )
                                return

                        # Locate nearest preceding Implementer stage
                        imp_idx: Optional[int] = None
                        imp_stage: Optional[Dict[str, Any]] = None
                        imp_session: Optional[Dict[str, Any]] = None
                        for cand_idx in range(idx - 1, -1, -1):
                            cand_stage = stages[cand_idx]
                            cand_sess = self.db.get_session(cand_stage["session_id"])
                            if (
                                cand_sess is not None
                                and cand_sess.get("role") == RoleId.IMPLEMENTER.value
                            ):
                                imp_idx = cand_idx
                                imp_stage = cand_stage
                                imp_session = cand_sess
                                break

                        if imp_idx is None or imp_stage is None or imp_session is None:
                            await self._mark_run_failed(
                                run_id,
                                "No preceding Implementer stage found for rework loop.",
                                run_start_monotonic,
                            )
                            return

                        rework_prompt = build_handoff_prompt(
                            case="C",
                            loop_num=review_loop_count,
                            max_review_loops=max_review_loops,
                            findings_text=verdict_obj.findings_text,
                            reviewer_stage_output=output_text,
                            approval_note=pending_approval_note,
                        )
                        pending_approval_note = None

                        imp_sr, imp_turn, imp_ok = await self._execute_stage_turn(
                            run_id=run_id,
                            stage=imp_stage,
                            stage_order=imp_idx,
                            session=imp_session,
                            iteration=review_loop_count,
                            input_prompt=rework_prompt,
                            run_start_monotonic=run_start_monotonic,
                        )
                        if not imp_ok or imp_turn is None:
                            return

                        imp_output = imp_turn.get("response_text") or ""
                        imp_finished_at = imp_turn.get("finished_at") or utc_now_iso()
                        imp_duration_ms = imp_turn.get("duration_ms")

                        updated_imp_sr = self.db.update_stage_run(
                            imp_sr["id"],
                            turn_id=imp_turn["id"],
                            output_text=imp_output,
                            status=StageRunStatus.COMPLETED.value,
                            finished_at=imp_finished_at,
                            duration_ms=imp_duration_ms,
                        )
                        if updated_imp_sr is not None:
                            await self.bus.publish(
                                "pipeline_stage_updated",
                                {"stage_run": updated_imp_sr},
                            )

                        if imp_stage.get("require_approval"):
                            pending_approval_note = await self._wait_for_approval(
                                run_id, imp_sr["id"]
                            )
                            if run_id in self._stopped_runs:
                                await self._mark_run_stopped(
                                    run_id, run_start_monotonic
                                )
                                return

                        rereview_prompt = build_handoff_prompt(
                            case="D",
                            loop_num=review_loop_count,
                            max_review_loops=max_review_loops,
                            implementer_rework_output=imp_output,
                            approval_note=pending_approval_note,
                        )
                        pending_approval_note = None

                        stage_run, turn, rev_ok = await self._execute_stage_turn(
                            run_id=run_id,
                            stage=stage,
                            stage_order=idx,
                            session=session,
                            iteration=review_loop_count,
                            input_prompt=rereview_prompt,
                            run_start_monotonic=run_start_monotonic,
                        )
                        if not rev_ok or turn is None:
                            return

                        output_text = turn.get("response_text") or ""
                        stage_finished_at = turn.get("finished_at") or utc_now_iso()
                        stage_duration_ms = turn.get("duration_ms")
                else:
                    updated_sr = self.db.update_stage_run(
                        stage_run["id"],
                        turn_id=turn["id"],
                        output_text=output_text,
                        status=StageRunStatus.COMPLETED.value,
                        finished_at=stage_finished_at,
                        duration_ms=stage_duration_ms,
                    )
                    if updated_sr is not None:
                        await self.bus.publish(
                            "pipeline_stage_updated", {"stage_run": updated_sr}
                        )

                    if stage.get("require_approval") and idx < len(stages) - 1:
                        pending_approval_note = await self._wait_for_approval(
                            run_id, stage_run["id"]
                        )
                        if run_id in self._stopped_runs:
                            await self._mark_run_stopped(run_id, run_start_monotonic)
                            return

                    prev_stage_name = stage["name"]
                    prev_session_role = session["role"]
                    prev_stage_output = output_text

            if run_id in self._stopped_runs:
                await self._mark_run_stopped(run_id, run_start_monotonic)
                return

            now = utc_now_iso()
            duration_ms = int((time.monotonic() - run_start_monotonic) * 1000)
            completed_run = self.db.update_pipeline_run(
                run_id,
                status=PipelineRunStatus.COMPLETED.value,
                finished_at=now,
                duration_ms=duration_ms,
            )
            if completed_run is not None:
                await self.bus.publish(
                    "pipeline_run_updated", {"run": completed_run}
                )
        finally:
            self._approval_events.pop(run_id, None)
            self._waiting_stage_run_ids.pop(run_id, None)
            self._active_stage_sessions.pop(run_id, None)
            self._active_run_tasks.pop(run_id, None)
            self._stopped_runs.discard(run_id)

    async def approve_pipeline_run(
        self, run_id: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Resumes a pipeline run currently in 'waiting_approval' status."""
        run = self.db.get_pipeline_run(run_id)
        if run is None:
            raise NotFoundError(f"Pipeline run {run_id} not found")

        if run.get("status") != PipelineRunStatus.WAITING_APPROVAL.value:
            raise ConflictError(
                f"Pipeline run {run_id} is not waiting for approval (status={run.get('status')})"
            )

        cleaned_note = note.strip() if (note and note.strip()) else None
        self._approval_notes[run_id] = cleaned_note

        waiting_sr_id = self._waiting_stage_run_ids.get(run_id)
        if waiting_sr_id is not None:
            sr = self.db.get_stage_run(waiting_sr_id)
            restored_status = (
                StageRunStatus.CHANGES_REQUESTED.value
                if (sr is not None and sr.get("verdict") == "CHANGES_REQUESTED")
                else StageRunStatus.COMPLETED.value
            )
            updated_sr = self.db.update_stage_run(
                waiting_sr_id,
                status=restored_status,
                approval_note=cleaned_note,
            )
            if updated_sr is not None:
                await self.bus.publish(
                    "pipeline_stage_updated", {"stage_run": updated_sr}
                )

        updated_run = self.db.update_pipeline_run(
            run_id,
            status=PipelineRunStatus.RUNNING.value,
        )
        if updated_run is not None:
            await self.bus.publish("pipeline_run_updated", {"run": updated_run})

        event = self._approval_events.get(run_id)
        if event is not None:
            event.set()

        return {
            "run": updated_run or self.db.get_pipeline_run(run_id),
            "stage_runs": self.db.list_stage_runs(run_id),
        }

    async def stop_pipeline_run(self, run_id: str) -> Dict[str, Any]:
        """Stops a running or waiting_approval pipeline run and kills any active stage turn."""
        run = self.db.get_pipeline_run(run_id)
        if run is None:
            raise NotFoundError(f"Pipeline run {run_id} not found")

        if run.get("status") not in (
            PipelineRunStatus.RUNNING.value,
            PipelineRunStatus.WAITING_APPROVAL.value,
            PipelineRunStatus.PENDING.value,
        ):
            return {
                "run": run,
                "stage_runs": self.db.list_stage_runs(run_id),
            }

        task = self._active_run_tasks.get(run_id)
        # If the background task was just created and hasn't entered its first stage yet,
        # yield briefly so the initial stage subprocess can be registered and stopped cleanly.
        for _ in range(20):
            if (
                task is None
                or task.done()
                or run_id in self._active_stage_sessions
                or run_id in self._waiting_stage_run_ids
            ):
                break
            await asyncio.sleep(0.01)

        self._stopped_runs.add(run_id)

        waiting_sr_id = self._waiting_stage_run_ids.get(run_id)
        if waiting_sr_id is not None:
            updated_sr = self.db.update_stage_run(
                waiting_sr_id,
                status=StageRunStatus.STOPPED.value,
                finished_at=utc_now_iso(),
            )
            if updated_sr is not None:
                await self.bus.publish(
                    "pipeline_stage_updated", {"stage_run": updated_sr}
                )

        event = self._approval_events.get(run_id)
        if event is not None:
            event.set()

        active_sess_id = self._active_stage_sessions.get(run_id)
        if active_sess_id is not None:
            await self.session_manager.stop_session(active_sess_id)

        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

        current_run = self.db.get_pipeline_run(run_id)
        if (
            current_run is not None
            and current_run.get("status") != PipelineRunStatus.STOPPED.value
        ):
            now = utc_now_iso()
            current_run = self.db.update_pipeline_run(
                run_id,
                status=PipelineRunStatus.STOPPED.value,
                finished_at=current_run.get("finished_at") or now,
            )
            if current_run is not None:
                await self.bus.publish("pipeline_run_updated", {"run": current_run})

        return {
            "run": self.db.get_pipeline_run(run_id),
            "stage_runs": self.db.list_stage_runs(run_id),
        }
