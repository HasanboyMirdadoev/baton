from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from backend.config import AppConfig, load_config
from backend.db import Database, _UNSET
from backend.models import (
    AnswerTeamQuestionsRequest,
    AppRunContract,
    ConflictError,
    NotFoundError,
    PermissionLevel,
    ProviderId,
    ResumeTeamRunRequest,
    ReviewTeamMockupRequest,
    RoleId,
    SessionStatus,
    SignoffTeamRunRequest,
    StartTeamRunRequest,
    TeamMessageKind,
    TeamRoleId,
    TeamRunStatus,
    TeamSignoffAction,
    TeamStageState,
    TurnStatus,
    UnprocessableError,
    ValidationError,
    utc_now_iso,
)
from backend.pubsub import EventBus
from backend.roles import (
    DEFAULT_TEAM_MEMBERS,
    TEAM_ROLE_DEFINITIONS,
    TEAM_ROLE_IDS,
    get_role_label,
)
from backend.session_manager import SessionManager
from backend.team_protocol import (
    ALLOWED_ROLE_TRANSITIONS,
    ALLOWED_STAGE_TRANSITIONS,
    BATON_BROWSER_HELPER_SOURCE,
    MAX_EXCERPT_CHARS,
    MIN_MOCKUP_BYTES,
    REVIEW_DIFF_FILENAME,
    ParsedTeamMessage,
    build_format_repair_prompt,
    build_prompt_pm_mockup_review,
    build_prompt_t1,
    build_prompt_t1_pm_triage,
    build_prompt_t2,
    build_prompt_t2_user_answers_to_pm,
    build_prompt_t3,
    build_prompt_t3_pm_to_ui_designer,
    build_prompt_t4,
    build_prompt_t4_mockup_revision,
    build_prompt_t5,
    build_prompt_t5_pm_to_architect,
    build_prompt_t6,
    build_prompt_t6_specialist_question_to_pm,
    build_prompt_t7,
    build_prompt_t7_pm_answer_to_specialist,
    build_prompt_t8,
    build_prompt_t8_architect_to_planner,
    build_prompt_t9,
    build_prompt_t9_planner_to_implementer,
    build_prompt_t10,
    build_prompt_t10_checkers,
    build_prompt_t10_reviewer,
    build_prompt_t10_ui_checker,
    build_prompt_t11,
    build_prompt_t11_rework,
    build_prompt_t12,
    build_prompt_t12_pm_final_summary,
    cap_text,
    check_mockup_deliverable,
    check_mockup_exists,
    format_qa_history,
    get_expected_json_example,
    parse_team_message,
    read_architecture_artifact,
    read_baton_artifact,
    read_plan_artifact,
    read_requirements_artifact,
    resolve_app_run_placeholders,
    snapshot_mockup_version,
    validate_app_run_contract,
    validate_mockup_file,
    write_architecture_artifact,
    write_baton_artifact,
    write_browser_helper,
    write_plan_artifact,
    write_requirements_artifact,
)
from backend.worktree_manager import (
    allocate_safe_port,
    cleanup_port_processes,
    finalize_workspace,
    get_workspace_diff,
    prepare_workspace,
)


# ---------------------------------------------------------------------------
# State -> Handler Mapping (Section 3.3)
# ---------------------------------------------------------------------------
# - pm_triage               -> _handle_pm_triage
# - waiting_user_answers    -> (pauses loop; resumed by answer_questions)
# - ui_design               -> _handle_ui_design
# - pm_mockup_review        -> _handle_pm_mockup_review
# - waiting_mockup_approval -> (pauses loop; resumed by review_mockup)
# - architecture            -> _handle_architecture
# - planning                -> _handle_planning
# - implementing            -> _handle_implementing
# - pm_answer_relay         -> _handle_pm_answer_relay
# - checking                -> _handle_checking (parallel ui_checker + reviewer)
# - pm_final_summary        -> _handle_pm_final_summary
# - waiting_final_signoff   -> (pauses loop; resolved by signoff_run)
# - paused_limit            -> (pauses loop; resumed by resume_run)
# - interrupted             -> (startup crash recovery; resumed by resume_run)
# ---------------------------------------------------------------------------


