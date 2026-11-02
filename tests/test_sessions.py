from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.config import AppConfig, load_config, validate_working_dir
from backend.db import Database
from backend.models import ConflictError, SessionStatus, TurnStatus
from backend.session_manager import SessionManager, _safe_readline


def test_validate_working_dir_and_config(
    workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rejects root and system directories
    with pytest.raises(ValueError):
        validate_working_dir("/")
    with pytest.raises(ValueError):
        validate_working_dir("/System")
    with pytest.raises(ValueError):
        validate_working_dir("/etc")
    with pytest.raises(ValueError):
        validate_working_dir("")
    with pytest.raises(ValueError):
        validate_working_dir(str(workspace_dir / "does_not_exist"))

    # Accepts valid workspace directory
    resolved = validate_working_dir(str(workspace_dir))
    assert Path(resolved).is_dir()

    # Forbidden port check
    monkeypatch.setenv("AGENT_RELAY_PORT", "5000")
    with pytest.raises(ValueError):
        load_config()


def test_multi_turn_session_claude_and_agy(
    app_config: AppConfig,
    db: Database,
    session_manager: SessionManager,
    workspace_dir: Path,
) -> None:
    async def _run() -> None:
        # 1. Claude session: Turn 1 & Turn 2
        claude_sess = db.create_session(
            name="Claude Planner",
            provider="claude",
            model="sonnet",
            role="planner",
            custom_prompt="",
            permission_level="readonly",
            working_dir=str(workspace_dir),
        )
        turn1 = await session_manager.run_turn_and_wait(
            claude_sess["id"], "Create a plan for feature X"
        )
        assert turn1["status"] == TurnStatus.COMPLETED.value
        assert turn1["turn_number"] == 1
        assert turn1["tool_call_count"] == 1
        assert "resumed=False" in turn1["response_text"]

        updated_claude_sess = db.get_session(claude_sess["id"])
        assert updated_claude_sess is not None
        assert updated_claude_sess["provider_session_id"] == "fake-claude-uuid-1"
        assert updated_claude_sess["status"] == SessionStatus.IDLE.value

        # Verify .jsonl and .raw.jsonl files exist and are populated
        norm_file = app_config.turns_dir / f"{turn1['id']}.jsonl"
        raw_file = app_config.turns_dir / f"{turn1['id']}.raw.jsonl"
        assert norm_file.is_file()
        assert raw_file.is_file()
        events = session_manager.read_turn_events(turn1["id"])
        assert len(events) >= 4

        # Turn 2 resumes with fake-claude-uuid-1
        turn2 = await session_manager.run_turn_and_wait(
            claude_sess["id"], "Refine step 2 of the plan"
        )
        assert turn2["status"] == TurnStatus.COMPLETED.value
        assert turn2["turn_number"] == 2
        assert "resumed=True" in turn2["response_text"]
        assert "conv=fake-claude-uuid-1" in turn2["response_text"]

        # 2. Antigravity session: Turn 1 & Turn 2
        agy_sess = db.create_session(
            name="AGY Designer",
            provider="antigravity",
            model="gemini-4-argon-high",
            role="designer",
            custom_prompt="",
            permission_level="readonly",
            working_dir=str(workspace_dir),
        )
        agy_t1 = await session_manager.run_turn_and_wait(
            agy_sess["id"], "Design UI layout"
        )
        assert agy_t1["status"] == TurnStatus.COMPLETED.value
        assert "resumed=False" in agy_t1["response_text"]

        updated_agy_sess = db.get_session(agy_sess["id"])
        assert updated_agy_sess is not None
        assert updated_agy_sess["provider_session_id"] == "fake-agy-conv-1"

        agy_t2 = await session_manager.run_turn_and_wait(
            agy_sess["id"], "Add dark mode tokens"
        )
        assert agy_t2["status"] == TurnStatus.COMPLETED.value
        assert "resumed=True" in agy_t2["response_text"]
        assert "conv=fake-agy-conv-1" in agy_t2["response_text"]

    asyncio.run(_run())


def test_concurrent_turn_prevention_and_stop(
    db: Database,
    session_manager: SessionManager,
    workspace_dir: Path,
) -> None:
    async def _run() -> None:
        sess = db.create_session(
            name="Slow Session",
            provider="claude",
            model="sonnet",
            role="implementer",
            custom_prompt="",
            permission_level="edit",
            working_dir=str(workspace_dir),
        )
        turn_id = await session_manager.start_turn(
            sess["id"], "Long running task [SLOW]"
        )
        assert session_manager.is_session_running(sess["id"]) is True

        # Starting a second turn while running must raise ConflictError
        with pytest.raises(ConflictError):
            await session_manager.start_turn(sess["id"], "Concurrent turn")

        # Stop the running session
        stopped = await session_manager.stop_session(sess["id"])
        assert stopped is True
        assert session_manager.is_session_running(sess["id"]) is False

        turn = db.get_turn(turn_id)
        assert turn is not None
        assert turn["status"] == TurnStatus.STOPPED.value

        updated_sess = db.get_session(sess["id"])
        assert updated_sess is not None
        assert updated_sess["status"] == SessionStatus.STOPPED.value

    asyncio.run(_run())


def test_turn_timeout_and_fail_stderr_capture(
    db: Database,
    session_manager: SessionManager,
    workspace_dir: Path,
) -> None:
    async def _run() -> None:
        sess = db.create_session(
            name="Timeout & Error Session",
            provider="antigravity",
            model="gemini-4-argon-high",
            role="implementer",
            custom_prompt="",
            permission_level="edit",
            working_dir=str(workspace_dir),
        )

        # 1. Timeout test (1s timeout on [SLOW] turn)
        t_timeout = await session_manager.run_turn_and_wait(
            sess["id"], "Wait [SLOW]", timeout_sec=1
        )
        assert t_timeout["status"] == TurnStatus.TIMEOUT.value
        assert "timed out" in (t_timeout["error_message"] or "").lower()
        sess_after_timeout = db.get_session(sess["id"])
        assert sess_after_timeout is not None
        assert sess_after_timeout["status"] == SessionStatus.ERROR.value

        # 2. [FAIL] test captures stderr and sets status to error
        t_fail = await session_manager.run_turn_and_wait(
            sess["id"], "Trigger failure [FAIL]"
        )
        assert t_fail["status"] == TurnStatus.ERROR.value
        assert "Fatal CLI error occurred" in (t_fail["error_message"] or "")
        sess_after_fail = db.get_session(sess["id"])
        assert sess_after_fail is not None
        assert sess_after_fail["status"] == SessionStatus.ERROR.value

    asyncio.run(_run())


def test_failed_turn_1_does_not_persist_provider_session_id(
    db: Database,
    session_manager: SessionManager,
    workspace_dir: Path,
) -> None:
    async def _run() -> None:
        sess = db.create_session(
            name="Claude Reviewer",
            provider="claude",
            model="sonnet",
            role="reviewer",
            custom_prompt="",
            permission_level="readonly",
            working_dir=str(workspace_dir),
        )
        # Turn 1 fails after emitting init with session_id
        t1 = await session_manager.run_turn_and_wait(
            sess["id"], "Initial review [FAIL]"
        )
        assert t1["status"] == TurnStatus.ERROR.value
        assert t1["response_text"] == ""
        assert "Fatal CLI error occurred" in (t1["error_message"] or "")

        sess_after_t1 = db.get_session(sess["id"])
        assert sess_after_t1 is not None
        assert sess_after_t1["provider_session_id"] is None

        # Turn 2 succeeds: because Turn 1 failed, Turn 2 must still send system_prompt
        # and not pass --resume
        t2 = await session_manager.run_turn_and_wait(
            sess["id"], "Retry review after login"
        )
        assert t2["status"] == TurnStatus.COMPLETED.value
        assert "resumed=False" in t2["response_text"]
        assert "sysprompt=True" in t2["response_text"]

        sess_after_t2 = db.get_session(sess["id"])
        assert sess_after_t2 is not None
        assert sess_after_t2["provider_session_id"] == "fake-claude-uuid-1"

    asyncio.run(_run())


def test_oversized_stream_line_does_not_hang_or_crash_turn(
    db: Database,
    session_manager: SessionManager,
    workspace_dir: Path,
) -> None:
    async def _run() -> None:
        # 1. End-to-end turn with >100 KiB single NDJSON line (> 64 KiB default StreamReader limit)
        sess = db.create_session(
            name="Large Output Session",
            provider="claude",
            model="sonnet",
            role="implementer",
            custom_prompt="",
            permission_level="edit",
            working_dir=str(workspace_dir),
        )
        turn = await session_manager.run_turn_and_wait(
            sess["id"], "Read huge file [LARGE_LINE]"
        )
        assert turn["status"] == TurnStatus.COMPLETED.value
        events = session_manager.read_turn_events(turn["id"])
        tool_results = [e for e in events if e.type.value == "tool_result"]
        assert len(tool_results) == 1
        assert len(tool_results[0].tool_output or "") == 100_000

        # 2. Direct test of _safe_readline when a line exceeds StreamReader's internal limit
        small_reader = asyncio.StreamReader(limit=1024)
        big_payload = b"A" * 5000 + b"\nsecond line\n"
        small_reader.feed_data(big_payload)
        small_reader.feed_eof()

        line1 = await _safe_readline(small_reader)
        line2 = await _safe_readline(small_reader)
        eof_line = await _safe_readline(small_reader)
        assert line1 == b"A" * 5000 + b"\n"
        assert line2 == b"second line\n"
        assert eof_line == b""

    asyncio.run(_run())
