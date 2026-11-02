from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.config import AppConfig, load_config, validate_working_dir
from backend.db import Database
from backend.models import (
    AnswerTeamQuestionsRequest,
    ApprovePipelineRunRequest,
    ConflictError,
    CreateSessionRequest,
    ForwardTurnRequest,
    NotFoundError,
    PermissionLevel,
    ResumeTeamRunRequest,
    ReviewTeamMockupRequest,
    RoleId,
    SavePipelineRequest,
    SaveTeamRequest,
    SessionStatus,
    SignoffTeamRunRequest,
    StartPipelineRunRequest,
    StartTeamRunRequest,
    StartTurnRequest,
    TeamRunStatus,
    TurnStatus,
    UnprocessableError,
    UpdateSessionRequest,
    ValidationError,
)
from backend.pipeline_engine import (
    PipelineEngine,
    build_forward_prompt,
    validate_pipeline_stages,
)
from backend.providers import list_providers_metadata
from backend.pubsub import EventBus
from backend.roles import (
    DEFAULT_TEAM_MEMBERS,
    ROLE_DEFINITIONS,
    TEAM_ROLE_DEFINITIONS,
    get_role_label,
)
from backend.session_manager import SessionManager
from backend.team_engine import TeamEngine
from backend.worktree_manager import finalize_workspace

# --- Exception Handlers ---

async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


async def conflict_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


async def unprocessable_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


async def validation_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


async def value_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# --- State Access Helpers ---

def get_config(request: Request) -> AppConfig:
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        cfg = load_config()
        request.app.state.config = cfg
    return cfg


def get_db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if db is None:
        cfg = get_config(request)
        db = Database(cfg.db_path)
        db.recover_orphaned_states()
        request.app.state.db = db
    return db


def get_bus(request: Request) -> EventBus:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        bus = EventBus()
        request.app.state.bus = bus
    return bus


def get_session_manager(request: Request) -> SessionManager:
    sm = getattr(request.app.state, "session_manager", None)
    if sm is None:
        cfg = get_config(request)
        db = get_db(request)
        bus = get_bus(request)
        sm = SessionManager(cfg, db, bus)
        request.app.state.session_manager = sm
    return sm


def get_pipeline_engine(request: Request) -> PipelineEngine:
    pe = getattr(request.app.state, "pipeline_engine", None)
    if pe is None:
        db = get_db(request)
        bus = get_bus(request)
        sm = get_session_manager(request)
        pe = PipelineEngine(db, sm, bus)
        request.app.state.pipeline_engine = pe
    return pe


def get_team_engine(request: Request) -> TeamEngine:
    te = getattr(request.app.state, "team_engine", None)
    if te is None:
        cfg = get_config(request)
        db = get_db(request)
        bus = get_bus(request)
        sm = get_session_manager(request)
        te = TeamEngine(db, sm, bus, cfg)
        request.app.state.team_engine = te
    return te


MOCKUP_CSP: str = (
    # `sandbox` gives the mockup an opaque origin even when opened as a top-level page
    # ("Open in new tab"), so its scripts can never act with Baton's origin.
    "sandbox allow-scripts; "
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "frame-ancestors 'self';"
)


# --- API Routes ---

api_router = APIRouter()


@api_router.get("/api/meta")
async def get_meta(request: Request) -> Dict[str, Any]:
    cfg = get_config(request)
    roles_list = [
        {
            "id": r.id.value,
            "name": r.name,
            "description": r.description,
            "default_permission": r.default_permission.value,
            "system_prompt": r.system_prompt,
        }
        for r in ROLE_DEFINITIONS.values()
    ]
    team_roles_list = [
        {
            "id": r.id.value,
            "name": r.name,
            "description": r.description,
            "default_provider": r.default_provider or "claude",
            "default_model": r.default_model or "sonnet",
            "default_permission": r.default_permission.value,
            "system_prompt": r.system_prompt,
        }
        for r in TEAM_ROLE_DEFINITIONS.values()
    ]
    return {
        "default_working_dir": os.getcwd(),
        "providers": await list_providers_metadata(cfg),
        "roles": roles_list,
        "team_roles": team_roles_list,
        "default_team": DEFAULT_TEAM_MEMBERS,
        "default_team_members": DEFAULT_TEAM_MEMBERS,
        "permissions": [
            {
                "id": PermissionLevel.READONLY.value,
                "label": "Read-Only (Plan)",
                "description": "Inspect files only; no edits or shell commands.",
            },
            {
                "id": PermissionLevel.EDIT.value,
                "label": "Edit Files",
                "description": "Auto-accept file edits in workspace.",
            },
            {
                "id": PermissionLevel.FULL.value,
                "label": "Full Access",
                "description": "Auto-approve file edits and shell commands.",
            },
        ],
    }