def _int_or(value: Any, default: int) -> int:
    """int(value), or `default` only when value is missing (0 is a valid setting)."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _apply_resume_note(prompt: str, note: Optional[str]) -> str:
    if not note:
        return prompt
    return f"{prompt}\n\n## User Guidance on Resume\n{note}"


class TeamEngine:
    """Orchestrates the 7-agent Team Mode routing state machine."""

    def __init__(
        self,
        db_or_config: Union[Database, AppConfig],
        session_manager_or_db: Union[SessionManager, Database],
        bus_or_sm: Union[EventBus, SessionManager],
        config_or_bus: Optional[Union[AppConfig, EventBus]] = None,
    ) -> None:
        if isinstance(db_or_config, AppConfig):
            self.config: AppConfig = db_or_config
            self.db: Database = session_manager_or_db  # type: ignore[assignment]
            self.session_manager: SessionManager = bus_or_sm  # type: ignore[assignment]
            self.bus: EventBus = config_or_bus  # type: ignore[assignment]
        else:
            self.db = db_or_config
            self.session_manager = session_manager_or_db  # type: ignore[assignment]
            self.bus = bus_or_sm  # type: ignore[assignment]
            if isinstance(config_or_bus, AppConfig):
                self.config = config_or_bus
            else:
                self.config = getattr(self.session_manager, "config", None) or load_config()

        self._active_run_tasks: Dict[str, asyncio.Task[None]] = {}
        self._active_role_sessions: Dict[str, Set[str]] = {}
        self._stopped_runs: Set[str] = set()
        self._needs_ui_mockup_overrides: Dict[str, Optional[bool]] = {}
        # Runs whose loop task was still finishing when a user action asked to relaunch it.
        self._relaunch_requested: Set[str] = set()
        # Optional user guidance from POST /resume, appended to the next turn's prompt.
        self._resume_notes: Dict[str, str] = {}
        self._signoff_locks: Dict[str, asyncio.Lock] = {}
        self._shutting_down: bool = False
        self._stage_question_counts: Dict[str, int] = {}
        self._force_specialist_deliverable: Dict[str, bool] = {}

    async def start_team_run(
        self,
        team_id_or_request: Optional[Union[str, StartTeamRunRequest]] = None,
        request: Optional[StartTeamRunRequest] = None,
        *,
        task_prompt: Optional[str] = None,
        target_repo: Optional[str] = None,
        team_id: Optional[str] = None,
        auto_approve_mockup: bool = False,
        needs_ui_mockup_override: Optional[bool] = None,
        max_rework_loops: Optional[int] = None,
        max_question_rounds: Optional[int] = None,
        max_total_turns: Optional[int] = None,
        turn_timeout_sec: Optional[int] = None,
        run_timeout_sec: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Creates a team run, worktree, 7 role sessions, and starts the background state machine."""
        import uuid

        req_obj: Optional[StartTeamRunRequest] = None
        eff_team_id: Optional[str] = team_id
        if isinstance(team_id_or_request, StartTeamRunRequest):
            req_obj = team_id_or_request
            if req_obj.team_id:
                eff_team_id = req_obj.team_id
        elif isinstance(team_id_or_request, str):
            eff_team_id = team_id_or_request
            req_obj = request
        elif request is not None:
            req_obj = request
            if req_obj.team_id and not eff_team_id:
                eff_team_id = req_obj.team_id

        eff_task_prompt = (
            req_obj.task_prompt if req_obj is not None else (task_prompt or "")
        ).strip()
        eff_target_repo = (
            req_obj.target_repo if req_obj is not None else (target_repo or "")
        ).strip()

        if not eff_task_prompt:
            raise UnprocessableError("task_prompt is required")
        if not eff_target_repo:
            raise UnprocessableError("target_repo is required")

        if eff_team_id:
            team = self.db.get_team(eff_team_id)
            if team is None:
                raise NotFoundError(f"Team {eff_team_id} not found")
        else:
            team = self.db.get_default_team()
            if team is None:
                team = {
                    "id": None,
                    "name": "Default Engineering Team",
                    "max_rework_loops": 3,
                    "max_question_rounds": 4,
                    "max_total_turns": 30,
                    "turn_timeout_sec": 600,
                    "run_timeout_sec": 3600,
                    "auto_approve_mockup": False,
                    "members": list(DEFAULT_TEAM_MEMBERS),
                }
            eff_team_id = team.get("id")

        run_id = str(uuid.uuid4())

        active_ports: List[int] = [self.config.port]
        for existing_run in self.db.list_team_runs():
            if existing_run.get("status") in (
                TeamRunStatus.PENDING.value,
                TeamRunStatus.RUNNING.value,
                TeamRunStatus.WAITING_USER.value,
            ):
                p = existing_run.get("allocated_port")
                if isinstance(p, int):
                    active_ports.append(p)

        allocated_port = int(
            await asyncio.to_thread(
                allocate_safe_port,
                active_ports,
                app_port=self.config.port,
            )
        )

        ws = await asyncio.to_thread(
            prepare_workspace,
            run_id,
            eff_target_repo,
            self.config.data_dir,
        )
        try:
            return await self._create_run_records(
                run_id=run_id,
                ws=ws,
                team=team,
                req_obj=req_obj,
                eff_team_id=eff_team_id,
                eff_task_prompt=eff_task_prompt,
                allocated_port=allocated_port,
                auto_approve_mockup=auto_approve_mockup,
                needs_ui_mockup_override=needs_ui_mockup_override,
                max_rework_loops=max_rework_loops,
                max_question_rounds=max_question_rounds,
                max_total_turns=max_total_turns,
                turn_timeout_sec=turn_timeout_sec,
                run_timeout_sec=run_timeout_sec,
            )
        except BaseException:
            await self._rollback_failed_start(run_id, ws)
            raise

    async def _rollback_failed_start(self, run_id: str, ws: Dict[str, Any]) -> None:
        """Undoes a half-created run: sessions, the run row, and the worktree + branch."""
        self._needs_ui_mockup_overrides.pop(run_id, None)
        try:
            # Sessions carry team_run_id from creation, so this also finds ones not yet bound.
            for sess in self.db.list_sessions():
                if sess.get("team_run_id") == run_id:
                    self.db.delete_session(str(sess["id"]))
        except Exception:
            pass
        try:
            self.db.delete_team_run(run_id)
        except Exception:
            pass
        try:
            await asyncio.to_thread(
                finalize_workspace,
                {
                    "id": run_id,
                    "target_repo": ws.get("target_repo"),
                    "worktree_path": ws.get("worktree_path"),
                    "branch_name": ws.get("branch_name"),
                },
                "discard",
            )
        except Exception:
            pass

    async def _create_run_records(
        self,
        *,
        run_id: str,
        ws: Dict[str, Any],
        team: Dict[str, Any],
        req_obj: Optional[StartTeamRunRequest],
        eff_team_id: Optional[str],
        eff_task_prompt: str,
        allocated_port: int,
        auto_approve_mockup: bool,
        needs_ui_mockup_override: Optional[bool],
        max_rework_loops: Optional[int],
        max_question_rounds: Optional[int],
        max_total_turns: Optional[int],
        turn_timeout_sec: Optional[int],
        run_timeout_sec: Optional[int],
    ) -> Dict[str, Any]:
        eff_auto_approve = (
            bool(req_obj.auto_approve_mockup)
            if req_obj is not None
            else bool(auto_approve_mockup or team.get("auto_approve_mockup", False))
        )
        if req_obj is not None and not req_obj.auto_approve_mockup and team.get("auto_approve_mockup"):
            eff_auto_approve = True

        eff_override = (
            req_obj.needs_ui_mockup_override
            if req_obj is not None
            else needs_ui_mockup_override
        )
        eff_max_rework = (
            req_obj.max_rework_loops
            if (req_obj is not None and req_obj.max_rework_loops is not None)
            else (
                max_rework_loops
                if max_rework_loops is not None
                else int(team.get("max_rework_loops", 3))
            )
        )
        eff_max_questions = (
            req_obj.max_question_rounds
            if (req_obj is not None and req_obj.max_question_rounds is not None)
            else (
                max_question_rounds
                if max_question_rounds is not None
                else int(team.get("max_question_rounds", 4))
            )
        )
        eff_max_turns = (
            req_obj.max_total_turns
            if (req_obj is not None and req_obj.max_total_turns is not None)
            else (
                max_total_turns
                if max_total_turns is not None
                else int(team.get("max_total_turns", 30))
            )
        )
        eff_turn_timeout = (
            req_obj.turn_timeout_sec
            if (req_obj is not None and req_obj.turn_timeout_sec is not None)
            else (
                turn_timeout_sec
                if turn_timeout_sec is not None
                else int(team.get("turn_timeout_sec", 600))
            )
        )
        eff_run_timeout = (
            req_obj.run_timeout_sec
            if (req_obj is not None and req_obj.run_timeout_sec is not None)
            else (
                run_timeout_sec
                if run_timeout_sec is not None
                else int(team.get("run_timeout_sec", 3600))
            )
        )

        self._needs_ui_mockup_overrides[run_id] = eff_override
        run = self.db.create_team_run(
            run_id=run_id,
            task_prompt=eff_task_prompt,
            target_repo=str(ws["target_repo"]),
            team_id=eff_team_id,
            team_name=str(team.get("name") or "Default Engineering Team"),
            is_git_repo=bool(ws.get("is_git_repo", True)),
            base_branch=str(ws["base_branch"]),
            base_commit=str(ws["base_commit"]),
            worktree_path=str(ws["worktree_path"]),
            work_subdir=str(ws.get("work_subdir") or ""),
            branch_name=str(ws["branch_name"]),
            allocated_port=allocated_port,
            status=TeamRunStatus.RUNNING.value,
            stage_state=TeamStageState.PM_TRIAGE.value,
            needs_ui_mockup=eff_override,
            auto_approve_mockup=eff_auto_approve,
            max_rework_loops=eff_max_rework,
            max_question_rounds=eff_max_questions,
            max_total_turns=eff_max_turns,
            turn_timeout_sec=eff_turn_timeout,
            run_timeout_sec=eff_run_timeout,
        )

        members = self.normalize_team_members(team.get("members"))
        for m in members:
            raw_name = str(m.get("agent_name") or get_role_label(m["role"])).strip()
            sess_name = raw_name if raw_name.startswith("[Team]") else f"[Team] {raw_name}"
            sess = self.db.create_session(
                name=sess_name,
                provider=str(m["provider"]),
                model=str(m["model"]),
                role=str(m["role"]),
                custom_prompt=str(m.get("custom_prompt") or ""),
                permission_level=str(m["permission_level"]),
                working_dir=str(ws.get("work_dir") or ws["worktree_path"]),
                team_run_id=run_id,
            )
            self.db.bind_team_run_session(run_id, str(m["role"]), sess["id"])
            await self.bus.publish("session_updated", {"session": sess})

        await self.bus.publish("team_run_updated", {"run": run})
        self._launch_run_loop(run_id)
        return self.get_run_detail(run_id)

    async def answer_questions(
        self,
        run_id: str,
        answers_or_request: Union[Sequence[str], AnswerTeamQuestionsRequest],
    ) -> Dict[str, Any]:
        """Touchpoint 1: Submits user answers to pending PM questions and resumes the state machine."""
        run = self.db.get_team_run(run_id)
        if run is None:
            raise NotFoundError(f"Team run {run_id} not found")

        if (
            run.get("status") != TeamRunStatus.WAITING_USER.value
            or run.get("stage_state") != TeamStageState.WAITING_USER_ANSWERS.value
        ):
            raise ConflictError(
                f"Team run {run_id} is not waiting for user answers "
                f"(status={run.get('status')}, stage_state={run.get('stage_state')})"
            )

        raw_answers = (
            answers_or_request.answers
            if isinstance(answers_or_request, AnswerTeamQuestionsRequest)
            else list(answers_or_request or [])
        )
        cleaned_answers = [str(a).strip() for a in raw_answers if str(a).strip()]
        if not cleaned_answers:
            raise UnprocessableError("At least one non-empty answer is required.")

        pending_q = list(run.get("pending_questions") or [])
        asking_role = run.get("asking_role")
        qa_history = list(run.get("qa_history") or [])
        qa_entry = {
            "round": len(qa_history) + 1,
            "from": asking_role or RoleId.PM.value,
            "questions": pending_q,
            "answers": cleaned_answers,
        }
        qa_history.append(qa_entry)

        await self._record_message(
            run_id=run_id,
            from_role="user",
            to_role=RoleId.PM.value,
            kind=TeamMessageKind.USER_ACTION.value,
            stage_state=TeamStageState.WAITING_USER_ANSWERS.value,
            summary=f"User answered {len(cleaned_answers)} question(s)",
            content_text="\n".join(f"- {a}" for a in cleaned_answers),
            payload={"questions": pending_q, "answers": cleaned_answers},
        )

        self._stage_question_counts[run_id] = 0
        self._force_specialist_deliverable.pop(run_id, None)
        self._set_pending_prompt(run_id, build_prompt_t2_user_answers_to_pm(
            questions=pending_q,
            answers=cleaned_answers,
            asking_role=asking_role,
        ))

        next_stage = (
            TeamStageState.PM_TRIAGE.value
            if not asking_role
            else TeamStageState.PM_ANSWER_RELAY.value
        )
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=next_stage,
            pending_questions=[],
            qa_history=qa_history,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
            await self.bus.publish(
                "team_inbox_updated",
                {"run_id": run_id, "stage_state": next_stage, "run": updated},
            )

        self._launch_run_loop(run_id)
        return self.get_run_detail(run_id)

    async def review_mockup(
        self,
        run_id: str,
        approved_or_request: Union[bool, ReviewTeamMockupRequest],
        feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Touchpoint 2: Approves or requests changes on the UI Designer's HTML mockup."""
        run = self.db.get_team_run(run_id)
        if run is None:
            raise NotFoundError(f"Team run {run_id} not found")

        if (
            run.get("status") != TeamRunStatus.WAITING_USER.value
            or run.get("stage_state") != TeamStageState.WAITING_MOCKUP_APPROVAL.value
        ):
            raise ConflictError(
                f"Team run {run_id} is not waiting for mockup approval "
                f"(status={run.get('status')}, stage_state={run.get('stage_state')})"
            )

        if isinstance(approved_or_request, ReviewTeamMockupRequest):
            approved = bool(approved_or_request.approved)
            raw_feedback = approved_or_request.feedback
        else:
            approved = bool(approved_or_request)
            raw_feedback = feedback

        cleaned_feedback = (raw_feedback or "").strip()
        if not approved and not cleaned_feedback:
            raise UnprocessableError(
                "Feedback is required when requesting mockup changes."
            )

        mockup_version = max(1, int(run.get("mockup_version") or 1))
        await self._record_message(
            run_id=run_id,
            from_role="user",
            to_role=RoleId.PM.value,
            kind=TeamMessageKind.USER_ACTION.value,
            stage_state=TeamStageState.WAITING_MOCKUP_APPROVAL.value,
            summary=(
                f"User approved UI mockup (v{mockup_version})"
                if approved
                else f"User requested changes on UI mockup (v{mockup_version})"
            ),
            content_text=cleaned_feedback,
            payload={"approved": approved, "feedback": cleaned_feedback},
        )

        if approved:
            next_stage = TeamStageState.ARCHITECTURE.value
            updated = self.db.update_team_run(
                run_id,
                status=TeamRunStatus.RUNNING.value,
                stage_state=next_stage,
                mockup_approved=True,
            )
        else:
            next_stage = TeamStageState.UI_DESIGN.value
            self._set_pending_prompt(run_id, build_prompt_t4_mockup_revision(
                mockup_version=mockup_version,
                feedback_source="User (relayed via PM)",
                feedback_text=cleaned_feedback,
            ))
            updated = self.db.update_team_run(
                run_id,
                status=TeamRunStatus.RUNNING.value,
                stage_state=next_stage,
                mockup_approved=False,
                last_findings=[cleaned_feedback],
            )

        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
            await self.bus.publish(
                "team_inbox_updated",
                {"run_id": run_id, "stage_state": next_stage, "run": updated},
            )

        self._launch_run_loop(run_id)
        return self.get_run_detail(run_id)

    async def resume_run(
        self,
        run_id: str,
        request: Optional[ResumeTeamRunRequest] = None,
        *,
        add_rework_loops: int = 0,
        add_question_rounds: int = 0,
        add_turns: int = 0,
        add_timeout_sec: int = 0,
        force_signoff: bool = False,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resumes a run paused at a limit or interrupted by server restart."""
        run = self.db.get_team_run(run_id)
        if run is None:
            raise NotFoundError(f"Team run {run_id} not found")

        cur_stage = str(run.get("stage_state") or "")
        if cur_stage not in (
            TeamStageState.PAUSED_LIMIT.value,
            TeamStageState.INTERRUPTED.value,
        ):
            raise ConflictError(
                f"Team run {run_id} cannot be resumed from stage_state '{cur_stage}'"
            )

        if request is not None:
            eff_add_rework = int(request.add_rework_loops or 0)
            eff_add_q = int(request.add_question_rounds or 0)
            eff_add_turns = int(request.add_turns or 0)
            eff_add_timeout = int(request.add_timeout_sec or 0)
            eff_force_signoff = bool(request.force_signoff)
            eff_note = (request.note or "").strip()
        else:
            eff_add_rework = int(add_rework_loops or 0)
            eff_add_q = int(add_question_rounds or 0)
            eff_add_turns = int(add_turns or 0)
            eff_add_timeout = int(add_timeout_sec or 0)
            eff_force_signoff = bool(force_signoff)
            eff_note = (note or "").strip()

        cur_rework = int(run.get("rework_loop_count") or 0)
        new_max_rework = _int_or(run.get("max_rework_loops"), 3) + max(0, eff_add_rework)
        new_max_q = int(run.get("max_question_rounds") or 4) + max(0, eff_add_q)
        cur_turns = int(run.get("turn_count") or 0)
        new_max_turns = int(run.get("max_total_turns") or 30) + max(0, eff_add_turns)
        if cur_turns >= new_max_turns:
            new_max_turns = cur_turns + (2 if eff_force_signoff else 5)

        cur_elapsed_sec = int((int(run.get("active_elapsed_ms") or 0)) / 1000)
        new_run_timeout = int(run.get("run_timeout_sec") or 3600) + max(0, eff_add_timeout)
        if cur_elapsed_sec >= new_run_timeout:
            new_run_timeout = cur_elapsed_sec + 600

        if eff_force_signoff:
            target_stage = TeamStageState.PM_FINAL_SUMMARY.value
            self._clear_pending_prompt(run_id)
            new_rework_count = cur_rework
        else:
            interrupted_stage = str(
                run.get("interrupted_stage_state") or TeamStageState.PM_TRIAGE.value
            )
            if interrupted_stage in (
                TeamStageState.PAUSED_LIMIT.value,
                TeamStageState.INTERRUPTED.value,
                "",
            ):
                target_stage = TeamStageState.PM_TRIAGE.value
            else:
                target_stage = interrupted_stage

            old_max_rework = _int_or(run.get("max_rework_loops"), 3)
            new_rework_count = cur_rework
            if (
                target_stage == TeamStageState.IMPLEMENTING.value
                and run.get("last_findings")
            ):
                if cur_rework >= old_max_rework:
                    new_rework_count = cur_rework + 1
                    if new_rework_count > new_max_rework:
                        new_max_rework = new_rework_count
                if not self._peek_pending_prompt(run_id):
                    ui_v = str(run.get("ui_check_verdict") or "FAILED")
                    code_v = str(run.get("code_review_verdict") or "FAILED")
                    findings = list(run.get("last_findings") or [])
                    self._set_pending_prompt(run_id, build_prompt_t11_rework(
                        rework_loop_count=max(1, new_rework_count),
                        max_rework_loops=new_max_rework,
                        ui_verdict=ui_v,
                        ui_findings=findings if ui_v != "APPROVED" else None,
                        code_verdict=code_v,
                        code_findings=findings if code_v != "APPROVED" else None,
                    ))

        if eff_note:
            # Applied to whichever prompt the next turn uses (persisted or default).
            self._resume_notes[run_id] = eff_note
        else:
            self._resume_notes.pop(run_id, None)

        await self._record_message(
            run_id=run_id,
            from_role="user",
            to_role=RoleId.PM.value,
            kind=TeamMessageKind.USER_ACTION.value,
            stage_state=cur_stage,
            summary=(
                "User forced progression to final sign-off summary"
                if eff_force_signoff
                else f"User resumed team run at '{target_stage}'"
            ),
            content_text=eff_note,
            payload={
                "add_rework_loops": eff_add_rework,
                "add_question_rounds": eff_add_q,
                "add_turns": eff_add_turns,
                "add_timeout_sec": eff_add_timeout,
                "force_signoff": eff_force_signoff,
                "note": eff_note,
            },
        )

        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=target_stage,
            interrupted_stage_state=None,
            error_message=None,
            rework_loop_count=new_rework_count,
            max_rework_loops=new_max_rework,
            max_question_rounds=new_max_q,
            max_total_turns=new_max_turns,
            run_timeout_sec=new_run_timeout,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
            await self.bus.publish(
                "team_inbox_updated",
                {"run_id": run_id, "stage_state": target_stage, "run": updated},
            )

        self._launch_run_loop(run_id)
        return self.get_run_detail(run_id)

    async def signoff_run(
        self,
        run_id: str,
        action_or_request: Union[str, TeamSignoffAction, SignoffTeamRunRequest],
        commit_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Touchpoint 3: Finalizes the workspace (merge, keep_branch, or discard) and completes the run."""
        lock = self._signoff_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise ConflictError(f"Sign-off for team run {run_id} is already in progress.")
        async with lock:
            return await self._signoff_run_locked(run_id, action_or_request, commit_message)

    async def _signoff_run_locked(
        self,
        run_id: str,
        action_or_request: Union[str, TeamSignoffAction, SignoffTeamRunRequest],
        commit_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        run = self.db.get_team_run(run_id)
        if run is None:
            raise NotFoundError(f"Team run {run_id} not found")

        if run.get("resolution"):
            raise ConflictError(
                f"Team run {run_id} was already signed off ({run.get('resolution')})."
            )
        task = self._active_run_tasks.get(run_id)
        if task is not None and not task.done():
            raise ConflictError(
                f"Team run {run_id} still has an agent turn finishing; "
                "wait for it to end (or click Stop) before signing off."
            )

        cur_status = str(run.get("status") or "")
        cur_stage = str(run.get("stage_state") or "")
        allowed_stages = (
            TeamStageState.WAITING_FINAL_SIGNOFF.value,
            TeamStageState.PAUSED_LIMIT.value,
            TeamStageState.INTERRUPTED.value,
            TeamStageState.STOPPED.value,
            TeamStageState.FAILED.value,
        )
        if cur_stage not in allowed_stages and cur_status not in (
            TeamRunStatus.STOPPED.value,
            TeamRunStatus.FAILED.value,
        ):
            raise ConflictError(
                f"Team run {run_id} is not ready for sign-off "
                f"(status={cur_status}, stage_state={cur_stage})"
            )

        if isinstance(action_or_request, SignoffTeamRunRequest):
            raw_action = action_or_request.action
            eff_commit_msg = (
                action_or_request.commit_message
                if action_or_request.commit_message is not None
                else commit_message
            )
        else:
            raw_action = action_or_request  # type: ignore[assignment]
            eff_commit_msg = commit_message

        eff_action = (
            raw_action.value
            if hasattr(raw_action, "value")
            else str(raw_action or "").strip().lower()
        )
        if eff_action not in (
            TeamSignoffAction.MERGE.value,
            TeamSignoffAction.KEEP_BRANCH.value,
            TeamSignoffAction.DISCARD.value,
        ):
            raise UnprocessableError(
                f"Invalid sign-off action '{eff_action}'. Must be 'merge', 'keep_branch', or 'discard'."
            )

        ws_res = await asyncio.to_thread(
            finalize_workspace,
            run,
            eff_action,
            eff_commit_msg,
        )
        resolution = str(ws_res.get("resolution") or eff_action)

        await self._record_message(
            run_id=run_id,
            from_role="user",
            to_role=RoleId.PM.value,
            kind=TeamMessageKind.USER_ACTION.value,
            stage_state=cur_stage,
            summary=f"User signed off team run: {eff_action} ({resolution})",
            content_text=eff_commit_msg or "",
            payload={
                "action": eff_action,
                "resolution": resolution,
                "commit_message": eff_commit_msg,
            },
        )

        now = utc_now_iso()
        elapsed_ms = int(run.get("active_elapsed_ms") or 0)
        if cur_status in (TeamRunStatus.STOPPED.value, TeamRunStatus.FAILED.value):
            next_status = cur_status
            next_stage = cur_stage
        else:
            next_status = TeamRunStatus.COMPLETED.value
            next_stage = TeamStageState.COMPLETED.value

        updated = self.db.update_team_run(
            run_id,
            status=next_status,
            stage_state=next_stage,
            resolution=resolution,
            finished_at=run.get("finished_at") or now,
            duration_ms=run.get("duration_ms") or elapsed_ms,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
            await self.bus.publish(
                "team_inbox_updated",
                {"run_id": run_id, "stage_state": next_stage, "run": updated},
            )

        return self.get_run_detail(run_id)

    async def stop_run(self, run_id: str) -> Dict[str, Any]:
        """Stops all active role turns for a team run, cleans up checker processes, and marks it stopped."""
        run = self.db.get_team_run(run_id)
        if run is None:
            raise NotFoundError(f"Team run {run_id} not found")

        if run.get("status") in (
            TeamRunStatus.COMPLETED.value,
            TeamRunStatus.STOPPED.value,
            TeamRunStatus.FAILED.value,
        ):
            return self.get_run_detail(run_id)

        task = self._active_run_tasks.get(run_id)
        for _ in range(20):
            if (
                task is None
                or task.done()
                or bool(self._active_role_sessions.get(run_id))
            ):
                break
            await asyncio.sleep(0.01)

        self._stopped_runs.add(run_id)
        self._relaunch_requested.discard(run_id)
        self._resume_notes.pop(run_id, None)

        active_ids = set(self._active_role_sessions.get(run_id, set()))
        for binding in self.db.get_team_run_sessions(run_id):
            sid = binding.get("session_id")
            if sid and self.session_manager.is_session_running(sid):
                active_ids.add(sid)

        for sid in active_ids:
            await self.session_manager.stop_session(sid)

        await self._cleanup_checker_processes(
            run.get("worktree_path") or "",
            int(run.get("allocated_port") or 18100),
        )

        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

        cur_run = self.db.get_team_run(run_id)
        if cur_run is not None and cur_run.get("status") != TeamRunStatus.STOPPED.value:
            await self._mark_run_stopped(run_id)
            await self._publish_inbox_updated(run_id)

        self._stopped_runs.discard(run_id)
        return self.get_run_detail(run_id)

    stop_team_run = stop_run

    def _launch_run_loop(self, run_id: str) -> asyncio.Task[None]:
        existing = self._active_run_tasks.get(run_id)
        if existing is not None and not existing.done():
            # The previous loop may be finishing a stage (e.g. the second checker of a
            # checking pass whose first checker already paused the run). It will exit
            # without seeing this resume, so ask it to start a fresh loop when it ends.
            self._relaunch_requested.add(run_id)
            return existing
        task: asyncio.Task[None] = asyncio.create_task(
            self._run_state_machine_loop(run_id)
        )
        self._active_run_tasks[run_id] = task
        return task

    async def _run_state_machine_loop(self, run_id: str) -> None:
        handlers = {
            TeamStageState.PM_TRIAGE.value: self._handle_pm_triage,
            TeamStageState.UI_DESIGN.value: self._handle_ui_design,
            TeamStageState.PM_MOCKUP_REVIEW.value: self._handle_pm_mockup_review,
            TeamStageState.ARCHITECTURE.value: self._handle_architecture,
            TeamStageState.PLANNING.value: self._handle_planning,
            TeamStageState.IMPLEMENTING.value: self._handle_implementing,
            TeamStageState.PM_ANSWER_RELAY.value: self._handle_pm_answer_relay,
            TeamStageState.CHECKING.value: self._handle_checking,
            TeamStageState.PM_FINAL_SUMMARY.value: self._handle_pm_final_summary,
        }
        try:
            while True:
                if self._shutting_down:
                    break
                if run_id in self._stopped_runs:
                    await self._mark_run_stopped(run_id)
                    break

                run = self.db.get_team_run(run_id)
                if run is None:
                    break

                if run.get("status") != TeamRunStatus.RUNNING.value:
                    break

                stage_state = str(run.get("stage_state") or "")
                handler = handlers.get(stage_state)
                if handler is None:
                    break

                keep_going = await handler(run_id)
                if not keep_going:
                    break
        except asyncio.CancelledError:
            if not self._shutting_down:
                await self._mark_run_stopped(run_id)
            raise
        except Exception as exc:
            await self._mark_run_failed(
                run_id, f"Unexpected team engine error: {exc}"
            )
        finally:
            current = asyncio.current_task()
            if self._active_run_tasks.get(run_id) is current:
                self._active_run_tasks.pop(run_id, None)
            self._active_role_sessions.pop(run_id, None)
            self._stopped_runs.discard(run_id)
            if run_id in self._relaunch_requested:
                self._relaunch_requested.discard(run_id)
                latest = self.db.get_team_run(run_id)
                if (
                    not self._shutting_down
                    and latest is not None
                    and latest.get("status") == TeamRunStatus.RUNNING.value
                ):
                    self._launch_run_loop(run_id)

    def get_run_detail(self, run_id: str) -> Dict[str, Any]:
        """Returns {'run': ..., 'sessions': [...], 'messages': [...]} for a team run."""
        run = self.db.get_team_run(run_id)
        if run is None:
            raise NotFoundError(f"Team run {run_id} not found")
        sessions = self.db.get_team_run_sessions(run_id)
        messages = self.db.list_team_messages(run_id)
        return {
            "run": run,
            "sessions": sessions,
            "messages": messages,
        }

    get_team_run_detail = get_run_detail

    def list_team_runs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Lists all team runs ordered by started_at DESC."""
        return self.db.list_team_runs(limit=limit)

    def get_mockup_path(self, run_id: str, version: Optional[int] = None) -> Path:
        """Resolves and validates the path to .baton/mockup.html (or mockup_v{version}.html)."""
        run = self.db.get_team_run(run_id)
        if run is None:
            raise NotFoundError(f"Team run {run_id} not found")

        wt_str = str(run.get("work_dir") or run.get("worktree_path") or "").strip()
        if not wt_str:
            raise NotFoundError(f"No workspace found for team run {run_id}")

        wt_root = Path(wt_str).resolve()
        baton_dir = (wt_root / ".baton").resolve()
        if version is not None:
            filename = f"mockup_v{int(version)}.html"
        else:
            filename = "mockup.html"

        candidate = (baton_dir / filename).resolve()
        if candidate.parent != baton_dir:
            raise ValidationError("Invalid mockup file path")

        if not candidate.exists() or not candidate.is_file():
            # Fallback to mockup.html if versioned snapshot wasn't found but mockup.html exists
            fallback = (baton_dir / "mockup.html").resolve()
            if (
                version is not None
                and int(version) == int(run.get("mockup_version") or 0)
                and fallback.exists()
                and fallback.is_file()
            ):
                return fallback
            raise NotFoundError(f"Mockup file not found for team run {run_id}")

        return candidate

    def get_mockup_html(self, run_id: str, version: Optional[int] = None) -> str:
        """Reads and returns the HTML content of the run's mockup."""
        path = self.get_mockup_path(run_id, version=version)
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise NotFoundError(f"Unable to read mockup file: {exc}")

    async def get_run_diff(self, run_id: str) -> Dict[str, Any]:
        """Computes and returns the current workspace git diff against base_commit."""
        run = self.db.get_team_run(run_id)
        if run is None:
            raise NotFoundError(f"Team run {run_id} not found")

        diff_res = await asyncio.to_thread(
            get_workspace_diff,
            run.get("work_dir") or run.get("worktree_path") or "",
            run.get("base_commit") or "HEAD",
            run_id=run_id,
            base_branch=run.get("base_branch"),
            branch_name=run.get("branch_name"),
        )
        return dict(diff_res)

    get_workspace_diff = get_run_diff

    @staticmethod
    def normalize_team_members(
        members: Optional[Sequence[Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Ensures all 7 required team roles are present, filling any omitted roles from defaults."""
        by_role: Dict[str, Dict[str, Any]] = {}
        for default_m in DEFAULT_TEAM_MEMBERS:
            r_key = str(default_m["role"]).strip().lower()
            by_role[r_key] = dict(default_m)

        if members:
            for item in members:
                if isinstance(item, dict):
                    raw_role = item.get("role")
                    role_str = (
                        raw_role.value
                        if hasattr(raw_role, "value")
                        else str(raw_role or "").strip().lower()
                    )
                    if role_str not in by_role:
                        raise ValidationError(f"Unsupported team role: '{role_str}'")
                    prov = item.get("provider")
                    prov_str = (
                        prov.value
                        if hasattr(prov, "value")
                        else str(prov or by_role[role_str]["provider"]).strip().lower()
                    )
                    perm = item.get("permission_level")
                    perm_str = (
                        perm.value
                        if hasattr(perm, "value")
                        else str(
                            perm or by_role[role_str]["permission_level"]
                        ).strip().lower()
                    )
                    by_role[role_str] = {
                        "role": role_str,
                        "agent_name": str(
                            item.get("agent_name") or by_role[role_str]["agent_name"]
                        ).strip(),
                        "provider": prov_str,
                        "model": str(
                            item.get("model") or by_role[role_str]["model"]
                        ).strip(),
                        "permission_level": perm_str,
                        "custom_prompt": str(item.get("custom_prompt") or ""),
                    }
                else:
                    role_val = getattr(item, "role", None)
                    role_str = (
                        role_val.value
                        if hasattr(role_val, "value")
                        else str(role_val or "").strip().lower()
                    )
                    if role_str not in by_role:
                        raise ValidationError(f"Unsupported team role: '{role_str}'")
                    prov_val = getattr(item, "provider", None)
                    prov_str = (
                        prov_val.value
                        if hasattr(prov_val, "value")
                        else str(prov_val or by_role[role_str]["provider"]).strip().lower()
                    )
                    perm_val = getattr(item, "permission_level", None)
                    perm_str = (
                        perm_val.value
                        if hasattr(perm_val, "value")
                        else str(
                            perm_val or by_role[role_str]["permission_level"]
                        ).strip().lower()
                    )
                    by_role[role_str] = {
                        "role": role_str,
                        "agent_name": str(
                            getattr(item, "agent_name", "")
                            or by_role[role_str]["agent_name"]
                        ).strip(),
                        "provider": prov_str,
                        "model": str(
                            getattr(item, "model", "") or by_role[role_str]["model"]
                        ).strip(),
                        "permission_level": perm_str,
                        "custom_prompt": str(getattr(item, "custom_prompt", "") or ""),
                    }

        ordered: List[Dict[str, Any]] = []
        for role_enum in TEAM_ROLE_IDS:
            ordered.append(by_role[role_enum.value])
        return ordered

    # -----------------------------------------------------------------------
    # Internal Event, Message & Lifecycle Helpers
    # -----------------------------------------------------------------------

    async def _publish_run_updated(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.db.get_team_run(run_id)
        if run is not None:
            await self.bus.publish("team_run_updated", {"run": run})
        return run

    async def _publish_inbox_updated(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.db.get_team_run(run_id)
        if run is not None:
            await self.bus.publish(
                "team_inbox_updated",
                {
                    "run_id": run_id,
                    "stage_state": run.get("stage_state"),
                    "run": run,
                },
            )
        return run

    # -----------------------------------------------------------------------
    # Persisted next-stage prompt (survives turn failures and server restarts)
    # -----------------------------------------------------------------------

    def _peek_pending_prompt(self, run_id: str) -> Optional[str]:
        return self.db.get_team_run_pending_prompt(run_id)

    def _set_pending_prompt(self, run_id: str, prompt: str) -> None:
        self.db.update_team_run(run_id, pending_prompt=prompt)

    def _clear_pending_prompt(self, run_id: str) -> None:
        self.db.update_team_run(run_id, pending_prompt=None)

    def shutdown(self) -> None:
        """
        Called before the server stops its CLI subprocesses. Active runs are then left in
        status 'running' so startup recovery turns them into resumable 'interrupted' runs,
        instead of being marked 'stopped' as if the user had clicked Stop.
        """
        self._shutting_down = True

    async def _record_message(
        self,
        run_id: str,
        from_role: str,
        to_role: str,
        kind: str,
        stage_state: str,
        summary: str = "",
        content_text: str = "",
        payload: Optional[Dict[str, Any]] = None,
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        msg = self.db.create_team_message(
            team_run_id=run_id,
            from_role=from_role,
            to_role=to_role,
            kind=kind,
            stage_state=stage_state,
            summary=summary,
            content_text=content_text,
            payload=payload or {},
            turn_id=turn_id,
        )
        await self.bus.publish(
            "team_message_created",
            {"run_id": run_id, "message": msg},
        )
        return msg

    async def _cleanup_checker_processes(
        self, worktree_path: Union[str, Path], allocated_port: int
    ) -> List[int]:
        return await asyncio.to_thread(
            cleanup_port_processes,
            worktree_path,
            allocated_port,
        )

    async def _transition_to_waiting_user(
        self,
        run_id: str,
        stage_state: Union[TeamStageState, str],
        **extra_updates: Any,
    ) -> Optional[Dict[str, Any]]:
        stage_str = (
            stage_state.value if isinstance(stage_state, TeamStageState) else str(stage_state)
        )
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.WAITING_USER.value,
            stage_state=stage_str,
            **extra_updates,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
            await self.bus.publish(
                "team_inbox_updated",
                {"run_id": run_id, "stage_state": stage_str, "run": updated},
            )
        return updated

    async def _mark_run_paused_limit(
        self,
        run_id: str,
        interrupted_stage: str,
        reason: str,
        **extra_updates: Any,
    ) -> Optional[Dict[str, Any]]:
        return await self._transition_to_waiting_user(
            run_id,
            TeamStageState.PAUSED_LIMIT.value,
            interrupted_stage_state=interrupted_stage,
            error_message=reason,
            **extra_updates,
        )

    async def _mark_run_failed(
        self, run_id: str, error_message: str
    ) -> Optional[Dict[str, Any]]:
        run = self.db.get_team_run(run_id)
        now = utc_now_iso()
        duration_ms = int(run.get("active_elapsed_ms") or 0) if run else 0
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.FAILED.value,
            stage_state=TeamStageState.FAILED.value,
            error_message=error_message,
            finished_at=now,
            duration_ms=duration_ms,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return updated

    async def _mark_run_stopped(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.db.get_team_run(run_id)
        now = utc_now_iso()
        duration_ms = int(run.get("active_elapsed_ms") or 0) if run else 0
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.STOPPED.value,
            stage_state=TeamStageState.STOPPED.value,
            finished_at=run.get("finished_at") or now if run else now,
            duration_ms=duration_ms,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return updated

    def _validate_parsed_for_run(
        self,
        run: Dict[str, Any],
        role_str: str,
        stage_str: str,
        reply_text: str,
    ) -> ParsedTeamMessage:
        asking_role = run.get("asking_role")
        parsed = parse_team_message(
            reply_text,
            sender_role=role_str,
            stage_state=stage_str,
            asking_role=asking_role,
        )
        if not parsed.valid:
            return parsed

        run_id = str(run["id"])
        override = self._needs_ui_mockup_overrides.get(run_id)
        if (
            role_str == RoleId.PM.value
            and stage_str == TeamStageState.PM_TRIAGE.value
            and parsed.kind == TeamMessageKind.DELIVERABLE.value
            and override is not None
        ):
            parsed.needs_ui_mockup = bool(override)
            parsed.to = (
                RoleId.UI_DESIGNER.value
                if override
                else RoleId.ARCHITECT.value
            )
            parsed.raw_dict["needs_ui_mockup"] = bool(override)
            parsed.raw_dict["to"] = parsed.to

        if (
            role_str == RoleId.UI_DESIGNER.value
            and parsed.kind == TeamMessageKind.DELIVERABLE.value
        ):
            wt_path = str(run.get("work_dir") or run.get("worktree_path") or "")
            mockup_rel = parsed.mockup_path or ".baton/mockup.html"
            ok, err = check_mockup_deliverable(wt_path, mockup_rel_path=mockup_rel)
            if not ok:
                parsed.valid = False
                parsed.error = err or "Mockup file '.baton/mockup.html' is missing or empty."
                return parsed

        if (
            self._force_specialist_deliverable.get(run_id)
            and role_str in (
                RoleId.UI_DESIGNER.value,
                RoleId.ARCHITECT.value,
                RoleId.PLANNER.value,
                RoleId.IMPLEMENTER.value,
            )
            and parsed.kind == TeamMessageKind.QUESTION.value
        ):
            parsed.valid = False
            parsed.error = (
                f"Maximum question rounds ({run.get('max_question_rounds', 4)}) reached for this stage; "
                "you must emit a 'deliverable' block."
            )
            return parsed

        return parsed

    async def _execute_single_session_turn(
        self,
        run_id: str,
        role_str: str,
        stage_str: str,
        prompt: str,
        record_elapsed: bool = True,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        if run_id in self._stopped_runs:
            await self._mark_run_stopped(run_id)
            return None, False

        run = self.db.get_team_run(run_id)
        if run is None:
            return None, False

        turn_count = int(run.get("turn_count") or 0)
        max_turns = int(run.get("max_total_turns") or 30)
        if turn_count >= max_turns:
            await self._mark_run_paused_limit(
                run_id,
                stage_str,
                f"Maximum total turns ({max_turns}) reached before starting '{role_str}' turn.",
            )
            return None, False

        elapsed_ms = int(run.get("active_elapsed_ms") or 0)
        run_timeout_sec = int(run.get("run_timeout_sec") or 3600)
        if elapsed_ms >= run_timeout_sec * 1000:
            await self._mark_run_paused_limit(
                run_id,
                stage_str,
                f"Team run timeout ({run_timeout_sec}s) reached.",
            )
            return None, False

        session_id = self.db.get_team_run_session_id(run_id, role_str)
        if not session_id:
            await self._mark_run_failed(
                run_id, f"No session bound for team role '{role_str}'"
            )
            return None, False

        new_turn_count = turn_count + 1
        updated_run = self.db.update_team_run(run_id, turn_count=new_turn_count)
        if updated_run is not None:
            await self.bus.publish("team_run_updated", {"run": updated_run})

        turn_timeout_sec = int(run.get("turn_timeout_sec") or 600)
        remaining_run_sec = max(1, int((run_timeout_sec * 1000 - elapsed_ms) / 1000))
        effective_timeout = min(turn_timeout_sec, remaining_run_sec)

        active_set = self._active_role_sessions.setdefault(run_id, set())
        active_set.add(session_id)
        t0 = time.monotonic()
        try:
            turn = await self.session_manager.run_turn_and_wait(
                session_id=session_id,
                prompt=prompt,
                timeout_sec=effective_timeout,
                team_run_id=run_id,
            )
        except Exception as exc:
            if self._shutting_down:
                return None, False
            await self._mark_run_paused_limit(
                run_id,
                stage_str,
                f"Role '{role_str}' turn failed: {exc}",
            )
            return None, False
        finally:
            active_set.discard(session_id)
            if role_str == RoleId.UI_CHECKER.value:
                cur_run = self.db.get_team_run(run_id) or run
                await self._cleanup_checker_processes(
                    cur_run.get("worktree_path") or "",
                    int(cur_run.get("allocated_port") or 18100),
                )

        dt_ms = int((time.monotonic() - t0) * 1000)
        if record_elapsed:
            latest = self.db.get_team_run(run_id)
            if latest is not None:
                new_elapsed = int(latest.get("active_elapsed_ms") or 0) + dt_ms
                self.db.update_team_run(
                    run_id,
                    active_elapsed_ms=new_elapsed,
                    duration_ms=new_elapsed,
                )

        if (
            self._shutting_down
            and run_id not in self._stopped_runs
            and turn.get("status") != TurnStatus.COMPLETED.value
        ):
            # Server shutdown killed the CLI: leave the run 'running' so startup recovery
            # marks it 'interrupted' and the user can resume it.
            return turn, False

        if (
            run_id in self._stopped_runs
            or turn.get("status") == TurnStatus.STOPPED.value
        ):
            await self._mark_run_stopped(run_id)
            return turn, False

        t_status = turn.get("status")
        if t_status == TurnStatus.TIMEOUT.value:
            err_msg = (
                turn.get("error_message")
                or f"Role '{role_str}' turn timed out after {effective_timeout}s"
            )
            await self._mark_run_paused_limit(run_id, stage_str, err_msg)
            return turn, False

        if t_status != TurnStatus.COMPLETED.value:
            err_msg = (
                turn.get("error_message")
                or f"Role '{role_str}' CLI process exited with status '{t_status}'"
            )
            await self._mark_run_paused_limit(run_id, stage_str, err_msg)
            return turn, False

        return turn, True

    async def _run_role_turn(
        self,
        run_id: str,
        role: Union[RoleId, str],
        prompt: str,
        stage_state: Optional[Union[TeamStageState, str]] = None,
        *,
        record_elapsed: bool = True,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[ParsedTeamMessage], bool]:
        role_str = role.value if isinstance(role, RoleId) else str(role).strip().lower()
        run = self.db.get_team_run(run_id)
        if run is None:
            return None, None, False

        stage_str = (
            stage_state.value
            if isinstance(stage_state, TeamStageState)
            else str(stage_state or run.get("stage_state") or "")
        )
        prompt = _apply_resume_note(prompt, self._resume_notes.pop(run_id, None))

        turn, ok = await self._execute_single_session_turn(
            run_id=run_id,
            role_str=role_str,
            stage_str=stage_str,
            prompt=prompt,
            record_elapsed=record_elapsed,
        )
        if not ok or turn is None:
            return turn, None, False

        cur_run = self.db.get_team_run(run_id) or run
        reply_text = str(turn.get("response_text") or "")
        parsed = self._validate_parsed_for_run(cur_run, role_str, stage_str, reply_text)
        if parsed.valid:
            return turn, parsed, True

        # Single re-prompt recovery (Section 4.3)
        original_body = parsed.body_markdown
        repair_prompt = build_format_repair_prompt(
            parsed,
            stage_state=stage_str,
            role=role_str,
            asking_role=cur_run.get("asking_role"),
        )
        retry_turn, retry_ok = await self._execute_single_session_turn(
            run_id=run_id,
            role_str=role_str,
            stage_str=stage_str,
            prompt=repair_prompt,
            record_elapsed=record_elapsed,
        )
        if not retry_ok or retry_turn is None:
            return retry_turn, None, False

        cur_run = self.db.get_team_run(run_id) or cur_run
        retry_reply = str(retry_turn.get("response_text") or "")
        retry_parsed = self._validate_parsed_for_run(
            cur_run, role_str, stage_str, retry_reply
        )
        if retry_parsed.valid:
            if not retry_parsed.body_markdown.strip() and original_body.strip():
                retry_parsed.body_markdown = original_body
            return retry_turn, retry_parsed, True

        fail_msg = (
            f"Role '{role_str}' failed to emit a valid baton-msg block after 1 retry: "
            f"{retry_parsed.error}"
        )
        await self._mark_run_failed(run_id, fail_msg)
        return retry_turn, retry_parsed, False

    # -----------------------------------------------------------------------
    # State Machine Stage Handlers (Section 3.3)
    # -----------------------------------------------------------------------

    async def _route_specialist_question_to_pm(
        self,
        run: Dict[str, Any],
        asking_role: str,
        previous_stage: str,
        parsed: ParsedTeamMessage,
    ) -> bool:
        run_id = str(run["id"])
        total_q = int(run.get("question_round_count") or 0) + 1
        stage_q = self._stage_question_counts.get(run_id, 0) + 1
        self._stage_question_counts[run_id] = stage_q
        max_q = int(run.get("max_question_rounds") or 4)

        force_binding = stage_q >= max_q
        if force_binding:
            self._force_specialist_deliverable[run_id] = True

        self._set_pending_prompt(run_id, build_prompt_t6_specialist_question_to_pm(
            asking_role=asking_role,
            previous_stage_state=previous_stage,
            questions=parsed.questions,
            specialist_body_markdown=parsed.body_markdown,
            force_binding_decision=force_binding,
        ))
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=TeamStageState.PM_ANSWER_RELAY.value,
            asking_role=asking_role,
            question_round_count=total_q,
            pending_questions=list(parsed.questions),
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return True

    async def _handle_pm_triage(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        prompt = self._peek_pending_prompt(run_id)
        if not prompt:
            prompt = build_prompt_t1_pm_triage(
                task_prompt=str(run.get("task_prompt") or ""),
                worktree_path=str(run.get("work_dir") or run.get("worktree_path") or ""),
                base_branch=str(run.get("base_branch") or "main"),
                allocated_port=int(run.get("allocated_port") or 18100),
                needs_ui_mockup_override=self._needs_ui_mockup_overrides.get(run_id),
            )

        turn, parsed, ok = await self._run_role_turn(
            run_id, RoleId.PM, prompt, TeamStageState.PM_TRIAGE
        )
        if not ok or parsed is None or turn is None:
            return False
        self._clear_pending_prompt(run_id)

        await self._record_message(
            run_id=run_id,
            from_role=RoleId.PM.value,
            to_role=parsed.to,
            kind=parsed.kind,
            stage_state=TeamStageState.PM_TRIAGE.value,
            summary=parsed.summary,
            content_text=parsed.body_markdown,
            payload=parsed.raw_dict,
            turn_id=turn.get("id"),
        )

        cur_run = self.db.get_team_run(run_id) or run
        if parsed.kind == TeamMessageKind.QUESTION.value:
            new_q = int(cur_run.get("question_round_count") or 0) + 1
            await self._transition_to_waiting_user(
                run_id,
                TeamStageState.WAITING_USER_ANSWERS.value,
                asking_role=None,
                pending_questions=parsed.questions,
                question_round_count=new_q,
            )
            return False

        # parsed.kind == "deliverable"
        needs_ui = bool(parsed.needs_ui_mockup)
        wt_path = str(cur_run.get("work_dir") or cur_run.get("worktree_path") or "")
        req_text = parsed.body_markdown.strip() or parsed.summary
        if wt_path:
            await asyncio.to_thread(
                write_requirements_artifact,
                wt_path,
                req_text,
                cur_run.get("qa_history") or [],
            )

        self._stage_question_counts[run_id] = 0
        self._force_specialist_deliverable.pop(run_id, None)
        next_stage = (
            TeamStageState.UI_DESIGN.value
            if needs_ui
            else TeamStageState.ARCHITECTURE.value
        )
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=next_stage,
            needs_ui_mockup=needs_ui,
            asking_role=None,
            pending_questions=[],
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return True

    async def _handle_ui_design(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        wt_path = str(run.get("work_dir") or run.get("worktree_path") or "")
        prompt = self._peek_pending_prompt(run_id)
        if not prompt:
            req_md = await asyncio.to_thread(read_requirements_artifact, wt_path)
            prompt = build_prompt_t3_pm_to_ui_designer(
                task_prompt=str(run.get("task_prompt") or ""),
                pm_requirements_markdown=req_md or str(run.get("task_prompt") or ""),
                qa_transcript_or_none=run.get("qa_history") or [],
            )

        turn, parsed, ok = await self._run_role_turn(
            run_id, RoleId.UI_DESIGNER, prompt, TeamStageState.UI_DESIGN
        )
        if not ok or parsed is None or turn is None:
            return False
        self._clear_pending_prompt(run_id)

        await self._record_message(
            run_id=run_id,
            from_role=RoleId.UI_DESIGNER.value,
            to_role=parsed.to,
            kind=parsed.kind,
            stage_state=TeamStageState.UI_DESIGN.value,
            summary=parsed.summary,
            content_text=parsed.body_markdown,
            payload=parsed.raw_dict,
            turn_id=turn.get("id"),
        )

        cur_run = self.db.get_team_run(run_id) or run
        if parsed.kind == TeamMessageKind.QUESTION.value:
            return await self._route_specialist_question_to_pm(
                cur_run,
                RoleId.UI_DESIGNER.value,
                TeamStageState.UI_DESIGN.value,
                parsed,
            )

        # parsed.kind == "deliverable"
        mockup_rel = parsed.mockup_path or ".baton/mockup.html"
        new_version = int(cur_run.get("mockup_version") or 0) + 1
        if wt_path:
            await asyncio.to_thread(
                snapshot_mockup_version,
                wt_path,
                new_version,
                mockup_rel,
            )

        self._stage_question_counts[run_id] = 0
        self._force_specialist_deliverable.pop(run_id, None)
        self._set_pending_prompt(run_id, build_prompt_pm_mockup_review(
            ui_designer_summary=parsed.summary,
            ui_designer_body=parsed.body_markdown,
            mockup_path=mockup_rel,
            mockup_version=new_version,
        ))
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=TeamStageState.PM_MOCKUP_REVIEW.value,
            mockup_version=new_version,
            asking_role=None,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return True

    async def _handle_pm_mockup_review(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        mockup_version = max(1, int(run.get("mockup_version") or 1))
        prompt = self._peek_pending_prompt(run_id)
        if not prompt:
            prompt = build_prompt_pm_mockup_review(
                ui_designer_summary="UI mockup ready for review",
                mockup_version=mockup_version,
            )

        turn, parsed, ok = await self._run_role_turn(
            run_id, RoleId.PM, prompt, TeamStageState.PM_MOCKUP_REVIEW
        )
        if not ok or parsed is None or turn is None:
            return False
        self._clear_pending_prompt(run_id)

        await self._record_message(
            run_id=run_id,
            from_role=RoleId.PM.value,
            to_role=parsed.to,
            kind=parsed.kind,
            stage_state=TeamStageState.PM_MOCKUP_REVIEW.value,
            summary=parsed.summary,
            content_text=parsed.body_markdown,
            payload=parsed.raw_dict,
            turn_id=turn.get("id"),
        )

        cur_run = self.db.get_team_run(run_id) or run
        if parsed.kind == TeamMessageKind.REJECT.value:
            self._set_pending_prompt(run_id, build_prompt_t4_mockup_revision(
                mockup_version=mockup_version,
                feedback_source="PM",
                feedback_text=parsed.findings or parsed.body_markdown or parsed.summary,
            ))
            updated = self.db.update_team_run(
                run_id,
                status=TeamRunStatus.RUNNING.value,
                stage_state=TeamStageState.UI_DESIGN.value,
                last_findings=parsed.findings,
            )
            if updated is not None:
                await self.bus.publish("team_run_updated", {"run": updated})
            return True

        # approval_request or approve
        if cur_run.get("auto_approve_mockup"):
            updated = self.db.update_team_run(
                run_id,
                status=TeamRunStatus.RUNNING.value,
                stage_state=TeamStageState.ARCHITECTURE.value,
                mockup_approved=True,
            )
            if updated is not None:
                await self.bus.publish("team_run_updated", {"run": updated})
            return True

        await self._transition_to_waiting_user(
            run_id,
            TeamStageState.WAITING_MOCKUP_APPROVAL.value,
        )
        return False

    async def _handle_architecture(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        wt_path = str(run.get("work_dir") or run.get("worktree_path") or "")
        prompt = self._peek_pending_prompt(run_id)
        if not prompt:
            req_md = await asyncio.to_thread(read_requirements_artifact, wt_path)
            prompt = build_prompt_t5_pm_to_architect(
                task_prompt=str(run.get("task_prompt") or ""),
                pm_requirements_markdown=req_md or str(run.get("task_prompt") or ""),
                allocated_port=int(run.get("allocated_port") or 18100),
                qa_transcript_or_none=run.get("qa_history") or [],
                needs_ui_mockup=bool(run.get("needs_ui_mockup")),
                mockup_approved=bool(run.get("mockup_approved")),
            )

        turn, parsed, ok = await self._run_role_turn(
            run_id, RoleId.ARCHITECT, prompt, TeamStageState.ARCHITECTURE
        )
        if not ok or parsed is None or turn is None:
            return False
        self._clear_pending_prompt(run_id)

        await self._record_message(
            run_id=run_id,
            from_role=RoleId.ARCHITECT.value,
            to_role=parsed.to,
            kind=parsed.kind,
            stage_state=TeamStageState.ARCHITECTURE.value,
            summary=parsed.summary,
            content_text=parsed.body_markdown,
            payload=parsed.raw_dict,
            turn_id=turn.get("id"),
        )

        cur_run = self.db.get_team_run(run_id) or run
        if parsed.kind == TeamMessageKind.QUESTION.value:
            return await self._route_specialist_question_to_pm(
                cur_run,
                RoleId.ARCHITECT.value,
                TeamStageState.ARCHITECTURE.value,
                parsed,
            )

        # parsed.kind == "deliverable"
        arch_text = parsed.body_markdown.strip() or parsed.summary
        if wt_path:
            await asyncio.to_thread(write_architecture_artifact, wt_path, arch_text)

        app_run = parsed.app_run or cur_run.get("app_run") or {}
        self._stage_question_counts[run_id] = 0
        self._force_specialist_deliverable.pop(run_id, None)
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=TeamStageState.PLANNING.value,
            app_run=app_run,
            asking_role=None,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return True

    async def _handle_planning(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        wt_path = str(run.get("work_dir") or run.get("worktree_path") or "")
        prompt = self._peek_pending_prompt(run_id)
        if not prompt:
            req_md = await asyncio.to_thread(read_requirements_artifact, wt_path)
            arch_md = await asyncio.to_thread(read_architecture_artifact, wt_path)
            prompt = build_prompt_t8_architect_to_planner(
                task_prompt=str(run.get("task_prompt") or ""),
                pm_requirements_excerpt=req_md or str(run.get("task_prompt") or ""),
                architecture_excerpt=arch_md or "(See repository files)",
                app_run=run.get("app_run") or {},
                allocated_port=int(run.get("allocated_port") or 18100),
                needs_ui_mockup=bool(run.get("needs_ui_mockup")),
                mockup_approved=bool(run.get("mockup_approved")),
            )

        turn, parsed, ok = await self._run_role_turn(
            run_id, RoleId.PLANNER, prompt, TeamStageState.PLANNING
        )
        if not ok or parsed is None or turn is None:
            return False
        self._clear_pending_prompt(run_id)

        await self._record_message(
            run_id=run_id,
            from_role=RoleId.PLANNER.value,
            to_role=parsed.to,
            kind=parsed.kind,
            stage_state=TeamStageState.PLANNING.value,
            summary=parsed.summary,
            content_text=parsed.body_markdown,
            payload=parsed.raw_dict,
            turn_id=turn.get("id"),
        )

        cur_run = self.db.get_team_run(run_id) or run
        if parsed.kind == TeamMessageKind.QUESTION.value:
            return await self._route_specialist_question_to_pm(
                cur_run,
                RoleId.PLANNER.value,
                TeamStageState.PLANNING.value,
                parsed,
            )

        # parsed.kind == "deliverable"
        plan_text = parsed.body_markdown.strip() or parsed.summary
        if wt_path:
            await asyncio.to_thread(write_plan_artifact, wt_path, plan_text)

        app_run = parsed.app_run or cur_run.get("app_run") or {}
        self._stage_question_counts[run_id] = 0
        self._force_specialist_deliverable.pop(run_id, None)
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=TeamStageState.IMPLEMENTING.value,
            app_run=app_run,
            asking_role=None,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return True

    async def _handle_implementing(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        wt_path = str(run.get("work_dir") or run.get("worktree_path") or "")
        prompt = self._peek_pending_prompt(run_id)
        if not prompt:
            plan_md = await asyncio.to_thread(read_plan_artifact, wt_path)
            prompt = build_prompt_t9_planner_to_implementer(
                task_prompt=str(run.get("task_prompt") or ""),
                plan_excerpt=plan_md or "(See .baton/plan.md)",
                needs_ui_mockup=bool(run.get("needs_ui_mockup")),
            )

        turn, parsed, ok = await self._run_role_turn(
            run_id, RoleId.IMPLEMENTER, prompt, TeamStageState.IMPLEMENTING
        )
        if not ok or parsed is None or turn is None:
            return False
        self._clear_pending_prompt(run_id)

        await self._record_message(
            run_id=run_id,
            from_role=RoleId.IMPLEMENTER.value,
            to_role=parsed.to,
            kind=parsed.kind,
            stage_state=TeamStageState.IMPLEMENTING.value,
            summary=parsed.summary,
            content_text=parsed.body_markdown,
            payload=parsed.raw_dict,
            turn_id=turn.get("id"),
        )

        cur_run = self.db.get_team_run(run_id) or run
        if parsed.kind == TeamMessageKind.QUESTION.value:
            return await self._route_specialist_question_to_pm(
                cur_run,
                RoleId.IMPLEMENTER.value,
                TeamStageState.IMPLEMENTING.value,
                parsed,
            )

        # parsed.kind == "deliverable" (to == "checkers")
        self._stage_question_counts[run_id] = 0
        self._force_specialist_deliverable.pop(run_id, None)
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=TeamStageState.CHECKING.value,
            asking_role=None,
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return True

    async def _handle_pm_answer_relay(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        asking_role = str(run.get("asking_role") or RoleId.ARCHITECT.value)
        role_to_stage = {
            RoleId.UI_DESIGNER.value: TeamStageState.UI_DESIGN.value,
            RoleId.ARCHITECT.value: TeamStageState.ARCHITECTURE.value,
            RoleId.PLANNER.value: TeamStageState.PLANNING.value,
            RoleId.IMPLEMENTER.value: TeamStageState.IMPLEMENTING.value,
        }
        prev_stage = role_to_stage.get(asking_role, TeamStageState.ARCHITECTURE.value)

        prompt = self._peek_pending_prompt(run_id)
        if not prompt:
            prompt = build_prompt_t6_specialist_question_to_pm(
                asking_role=asking_role,
                previous_stage_state=prev_stage,
                questions=run.get("pending_questions") or ["Please provide guidance."],
                force_binding_decision=bool(self._force_specialist_deliverable.get(run_id)),
            )

        turn, parsed, ok = await self._run_role_turn(
            run_id, RoleId.PM, prompt, TeamStageState.PM_ANSWER_RELAY
        )
        if not ok or parsed is None or turn is None:
            return False
        self._clear_pending_prompt(run_id)

        await self._record_message(
            run_id=run_id,
            from_role=RoleId.PM.value,
            to_role=parsed.to,
            kind=parsed.kind,
            stage_state=TeamStageState.PM_ANSWER_RELAY.value,
            summary=parsed.summary,
            content_text=parsed.body_markdown,
            payload=parsed.raw_dict,
            turn_id=turn.get("id"),
        )

        if parsed.kind == TeamMessageKind.QUESTION.value:
            await self._transition_to_waiting_user(
                run_id,
                TeamStageState.WAITING_USER_ANSWERS.value,
                asking_role=asking_role,
                pending_questions=parsed.questions,
            )
            return False

        # parsed.kind == "answer"
        target_role = parsed.to or asking_role
        target_stage = role_to_stage.get(target_role, prev_stage)
        ans_body = parsed.body_markdown.strip() or parsed.summary
        if parsed.answers:
            ans_list_str = "\n".join(f"- {a}" for a in parsed.answers)
            ans_body = f"{ans_body}\n\n{ans_list_str}".strip()

        self._set_pending_prompt(run_id, build_prompt_t7_pm_answer_to_specialist(
            asking_role=target_role,
            pm_answer_markdown=ans_body,
            force_deliverable=bool(self._force_specialist_deliverable.get(run_id)),
        ))
        updated = self.db.update_team_run(
            run_id,
            status=TeamRunStatus.RUNNING.value,
            stage_state=target_stage,
            asking_role=None,
            pending_questions=[],
        )
        if updated is not None:
            await self.bus.publish("team_run_updated", {"run": updated})
        return True

    def _get_latest_role_summary(self, run_id: str, role_str: str) -> str:
        messages = self.db.list_team_messages(run_id)
        for msg in reversed(messages):
            if msg.get("from_role") == role_str:
                body = str(msg.get("content_text") or "").strip()
                summary = str(msg.get("summary") or "").strip()
                return body or summary
        return ""

    async def _handle_checking(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        turn_count = int(run.get("turn_count") or 0)
        max_turns = int(run.get("max_total_turns") or 30)
        if turn_count + 2 > max_turns:
            await self._mark_run_paused_limit(
                run_id,
                TeamStageState.CHECKING.value,
                f"Maximum total turns ({max_turns}) reached before parallel verification stage.",
            )
            return False

        wt_path = str(run.get("work_dir") or run.get("worktree_path") or "")
        allocated_port = int(run.get("allocated_port") or 18100)
        rework_loop_count = int(run.get("rework_loop_count") or 0)
        max_rework_loops = _int_or(run.get("max_rework_loops"), 3)

        if wt_path:
            await asyncio.to_thread(write_browser_helper, wt_path)

        plan_md = await asyncio.to_thread(read_plan_artifact, wt_path)
        diff_res = await asyncio.to_thread(
            get_workspace_diff,
            wt_path,
            str(run.get("base_commit") or "HEAD"),
        )
        stat_text = str(diff_res.get("stat_text") or "")
        if wt_path:
            # The reviewer is read-only (no shell), so it cannot run `git diff` itself.
            await asyncio.to_thread(
                write_baton_artifact,
                wt_path,
                REVIEW_DIFF_FILENAME,
                str(diff_res.get("diff_text") or "(no changes against the base commit)\n"),
            )
        imp_excerpt = (
            self._get_latest_role_summary(run_id, RoleId.IMPLEMENTER.value)
            or "Implementation completed in worktree."
        )

        ui_prompt, rev_prompt = build_prompt_t10_checkers(
            task_prompt=str(run.get("task_prompt") or ""),
            allocated_port=allocated_port,
            app_run=run.get("app_run") or {},
            implementer_output_excerpt=imp_excerpt,
            plan_excerpt=plan_md or "(See .baton/plan.md)",
            git_diff_stat=stat_text,
            rework_loop_count=rework_loop_count,
            needs_ui_mockup=bool(run.get("needs_ui_mockup")),
        )
        note = self._resume_notes.pop(run_id, None)
        ui_prompt = _apply_resume_note(ui_prompt, note)
        rev_prompt = _apply_resume_note(rev_prompt, note)

        t0 = time.monotonic()
        ui_task = asyncio.create_task(
            self._run_role_turn(
                run_id,
                RoleId.UI_CHECKER,
                ui_prompt,
                TeamStageState.CHECKING,
                record_elapsed=False,
            )
        )
        rev_task = asyncio.create_task(
            self._run_role_turn(
                run_id,
                RoleId.REVIEWER,
                rev_prompt,
                TeamStageState.CHECKING,
                record_elapsed=False,
            )
        )
        (ui_turn, ui_parsed, ui_ok), (rev_turn, rev_parsed, rev_ok) = await asyncio.gather(
            ui_task, rev_task
        )

        dt_ms = int((time.monotonic() - t0) * 1000)
        latest = self.db.get_team_run(run_id)
        if latest is not None:
            new_elapsed = int(latest.get("active_elapsed_ms") or 0) + dt_ms
            self.db.update_team_run(
                run_id,
                active_elapsed_ms=new_elapsed,
                duration_ms=new_elapsed,
            )

        if ui_parsed is not None and ui_turn is not None:
            await self._record_message(
                run_id=run_id,
                from_role=RoleId.UI_CHECKER.value,
                to_role=ui_parsed.to,
                kind=ui_parsed.kind,
                stage_state=TeamStageState.CHECKING.value,
                summary=ui_parsed.summary,
                content_text=ui_parsed.body_markdown,
                payload=ui_parsed.raw_dict,
                turn_id=ui_turn.get("id"),
            )

        if rev_parsed is not None and rev_turn is not None:
            await self._record_message(
                run_id=run_id,
                from_role=RoleId.REVIEWER.value,
                to_role=rev_parsed.to,
                kind=rev_parsed.kind,
                stage_state=TeamStageState.CHECKING.value,
                summary=rev_parsed.summary,
                content_text=rev_parsed.body_markdown,
                payload=rev_parsed.raw_dict,
                turn_id=rev_turn.get("id"),
            )

        if not ui_ok or not rev_ok or ui_parsed is None or rev_parsed is None:
            return False

        ui_verdict = (
            "APPROVED"
            if ui_parsed.kind == TeamMessageKind.APPROVE.value
            else "CHANGES_REQUESTED"
        )
        code_verdict = (
            "APPROVED"
            if rev_parsed.kind == TeamMessageKind.APPROVE.value
            else "CHANGES_REQUESTED"
        )

        if ui_verdict == "APPROVED" and code_verdict == "APPROVED":
            self._set_pending_prompt(run_id, build_prompt_t12_pm_final_summary(
                rework_loop_count=rework_loop_count,
                implementer_summary=imp_excerpt,
                ui_checker_summary=ui_parsed.body_markdown or ui_parsed.summary,
                code_reviewer_summary=rev_parsed.body_markdown or rev_parsed.summary,
                git_diff_stat=stat_text,
            ))
            updated = self.db.update_team_run(
                run_id,
                status=TeamRunStatus.RUNNING.value,
                stage_state=TeamStageState.PM_FINAL_SUMMARY.value,
                ui_check_verdict="APPROVED",
                code_review_verdict="APPROVED",
                last_findings=[],
            )
            if updated is not None:
                await self.bus.publish("team_run_updated", {"run": updated})
            return True

        combined_findings: List[str] = []
        if ui_verdict != "APPROVED":
            combined_findings.extend(ui_parsed.findings or [ui_parsed.summary])
        if code_verdict != "APPROVED":
            combined_findings.extend(rev_parsed.findings or [rev_parsed.summary])

        cur_run = self.db.get_team_run(run_id) or run
        max_rework_loops = _int_or(cur_run.get("max_rework_loops"), 3)
        if rework_loop_count < max_rework_loops:
            next_loop = rework_loop_count + 1
            self._set_pending_prompt(run_id, build_prompt_t11_rework(
                rework_loop_count=next_loop,
                max_rework_loops=max_rework_loops,
                ui_verdict=ui_verdict,
                ui_findings=ui_parsed.findings or ui_parsed.body_markdown,
                code_verdict=code_verdict,
                code_findings=rev_parsed.findings or rev_parsed.body_markdown,
            ))
            updated = self.db.update_team_run(
                run_id,
                status=TeamRunStatus.RUNNING.value,
                stage_state=TeamStageState.IMPLEMENTING.value,
                rework_loop_count=next_loop,
                ui_check_verdict=ui_verdict,
                code_review_verdict=code_verdict,
                last_findings=combined_findings,
            )
            if updated is not None:
                await self.bus.publish("team_run_updated", {"run": updated})
            return True

        # Rework limit reached -> pause at paused_limit
        self._set_pending_prompt(run_id, build_prompt_t11_rework(
            rework_loop_count=rework_loop_count + 1,
            max_rework_loops=max_rework_loops + 1,
            ui_verdict=ui_verdict,
            ui_findings=ui_parsed.findings or ui_parsed.body_markdown,
            code_verdict=code_verdict,
            code_findings=rev_parsed.findings or rev_parsed.body_markdown,
        ))
        await self._mark_run_paused_limit(
            run_id,
            TeamStageState.IMPLEMENTING.value,
            f"Maximum rework loops ({max_rework_loops}) reached; verification still requested changes.",
            ui_check_verdict=ui_verdict,
            code_review_verdict=code_verdict,
            last_findings=combined_findings,
        )
        return False

    async def _handle_pm_final_summary(self, run_id: str) -> bool:
        run = self.db.get_team_run(run_id)
        if run is None:
            return False

        prompt = self._peek_pending_prompt(run_id)
        if not prompt:
            wt_path = str(run.get("work_dir") or run.get("worktree_path") or "")
            diff_res = await asyncio.to_thread(
                get_workspace_diff,
                wt_path,
                str(run.get("base_commit") or "HEAD"),
            )
            prompt = build_prompt_t12_pm_final_summary(
                rework_loop_count=int(run.get("rework_loop_count") or 0),
                implementer_summary=self._get_latest_role_summary(
                    run_id, RoleId.IMPLEMENTER.value
                )
                or "Completed implementation.",
                ui_checker_summary=self._get_latest_role_summary(
                    run_id, RoleId.UI_CHECKER.value
                )
                or "UI checks passed.",
                code_reviewer_summary=self._get_latest_role_summary(
                    run_id, RoleId.REVIEWER.value
                )
                or "Code review passed.",
                git_diff_stat=str(diff_res.get("stat_text") or ""),
            )

        turn, parsed, ok = await self._run_role_turn(
            run_id, RoleId.PM, prompt, TeamStageState.PM_FINAL_SUMMARY
        )
        if not ok or parsed is None or turn is None:
            return False
        self._clear_pending_prompt(run_id)

        await self._record_message(
            run_id=run_id,
            from_role=RoleId.PM.value,
            to_role=parsed.to,
            kind=parsed.kind,
            stage_state=TeamStageState.PM_FINAL_SUMMARY.value,
            summary=parsed.summary,
            content_text=parsed.body_markdown,
            payload=parsed.raw_dict,
            turn_id=turn.get("id"),
        )

        final_summary = parsed.body_markdown.strip() or parsed.summary
        await self._transition_to_waiting_user(
            run_id,
            TeamStageState.WAITING_FINAL_SIGNOFF.value,
            final_summary=final_summary,
        )
        return False
