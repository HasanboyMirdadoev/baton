from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Set

from backend.config import AppConfig
from backend.db import Database
from backend.models import (
    ConflictError,
    NormalizedEvent,
    NormalizedEventType,
    NotFoundError,
    PermissionLevel,
    ProviderId,
    RoleId,
    SessionStatus,
    TurnStatus,
    utc_now_iso,
)
from backend.providers import StreamParseState, get_adapter
from backend.pubsub import EventBus
from backend.roles import (
    ROLE_DEFINITIONS,
    resolve_system_prompt,
    resolve_team_system_prompt,
)

_STREAM_READER_LIMIT = 32 * 1024 * 1024  # 32 MiB


async def _safe_readline(stream: asyncio.StreamReader) -> bytes:
    chunks: List[bytes] = []
    while True:
        try:
            chunk = await stream.readuntil(b"\n")
            if not chunks:
                return chunk
            chunks.append(chunk)
            return b"".join(chunks)
        except asyncio.IncompleteReadError as exc:
            if not chunks:
                return exc.partial
            chunks.append(exc.partial)
            return b"".join(chunks)
        except asyncio.LimitOverrunError as exc:
            chunk = await stream.read(max(exc.consumed, 1))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
        except ValueError:
            chunk = await stream.read(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            if b"\n" in chunk:
                return b"".join(chunks)


class SessionManager:
    def __init__(self, config: AppConfig, db: Database, bus: EventBus) -> None:
        self.config = config
        self.db = db
        self.bus = bus
        self._active_procs: Dict[str, asyncio.subprocess.Process] = {}
        self._active_tasks: Dict[str, asyncio.Task[Dict[str, Any]]] = {}
        self._active_session_turns: Dict[str, str] = {}
        self._stopped_turns: Set[str] = set()

    def is_session_running(self, session_id: str) -> bool:
        return (
            session_id in self._active_procs
            or session_id in self._active_session_turns
        )

    async def start_turn(
        self,
        session_id: str,
        prompt: str,
        pipeline_run_id: Optional[str] = None,
        stage_run_id: Optional[str] = None,
        timeout_sec: Optional[int] = None,
        team_run_id: Optional[str] = None,
    ) -> str:
        """
        Validates session exists and is not currently running.
        Creates a new row in `turns` with status='running'.
        Schedules `self._execute_turn(...)` as an asyncio.Task and returns `turn_id` immediately.
        """
        session = self.db.get_session(session_id)
        if session is None:
            raise NotFoundError(f"Session {session_id} not found")

        if self.is_session_running(session_id):
            raise ConflictError(f"Session {session_id} is already running a turn")

        turn = self.db.create_turn(
            session_id=session_id,
            prompt=prompt,
            pipeline_run_id=pipeline_run_id,
            stage_run_id=stage_run_id,
            team_run_id=team_run_id,
        )
        turn_id: str = turn["id"]
        self._active_session_turns[session_id] = turn_id

        updated_session = self.db.get_session(session_id) or session
        await self.bus.publish("session_updated", {"session": updated_session})
        await self.bus.publish("turn_started", {"turn": turn})

        spawned_event = asyncio.Event()
        task: asyncio.Task[Dict[str, Any]] = asyncio.create_task(
            self._execute_turn(updated_session, turn, timeout_sec, spawned_event)
        )
        self._active_tasks[turn_id] = task
        await spawned_event.wait()
        return turn_id

    async def run_turn_and_wait(
        self,
        session_id: str,
        prompt: str,
        pipeline_run_id: Optional[str] = None,
        stage_run_id: Optional[str] = None,
        timeout_sec: Optional[int] = None,
        team_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Used by PipelineEngine and TeamEngine: starts a turn and awaits its completion, returning
        the final turn record dict from SQLite.
        """
        turn_id = await self.start_turn(
            session_id=session_id,
            prompt=prompt,
            pipeline_run_id=pipeline_run_id,
            stage_run_id=stage_run_id,
            timeout_sec=timeout_sec,
            team_run_id=team_run_id,
        )
        task = self._active_tasks.get(turn_id)
        if task is not None:
            result = await task
            return self.db.get_turn(turn_id) or result
        turn = self.db.get_turn(turn_id)
        assert turn is not None
        return turn

    async def _terminate_proc(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass

        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def _execute_turn(
        self,
        session: Dict[str, Any],
        turn: Dict[str, Any],
        timeout_sec: Optional[int],
        spawned_event: asyncio.Event,
    ) -> Dict[str, Any]:
        session_id: str = session["id"]
        turn_id: str = turn["id"]
        start_monotonic = time.monotonic()
        effective_timeout = (
            timeout_sec
            if (timeout_sec is not None and timeout_sec > 0)
            else self.config.default_turn_timeout_sec
        )

        adapter = get_adapter(ProviderId(session["provider"]), self.config)
        is_first_turn = session.get("provider_session_id") is None
        role_enum = RoleId(session["role"])
        session_permission = PermissionLevel(session["permission_level"])
        if (
            session.get("team_run_id")
            or turn.get("team_run_id")
            or role_enum not in ROLE_DEFINITIONS
        ):
            sys_prompt = resolve_team_system_prompt(
                role_enum, session.get("custom_prompt"), session_permission
            )
        else:
            sys_prompt = resolve_system_prompt(
                role_enum, session.get("custom_prompt"), session_permission
            )
        cmd = adapter.build_command(
            prompt=turn["prompt"],
            model=session["model"],
            permission=session_permission,
            system_prompt=sys_prompt,
            provider_conversation_id=session.get("provider_session_id"),
            is_first_turn=is_first_turn,
        )

        self.config.turns_dir.mkdir(parents=True, exist_ok=True)
        norm_path = self.config.turns_dir / f"{turn_id}.jsonl"
        raw_path = self.config.turns_dir / f"{turn_id}.raw.jsonl"
        stderr_path = self.config.turns_dir / f"{turn_id}.stderr.log"

        state = StreamParseState()
        tool_call_count = 0
        failed_tool_count = 0
        timed_out = False
        spawn_error: Optional[str] = None
        proc: Optional[asyncio.subprocess.Process] = None

        try:
            with open(norm_path, "w", encoding="utf-8") as norm_file, open(
                raw_path, "w", encoding="utf-8"
            ) as raw_file, open(stderr_path, "w", encoding="utf-8") as stderr_file:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=session["working_dir"],
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=_STREAM_READER_LIMIT,
                        start_new_session=True,
                        env={**os.environ, "BATON_PYTHON": sys.executable},
                    )
                    self._active_procs[session_id] = proc
                except Exception as exc:
                    spawn_error = f"Failed to spawn CLI subprocess: {exc}"
                finally:
                    spawned_event.set()

                if proc is not None:
                    if turn_id in self._stopped_turns:
                        await self._terminate_proc(proc)

                    async def _read_stdout() -> None:
                        nonlocal tool_call_count, failed_tool_count
                        assert proc is not None and proc.stdout is not None
                        while True:
                            line_bytes = await _safe_readline(proc.stdout)
                            if not line_bytes:
                                break
                            line_str = line_bytes.decode("utf-8", errors="replace")
                            raw_file.write(
                                line_str if line_str.endswith("\n") else line_str + "\n"
                            )
                            raw_file.flush()

                            events = adapter.parse_stream_line(line_str, state)

                            for event in events:
                                if event.type == NormalizedEventType.TOOL_USE:
                                    tool_call_count += 1
                                elif (
                                    event.type == NormalizedEventType.TOOL_RESULT
                                    and event.is_error
                                ):
                                    failed_tool_count += 1

                                norm_file.write(event.model_dump_json() + "\n")
                                norm_file.flush()

                                await self.bus.publish(
                                    "turn_event",
                                    {
                                        "session_id": session_id,
                                        "turn_id": turn_id,
                                        "pipeline_run_id": turn.get("pipeline_run_id"),
                                        "stage_run_id": turn.get("stage_run_id"),
                                        "team_run_id": turn.get("team_run_id")
                                        or session.get("team_run_id"),
                                        "role": session.get("role"),
                                        "event": event.model_dump(),
                                    },
                                )

                    async def _read_stderr() -> None:
                        assert proc is not None and proc.stderr is not None
                        while True:
                            line_bytes = await _safe_readline(proc.stderr)
                            if not line_bytes:
                                break
                            line_str = line_bytes.decode("utf-8", errors="replace")
                            stderr_file.write(line_str)
                            stderr_file.flush()

                    async def _consume_and_wait() -> None:
                        assert proc is not None
                        await asyncio.gather(_read_stdout(), _read_stderr())
                        await proc.wait()

                    try:
                        await asyncio.wait_for(
                            _consume_and_wait(), timeout=float(effective_timeout)
                        )
                    except asyncio.TimeoutError:
                        timed_out = True
                        await self._terminate_proc(proc)
                    except asyncio.CancelledError:
                        await self._terminate_proc(proc)
                        raise
                    except Exception as exc:
                        spawn_error = f"Stream reader error: {exc}"
                        await self._terminate_proc(proc)

            # Determine final TurnStatus and SessionStatus
            if turn_id in self._stopped_turns:
                final_turn_status = TurnStatus.STOPPED
                final_session_status = SessionStatus.STOPPED
                error_message: Optional[str] = None
            elif timed_out:
                final_turn_status = TurnStatus.TIMEOUT
                final_session_status = SessionStatus.ERROR
                error_message = f"Turn timed out after {effective_timeout}s"
            elif (
                proc is not None
                and proc.returncode == 0
                and state.final_status == "SUCCESS"
            ):
                final_turn_status = TurnStatus.COMPLETED
                final_session_status = SessionStatus.IDLE
                error_message = None
            else:
                final_turn_status = TurnStatus.ERROR
                final_session_status = SessionStatus.ERROR
                stderr_tail_lines: List[str] = []
                if stderr_path.exists():
                    try:
                        stderr_content = stderr_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        non_empty = [
                            ln.strip()
                            for ln in stderr_content.splitlines()
                            if ln.strip()
                        ]
                        stderr_tail_lines = non_empty[-15:]
                    except OSError:
                        pass

                stderr_tail = "\n".join(stderr_tail_lines)
                base_err = state.error_message or spawn_error
                if base_err and stderr_tail:
                    if stderr_tail not in base_err:
                        error_message = f"{base_err}\n{stderr_tail}"
                    else:
                        error_message = base_err
                elif stderr_tail:
                    error_message = stderr_tail
                elif base_err:
                    error_message = base_err
                else:
                    rc = proc.returncode if proc is not None else "unknown"
                    error_message = f"CLI process exited with code {rc}"

            response_text = (
                state.final_response
                if state.final_response is not None
                else "".join(state.accumulated_text)
            )
            finished_at = utc_now_iso()
            duration_ms = int((time.monotonic() - start_monotonic) * 1000)

            self._active_procs.pop(session_id, None)
            self._active_session_turns.pop(session_id, None)

            updated_turn = self.db.update_turn(
                turn_id=turn_id,
                status=final_turn_status.value,
                response_text=response_text,
                error_message=error_message,
                tool_call_count=tool_call_count,
                failed_tool_count=failed_tool_count,
                denied_actions=state.denied_actions,
                finished_at=finished_at,
                duration_ms=duration_ms,
            )
            new_provider_session_id: Optional[str] = None
            if (
                final_turn_status == TurnStatus.COMPLETED
                and session.get("provider_session_id") is None
                and state.provider_conversation_id
            ):
                new_provider_session_id = state.provider_conversation_id

            updated_session = self.db.update_session(
                session_id=session_id,
                status=final_session_status.value,
                provider_session_id=new_provider_session_id,
            )

            if updated_turn is not None:
                await self.bus.publish("turn_completed", {"turn": updated_turn})
            if updated_session is not None:
                await self.bus.publish("session_updated", {"session": updated_session})

            return updated_turn or turn
        finally:
            self._active_procs.pop(session_id, None)
            self._active_session_turns.pop(session_id, None)
            self._active_tasks.pop(turn_id, None)
            self._stopped_turns.discard(turn_id)
            spawned_event.set()

    async def stop_session(self, session_id: str) -> bool:
        """
        If a subprocess is running for `session_id`, marks the active turn as `stopped`,
        sends SIGTERM to its process group, waits up to 3.0s, sends SIGKILL if needed,
        and updates session status to 'stopped'. Returns True if a running process was stopped.
        """
        proc = self._active_procs.get(session_id)
        turn_id = self._active_session_turns.get(session_id)
        if proc is None and turn_id is None:
            return False

        if turn_id is not None:
            self._stopped_turns.add(turn_id)

        if proc is None:
            for _ in range(20):
                proc = self._active_procs.get(session_id)
                if proc is not None or session_id not in self._active_session_turns:
                    break
                await asyncio.sleep(0.01)

        if proc is not None:
            await self._terminate_proc(proc)

        if turn_id is not None:
            task = self._active_tasks.get(turn_id)
            if task is not None and not task.done():
                await asyncio.gather(task, return_exceptions=True)

        return True

    async def stop_all(self) -> None:
        """Stops all currently active session subprocesses cleanly."""
        active_session_ids = list(self._active_procs.keys())
        for session_id in active_session_ids:
            await self.stop_session(session_id)

    def read_turn_events(self, turn_id: str) -> List[NormalizedEvent]:
        """Reads `~/.agent-relay/turns/<turn_id>.jsonl` and returns parsed NormalizedEvent list."""
        path = self.config.turns_dir / f"{turn_id}.jsonl"
        if not path.exists() or not path.is_file():
            return []
        events: List[NormalizedEvent] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        events.append(NormalizedEvent.model_validate_json(stripped))
                    except Exception:
                        continue
        except OSError:
            return []
        return events