@api_router.get("/api/sessions")
async def list_sessions(request: Request) -> Dict[str, Any]:
    db = get_db(request)
    return {"sessions": db.list_sessions()}


@api_router.post("/api/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(request: Request, req: CreateSessionRequest) -> Dict[str, Any]:
    db = get_db(request)
    bus = get_bus(request)

    working_dir = validate_working_dir(req.working_dir or os.getcwd())
    if req.permission_level is not None:
        perm = req.permission_level.value
    else:
        role_def = ROLE_DEFINITIONS.get(req.role)
        perm = (
            role_def.default_permission.value
            if role_def is not None
            else PermissionLevel.READONLY.value
        )

    session = db.create_session(
        name=req.name,
        provider=req.provider.value,
        model=req.model,
        role=req.role.value,
        custom_prompt=req.custom_prompt,
        permission_level=perm,
        working_dir=working_dir,
    )
    await bus.publish("session_updated", {"session": session})
    return session


@api_router.get("/api/sessions/{session_id}")
async def get_session(request: Request, session_id: str) -> Dict[str, Any]:
    db = get_db(request)
    session = db.get_session(session_id)
    if session is None:
        raise NotFoundError(f"Session {session_id} not found")
    turns = db.list_turns_for_session(session_id)
    return {"session": session, "turns": turns}


@api_router.patch("/api/sessions/{session_id}")
async def update_session(
    request: Request, session_id: str, req: UpdateSessionRequest
) -> Dict[str, Any]:
    db = get_db(request)
    bus = get_bus(request)
    sm = get_session_manager(request)

    session = db.get_session(session_id)
    if session is None:
        raise NotFoundError(f"Session {session_id} not found")
    if sm.is_session_running(session_id):
        raise ConflictError(f"Session {session_id} is currently running a turn")

    perm_str = req.permission_level.value if req.permission_level is not None else None
    updated = db.update_session(
        session_id=session_id,
        name=req.name,
        permission_level=perm_str,
        model=req.model,
        custom_prompt=req.custom_prompt,
    )
    if updated is None:
        raise NotFoundError(f"Session {session_id} not found")
    await bus.publish("session_updated", {"session": updated})
    return updated


@api_router.delete("/api/sessions/{session_id}")
async def delete_session(request: Request, session_id: str) -> Dict[str, Any]:
    db = get_db(request)
    sm = get_session_manager(request)

    session = db.get_session(session_id)
    if session is None:
        raise NotFoundError(f"Session {session_id} not found")
    if db.is_session_bound_to_pipeline(session_id):
        raise ConflictError(
            f"Session {session_id} is bound to an existing pipeline stage"
        )

    if sm.is_session_running(session_id):
        await sm.stop_session(session_id)

    db.delete_session(session_id)
    return {"deleted": True, "session_id": session_id}


@api_router.post(
    "/api/sessions/{session_id}/turns", status_code=status.HTTP_202_ACCEPTED
)
async def start_turn(
    request: Request, session_id: str, req: StartTurnRequest
) -> Dict[str, Any]:
    db = get_db(request)
    sm = get_session_manager(request)

    turn_id = await sm.start_turn(
        session_id=session_id,
        prompt=req.prompt,
        timeout_sec=req.timeout_sec,
    )
    turn = db.get_turn(turn_id)
    return {
        "turn_id": turn_id,
        "session_id": session_id,
        "turn_number": turn["turn_number"] if turn else 1,
        "status": turn["status"] if turn else "running",
    }


@api_router.post("/api/sessions/{session_id}/stop")
async def stop_session(request: Request, session_id: str) -> Dict[str, Any]:
    db = get_db(request)
    sm = get_session_manager(request)

    session = db.get_session(session_id)
    if session is None:
        raise NotFoundError(f"Session {session_id} not found")

    stopped = await sm.stop_session(session_id)
    return {"stopped": stopped, "session_id": session_id}


@api_router.get("/api/turns")
async def list_all_turns(request: Request) -> Dict[str, Any]:
    db = get_db(request)
    return {"turns": db.list_all_turns()}


@api_router.get("/api/turns/{turn_id}")
async def get_turn(request: Request, turn_id: str) -> Dict[str, Any]:
    db = get_db(request)
    sm = get_session_manager(request)

    turn = db.get_turn(turn_id)
    if turn is None:
        raise NotFoundError(f"Turn {turn_id} not found")

    events = [e.model_dump() for e in sm.read_turn_events(turn_id)]
    return {"turn": turn, "events": events}


@api_router.post(
    "/api/turns/{turn_id}/forward", status_code=status.HTTP_202_ACCEPTED
)
async def forward_turn(
    request: Request, turn_id: str, req: ForwardTurnRequest
) -> Dict[str, Any]:
    db = get_db(request)
    sm = get_session_manager(request)

    source_turn = db.get_turn(turn_id)
    if source_turn is None:
        raise NotFoundError(f"Turn {turn_id} not found")

    target_session = db.get_session(req.target_session_id)
    if target_session is None:
        raise NotFoundError(f"Session {req.target_session_id} not found")

    if req.target_session_id == source_turn.get("session_id"):
        raise UnprocessableError("Cannot forward a turn to the same session")

    if source_turn.get("status") != TurnStatus.COMPLETED.value:
        raise UnprocessableError(
            f"Source turn {turn_id} is not completed (status={source_turn.get('status')})"
        )

    response_text = source_turn.get("response_text") or ""
    if not response_text.strip():
        raise UnprocessableError(f"Source turn {turn_id} has empty response_text")

    if (
        sm.is_session_running(req.target_session_id)
        or target_session.get("status") == SessionStatus.RUNNING.value
    ):
        raise ConflictError(
            f"Session {req.target_session_id} is currently running a turn"
        )

    source_session_name = source_turn.get("session_name") or "Unknown Session"
    source_role_label = get_role_label(source_turn.get("role") or "")
    prompt = build_forward_prompt(
        source_session_name=source_session_name,
        source_role_label=source_role_label,
        turn_number=int(source_turn["turn_number"]),
        response_text=response_text,
        instructions=req.instructions,
    )

    new_turn_id = await sm.start_turn(
        session_id=req.target_session_id,
        prompt=prompt,
    )
    new_turn = db.get_turn(new_turn_id)
    return {
        "turn_id": new_turn_id,
        "session_id": req.target_session_id,
        "turn_number": new_turn["turn_number"] if new_turn else 1,
        "status": new_turn["status"] if new_turn else "running",
    }


@api_router.get("/api/pipelines")
async def list_pipelines(request: Request) -> Dict[str, Any]:
    db = get_db(request)
    return {"pipelines": db.list_pipelines()}


@api_router.post("/api/pipelines", status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    request: Request, req: SavePipelineRequest
) -> Dict[str, Any]:
    db = get_db(request)
    stages_dicts = [s.model_dump() for s in req.stages]
    validate_pipeline_stages(db, stages_dicts)

    pipeline = db.create_pipeline(
        name=req.name,
        description=req.description,
        max_review_loops=req.max_review_loops,
        stages=stages_dicts,
    )
    return pipeline


@api_router.put("/api/pipelines/{pipeline_id}")
async def update_pipeline(
    request: Request, pipeline_id: str, req: SavePipelineRequest
) -> Dict[str, Any]:
    db = get_db(request)
    existing = db.get_pipeline(pipeline_id)
    if existing is None:
        raise NotFoundError(f"Pipeline {pipeline_id} not found")

    stages_dicts = [s.model_dump() for s in req.stages]
    validate_pipeline_stages(db, stages_dicts)

    updated = db.update_pipeline(
        pipeline_id=pipeline_id,
        name=req.name,
        description=req.description,
        max_review_loops=req.max_review_loops,
        stages=stages_dicts,
    )
    if updated is None:
        raise NotFoundError(f"Pipeline {pipeline_id} not found")
    return updated


@api_router.delete("/api/pipelines/{pipeline_id}")
async def delete_pipeline(request: Request, pipeline_id: str) -> Dict[str, Any]:
    db = get_db(request)
    pipeline = db.get_pipeline(pipeline_id)
    if pipeline is None:
        raise NotFoundError(f"Pipeline {pipeline_id} not found")

    if db.has_active_pipeline_runs(pipeline_id):
        raise ConflictError(
            f"Pipeline {pipeline_id} has active runs and cannot be deleted"
        )

    db.delete_pipeline(pipeline_id)
    return {"deleted": True, "pipeline_id": pipeline_id}


@api_router.post(
    "/api/pipelines/{pipeline_id}/runs", status_code=status.HTTP_202_ACCEPTED
)
async def start_pipeline_run(
    request: Request, pipeline_id: str, req: StartPipelineRunRequest
) -> Dict[str, Any]:
    pe = get_pipeline_engine(request)
    result = await pe.start_pipeline_run(
        pipeline_id=pipeline_id,
        task_prompt=req.task_prompt or "",
        start_stage_index=req.start_stage_index,
        initial_input=req.initial_input,
        initial_input_turn_id=req.initial_input_turn_id,
    )
    return result


@api_router.get("/api/pipeline-runs")
async def list_pipeline_runs(request: Request) -> Dict[str, Any]:
    db = get_db(request)
    return {"runs": db.list_pipeline_runs()}


@api_router.get("/api/pipeline-runs/{run_id}")
async def get_pipeline_run(request: Request, run_id: str) -> Dict[str, Any]:
    db = get_db(request)
    run = db.get_pipeline_run(run_id)
    if run is None:
        raise NotFoundError(f"Pipeline run {run_id} not found")

    stage_runs = db.list_stage_runs(run_id)
    return {"run": run, "stage_runs": stage_runs}


@api_router.post("/api/pipeline-runs/{run_id}/approve")
async def approve_pipeline_run(
    request: Request, run_id: str, req: ApprovePipelineRunRequest
) -> Dict[str, Any]:
    pe = get_pipeline_engine(request)
    result = await pe.approve_pipeline_run(run_id, req.note)
    return result


@api_router.post("/api/pipeline-runs/{run_id}/stop")
async def stop_pipeline_run(request: Request, run_id: str) -> Dict[str, Any]:
    pe = get_pipeline_engine(request)
    result = await pe.stop_pipeline_run(run_id)
    return result


# --- Team Configuration Endpoints ---

@api_router.get("/api/teams")
async def list_teams(request: Request) -> Dict[str, Any]:
    db = get_db(request)
    return {"teams": db.list_teams()}


@api_router.post("/api/teams", status_code=status.HTTP_201_CREATED)
async def create_team(request: Request, req: SaveTeamRequest) -> Dict[str, Any]:
    db = get_db(request)
    members_dicts = [m.model_dump() for m in req.members] if req.members else []
    normalized_members = TeamEngine.normalize_team_members(members_dicts)
    team = db.create_team(
        name=req.name,
        description=req.description,
        max_rework_loops=req.max_rework_loops,
        max_question_rounds=req.max_question_rounds,
        max_total_turns=req.max_total_turns,
        turn_timeout_sec=req.turn_timeout_sec,
        run_timeout_sec=req.run_timeout_sec,
        auto_approve_mockup=req.auto_approve_mockup,
        members=normalized_members,
    )
    return team


@api_router.get("/api/teams/{team_id}")
async def get_team(request: Request, team_id: str) -> Dict[str, Any]:
    db = get_db(request)
    team = db.get_team(team_id)
    if team is None:
        raise NotFoundError(f"Team {team_id} not found")
    return team


@api_router.put("/api/teams/{team_id}")
async def update_team(
    request: Request, team_id: str, req: SaveTeamRequest
) -> Dict[str, Any]:
    db = get_db(request)
    existing = db.get_team(team_id)
    if existing is None:
        raise NotFoundError(f"Team {team_id} not found")

    members_dicts = [m.model_dump() for m in req.members] if req.members else []
    normalized_members = TeamEngine.normalize_team_members(members_dicts)
    updated = db.update_team(
        team_id=team_id,
        name=req.name,
        description=req.description,
        max_rework_loops=req.max_rework_loops,
        max_question_rounds=req.max_question_rounds,
        max_total_turns=req.max_total_turns,
        turn_timeout_sec=req.turn_timeout_sec,
        run_timeout_sec=req.run_timeout_sec,
        auto_approve_mockup=req.auto_approve_mockup,
        members=normalized_members,
    )
    if updated is None:
        raise NotFoundError(f"Team {team_id} not found")
    return updated


@api_router.delete("/api/teams/{team_id}")
async def delete_team(request: Request, team_id: str) -> Dict[str, Any]:
    db = get_db(request)
    team = db.get_team(team_id)
    if team is None:
        raise NotFoundError(f"Team {team_id} not found")

    if db.count_teams() <= 1:
        raise ConflictError("Cannot delete the last remaining team")

    if db.has_active_team_runs(team_id):
        raise ConflictError(
            f"Team {team_id} has active runs and cannot be deleted"
        )

    db.delete_team(team_id)
    return {"deleted": True, "team_id": team_id}


# --- Team Run Lifecycle Endpoints ---

@api_router.post(
    "/api/teams/{team_id}/runs", status_code=status.HTTP_202_ACCEPTED
)
async def start_team_run_for_team(
    request: Request, team_id: str, req: StartTeamRunRequest
) -> Dict[str, Any]:
    te = get_team_engine(request)
    result = await te.start_team_run(team_id_or_request=team_id, request=req)
    return result


@api_router.post("/api/team-runs", status_code=status.HTTP_202_ACCEPTED)
async def start_team_run(
    request: Request, req: StartTeamRunRequest
) -> Dict[str, Any]:
    te = get_team_engine(request)
    result = await te.start_team_run(team_id_or_request=req)
    return result


@api_router.get("/api/team-runs")
async def list_team_runs(request: Request) -> Dict[str, Any]:
    te = get_team_engine(request)
    return {"runs": te.list_team_runs()}


@api_router.get("/api/team-runs/{run_id}")
async def get_team_run(request: Request, run_id: str) -> Dict[str, Any]:
    te = get_team_engine(request)
    return te.get_run_detail(run_id)


@api_router.delete("/api/team-runs/{run_id}")
async def delete_team_run(request: Request, run_id: str) -> Dict[str, Any]:
    db = get_db(request)
    te = get_team_engine(request)
    run = db.get_team_run(run_id)
    if run is None:
        raise NotFoundError(f"Team run {run_id} not found")
    if run.get("status") in (
        TeamRunStatus.RUNNING.value,
        TeamRunStatus.WAITING_USER.value,
    ):
        await te.stop_run(run_id)
    run = db.get_team_run(run_id) or run
    if not run.get("resolution"):
        # Remove the run's worktree and baton/<run_id> branch from the user's repo;
        # otherwise they outlive the run with nothing in the UI pointing at them.
        await asyncio.to_thread(finalize_workspace, run, "discard")
    deleted = db.delete_team_run(run_id)
    return {"deleted": deleted, "run_id": run_id}


# --- Team Run Touchpoint & Inspection Endpoints ---

@api_router.post("/api/team-runs/{run_id}/answer")
async def answer_team_questions(
    request: Request, run_id: str, req: AnswerTeamQuestionsRequest
) -> Dict[str, Any]:
    te = get_team_engine(request)
    result = await te.answer_questions(run_id, req)
    return result


@api_router.post("/api/team-runs/{run_id}/mockup-review")
async def review_team_mockup(
    request: Request, run_id: str, req: ReviewTeamMockupRequest
) -> Dict[str, Any]:
    te = get_team_engine(request)
    result = await te.review_mockup(run_id, req)
    return result


@api_router.get("/api/team-runs/{run_id}/mockup")
async def get_team_run_mockup(
    request: Request, run_id: str, v: Optional[int] = None
) -> Response:
    te = get_team_engine(request)
    mockup_path = te.get_mockup_path(run_id, version=v)
    try:
        content = mockup_path.read_bytes()
    except OSError as exc:
        raise NotFoundError(f"Unable to read mockup file: {exc}")
    headers = {
        "Content-Security-Policy": MOCKUP_CSP,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    return Response(
        content=content,
        media_type="text/html; charset=utf-8",
        headers=headers,
    )


@api_router.get("/api/team-runs/{run_id}/diff")
async def get_team_run_diff(request: Request, run_id: str) -> Dict[str, Any]:
    te = get_team_engine(request)
    result = await te.get_run_diff(run_id)
    return result


@api_router.post("/api/team-runs/{run_id}/resume")
async def resume_team_run(
    request: Request, run_id: str, req: ResumeTeamRunRequest
) -> Dict[str, Any]:
    te = get_team_engine(request)
    result = await te.resume_run(run_id, req)
    return result


@api_router.post("/api/team-runs/{run_id}/signoff")
async def signoff_team_run(
    request: Request, run_id: str, req: SignoffTeamRunRequest
) -> Dict[str, Any]:
    te = get_team_engine(request)
    result = await te.signoff_run(run_id, req)
    return result


@api_router.post("/api/team-runs/{run_id}/stop")
async def stop_team_run(request: Request, run_id: str) -> Dict[str, Any]:
    te = get_team_engine(request)
    result = await te.stop_run(run_id)
    return result


@api_router.get("/api/events")
async def sse_events(request: Request) -> StreamingResponse:
    bus = get_bus(request)

    async def event_generator() -> AsyncIterator[str]:
        async for item in bus.subscribe():
            if await request.is_disconnected():
                break
            event_name = item.get("event", "message")
            data_json = json.dumps(item.get("data", {}))
            yield f"event: {event_name}\ndata: {data_json}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Application Factory ---

def create_app(config: Optional[AppConfig] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = config or getattr(app.state, "config", None) or load_config()
        app.state.config = cfg

        db = getattr(app.state, "db", None) or Database(cfg.db_path)
        db.recover_orphaned_states()

        bus = getattr(app.state, "bus", None) or EventBus()
        session_manager = getattr(app.state, "session_manager", None) or SessionManager(
            cfg, db, bus
        )
        pipeline_engine = getattr(app.state, "pipeline_engine", None) or PipelineEngine(
            db, session_manager, bus
        )
        team_engine = getattr(app.state, "team_engine", None) or TeamEngine(
            db, session_manager, bus, cfg
        )

        app.state.db = db
        app.state.bus = bus
        app.state.session_manager = session_manager
        app.state.pipeline_engine = pipeline_engine
        app.state.team_engine = team_engine

        try:
            yield
        finally:
            # Let active team runs survive a restart as resumable 'interrupted' runs.
            team_engine.shutdown()
            await session_manager.stop_all()

    app = FastAPI(title="Agent Relay", lifespan=lifespan)
    if config is not None:
        app.state.config = config

    app.add_exception_handler(NotFoundError, not_found_handler)
    app.add_exception_handler(ConflictError, conflict_handler)
    app.add_exception_handler(UnprocessableError, unprocessable_handler)
    app.add_exception_handler(ValidationError, validation_handler)
    app.add_exception_handler(ValueError, value_error_handler)

    app.include_router(api_router)

    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.is_dir():
        app.mount(
            "/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend"
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run("backend.main:app", host=cfg.host, port=cfg.port, reload=False)
