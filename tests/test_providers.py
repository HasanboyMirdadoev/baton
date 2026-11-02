from __future__ import annotations

import asyncio
import json
from pathlib import Path

from backend.config import AppConfig
from backend.models import NormalizedEventType, PermissionLevel, ProviderId
from backend.providers.antigravity import agy_settings_json
from backend.providers.safe_commands import claude_allowed_tools
from backend.providers import (
    AntigravityAdapter,
    ClaudeAdapter,
    StreamParseState,
    get_adapter,
    list_providers_metadata,
)

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "docs" / "reference" / "samples"


def test_claude_build_command_first_and_subsequent_turns() -> None:
    adapter = ClaudeAdapter("/usr/local/bin/claude")

    # Turn 1 with READONLY and system prompt
    cmd_readonly = adapter.build_command(
        prompt="-list files",
        model="sonnet",
        permission=PermissionLevel.READONLY,
        system_prompt="You are a designer.",
        provider_conversation_id=None,
        is_first_turn=True,
    )
    assert cmd_readonly == [
        "/usr/local/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        "sonnet",
        "--permission-mode",
        "plan",
        "--permission-prompts",
        "none",
        "--disallowedTools",
        "Edit,Write,NotebookEdit",
        "--allowedTools",
        ",".join(claude_allowed_tools()),
        "--append-system-prompt",
        "You are a designer.",
        "--",
        "-list files",
    ]

    # Turn 2 with EDIT and resume ID (system prompt omitted on resume)
    cmd_edit_resume = adapter.build_command(
        prompt="Now update foo.py",
        model="opus",
        permission=PermissionLevel.EDIT,
        system_prompt="You are an implementer.",
        provider_conversation_id="conv-uuid-123",
        is_first_turn=False,
    )
    assert cmd_edit_resume == [
        "/usr/local/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--resume",
        "conv-uuid-123",
        "--model",
        "opus",
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--allowedTools",
        ",".join(claude_allowed_tools()),
        "--",
        "Now update foo.py",
    ]

    # FULL permission
    cmd_full = adapter.build_command(
        prompt="Run pytest",
        model="haiku",
        permission=PermissionLevel.FULL,
        system_prompt="",
        provider_conversation_id=None,
        is_first_turn=True,
    )
    assert "--permission-mode" in cmd_full
    assert "bypassPermissions" in cmd_full
    assert "--dangerously-skip-permissions" in cmd_full
    assert "--append-system-prompt" not in cmd_full


def test_antigravity_build_command_first_and_subsequent_turns() -> None:
    adapter = AntigravityAdapter("/usr/local/bin/agy")

    # Turn 1 with READONLY and system prompt prepended
    cmd_t1 = adapter.build_command(
        prompt="Inspect architecture",
        model="gemini-4-argon-high",
        permission=PermissionLevel.READONLY,
        system_prompt="Role instructions.",
        provider_conversation_id=None,
        is_first_turn=True,
    )
    assert cmd_t1 == [
        "/usr/local/bin/agy",
        "--output-format",
        "stream-json",
        "--mode",
        "plan",
        "--settings",
        agy_settings_json(PermissionLevel.READONLY),
        "--model",
        "gemini-4-argon-high",
        "-p",
        "Role instructions.\n\n# Task\n\nInspect architecture",
    ]

    # Turn 2 with EDIT and --conversation (no system prompt prepended)
    cmd_t2 = adapter.build_command(
        prompt="Follow up question",
        model="gemini-3.8-flash-medium",
        permission=PermissionLevel.EDIT,
        system_prompt="Role instructions.",
        provider_conversation_id="agy-conv-99",
        is_first_turn=False,
    )
    assert cmd_t2 == [
        "/usr/local/bin/agy",
        "--output-format",
        "stream-json",
        "--mode",
        "accept-edits",
        "--settings",
        agy_settings_json(PermissionLevel.EDIT),
        "--model",
        "gemini-3.8-flash-medium",
        "--conversation",
        "agy-conv-99",
        "-p",
        "Follow up question",
    ]

    # FULL permission
    cmd_full = adapter.build_command(
        prompt="Run all",
        model="claude-sonnet-5-5-medium",
        permission=PermissionLevel.FULL,
        system_prompt=None,
        provider_conversation_id=None,
        is_first_turn=True,
    )
    assert cmd_full == [
        "/usr/local/bin/agy",
        "--output-format",
        "stream-json",
        "--mode",
        "accept-edits",
        "--dangerously-skip-permissions",
        "--model",
        "claude-sonnet-5-5-medium",
        "-p",
        "Run all",
    ]


def test_claude_parse_stream_line() -> None:
    adapter = ClaudeAdapter()
    state = StreamParseState()

    # Malformed & blank lines ignored safely
    assert adapter.parse_stream_line("", state) == []
    assert adapter.parse_stream_line("not valid json {{{", state) == []

    # 1. Init
    init_events = adapter.parse_stream_line(
        json.dumps({"type": "system", "subtype": "init", "session_id": "claude-s1"}),
        state,
    )
    assert len(init_events) == 1
    assert init_events[0].type == NormalizedEventType.SESSION_INIT
    assert state.provider_conversation_id == "claude-s1"

    # 2. Thinking delta
    think_events = adapter.parse_stream_line(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "Step 1..."},
                },
            }
        ),
        state,
    )
    assert len(think_events) == 1
    assert think_events[0].type == NormalizedEventType.THINKING_DELTA
    assert think_events[0].text == "Step 1..."

    # 3. Text delta
    text_events = adapter.parse_stream_line(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello world"},
                },
            }
        ),
        state,
    )
    assert len(text_events) == 1
    assert text_events[0].type == NormalizedEventType.TEXT_DELTA
    assert text_events[0].text == "Hello world"
    assert state.saw_text_delta is True

    # 4. Assistant tool_use (and duplicate text suppressed because saw_text_delta is True)
    asst_events = adapter.parse_stream_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Hello world"},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Read",
                            "input": {"path": "a.py"},
                        },
                    ]
                },
            }
        ),
        state,
    )
    assert len(asst_events) == 1
    assert asst_events[0].type == NormalizedEventType.TOOL_USE
    assert asst_events[0].tool_call_id == "tu_1"
    assert asst_events[0].tool_name == "Read"
    assert asst_events[0].tool_input == {"path": "a.py"}

    # 5. User tool_result
    user_events = adapter.parse_stream_line(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [{"type": "text", "text": "print(1)"}],
                            "is_error": False,
                        }
                    ]
                },
            }
        ),
        state,
    )
    assert len(user_events) == 1
    assert user_events[0].type == NormalizedEventType.TOOL_RESULT
    assert user_events[0].tool_call_id == "tu_1"
    assert user_events[0].tool_output == "print(1)"
    assert user_events[0].is_error is False

    # 6. Result with denied actions
    res_events = adapter.parse_stream_line(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Hello world",
                "permission_denials": [{"tool_name": "Bash"}],
            }
        ),
        state,
    )
    assert len(res_events) == 1
    assert res_events[0].type == NormalizedEventType.TURN_RESULT
    assert state.final_status == "SUCCESS"
    assert state.final_response == "Hello world"
    assert state.denied_actions == ["Bash"]

    # 7. Error result
    err_state = StreamParseState()
    err_events = adapter.parse_stream_line(
        json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "error": "Rate limit exceeded",
            }
        ),
        err_state,
    )
    assert len(err_events) == 1
    assert err_events[0].is_error is True
    assert err_state.final_status == "ERROR"
    assert err_state.error_message == "Rate limit exceeded"


def test_antigravity_parse_stream_line() -> None:
    adapter = AntigravityAdapter()
    state = StreamParseState()

    assert adapter.parse_stream_line("   ", state) == []
    assert adapter.parse_stream_line("invalid json", state) == []

    # 1. First line with conversation_id + thinking step_update
    ev1 = adapter.parse_stream_line(
        json.dumps(
            {
                "conversation_id": "agy-c1",
                "step_update": {
                    "step_index": 0,
                    "step_type": "thinking",
                    "text_delta": "Planning...",
                },
            }
        ),
        state,
    )
    assert len(ev1) == 2
    assert ev1[0].type == NormalizedEventType.SESSION_INIT
    assert ev1[0].provider_conversation_id == "agy-c1"
    assert ev1[1].type == NormalizedEventType.THINKING_DELTA
    assert ev1[1].text == "Planning..."

    # 2. Tool RUNNING then DONE
    ev_tool_run = adapter.parse_stream_line(
        json.dumps(
            {
                "conversation_id": "agy-c1",
                "step_update": {
                    "step_index": 1,
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "state": "RUNNING",
                    "tool_info": {"parameters": {"file": "main.py"}},
                },
            }
        ),
        state,
    )
    assert len(ev_tool_run) == 1
    assert ev_tool_run[0].type == NormalizedEventType.TOOL_USE
    assert ev_tool_run[0].tool_call_id == "1"
    assert ev_tool_run[0].tool_name == "view_file"

    ev_tool_done = adapter.parse_stream_line(
        json.dumps(
            {
                "conversation_id": "agy-c1",
                "step_update": {
                    "step_index": 1,
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "state": "DONE",
                    "tool_info": {
                        "parameters": {"file": "main.py"},
                        "output": "contents",
                    },
                },
            }
        ),
        state,
    )
    assert len(ev_tool_done) == 1
    assert ev_tool_done[0].type == NormalizedEventType.TOOL_RESULT
    assert ev_tool_done[0].tool_output == "contents"
    assert ev_tool_done[0].is_error is False

    # 3. Agent response delta
    ev_text = adapter.parse_stream_line(
        json.dumps(
            {
                "conversation_id": "agy-c1",
                "step_update": {
                    "step_index": 2,
                    "step_type": "agent_response",
                    "text_delta": "Done!",
                },
            }
        ),
        state,
    )
    assert len(ev_text) == 1
    assert ev_text[0].type == NormalizedEventType.TEXT_DELTA
    assert ev_text[0].text == "Done!"

    # 4. Final result with denied_actions
    ev_res = adapter.parse_stream_line(
        json.dumps(
            {
                "conversation_id": "agy-c1",
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Done!",
                    "denied_actions": [{"display_name": "run_command"}],
                },
            }
        ),
        state,
    )
    assert len(ev_res) == 1
    assert ev_res[0].type == NormalizedEventType.TURN_RESULT
    assert state.final_status == "SUCCESS"
    assert state.final_response == "Done!"
    assert state.denied_actions == ["run_command"]


def test_list_models_and_registry(app_config: AppConfig) -> None:
    claude_adapter = get_adapter(ProviderId.CLAUDE, app_config)
    agy_adapter = get_adapter(ProviderId.ANTIGRAVITY, app_config)

    claude_models = asyncio.run(claude_adapter.list_models())
    assert any(m["id"] == "sonnet" for m in claude_models)

    agy_models = asyncio.run(agy_adapter.list_models())
    agy_ids = [m["id"] for m in agy_models]
    assert "gemini-4-argon-high" in agy_ids
    assert "gemini-3.8-flash-medium" in agy_ids
    assert "claude-sonnet-5-5-medium" in agy_ids

    meta_providers = asyncio.run(list_providers_metadata(app_config))
    assert len(meta_providers) == 2
    assert meta_providers[0]["id"] == "claude"
    assert meta_providers[1]["id"] == "antigravity"


def test_real_claude_sample_error_sequence_not_treated_as_reply() -> None:
    adapter = ClaudeAdapter()
    for sample_name in ("claude-turn1.jsonl", "claude-turn2.jsonl"):
        sample_path = SAMPLES_DIR / sample_name
        lines = sample_path.read_text(encoding="utf-8").splitlines()
        state = StreamParseState()
        all_events = []
        for line in lines:
            all_events.extend(adapter.parse_stream_line(line, state))

        assert state.provider_conversation_id == "ca1ef2bb-bff5-460f-a077-7cefa6b34e12"
        assert state.final_status == "ERROR"
        assert state.final_response == ""
        assert state.accumulated_text == []
        assert state.error_message == "Not logged in · Please run /login"
        assert not any(e.type == NormalizedEventType.TEXT_DELTA for e in all_events)
        result_events = [
            e for e in all_events if e.type == NormalizedEventType.TURN_RESULT
        ]
        assert len(result_events) == 1
        assert result_events[0].is_error is True
        assert result_events[0].text == ""


def test_claude_thinking_delta_not_duplicated_before_tool_use() -> None:
    adapter = ClaudeAdapter()
    state = StreamParseState()

    # Stream a thinking_delta before any text_delta
    ev_delta = adapter.parse_stream_line(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "Reasoning before tool call...",
                    },
                },
            }
        ),
        state,
    )
    assert len(ev_delta) == 1
    assert ev_delta[0].type == NormalizedEventType.THINKING_DELTA
    assert state.saw_thinking_delta is True
    assert state.saw_text_delta is False

    # Assistant message with thinking + tool_use (still no text_delta)
    ev_asst = adapter.parse_stream_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Reasoning before tool call...",
                        },
                        {
                            "type": "tool_use",
                            "id": "tu_think_1",
                            "name": "Read",
                            "input": {"file_path": "x.py"},
                        },
                    ]
                },
            }
        ),
        state,
    )
    assert len(ev_asst) == 1
    assert ev_asst[0].type == NormalizedEventType.TOOL_USE


def test_real_agy_samples_parsed_correctly() -> None:
    adapter = AntigravityAdapter()

    # Turn 1 sample: init, user_input, agent_response, tool ACTIVE/DONE, text_deltas, result
    t1_lines = (SAMPLES_DIR / "agy-turn1.jsonl").read_text(encoding="utf-8").splitlines()
    t1_state = StreamParseState()
    t1_events = []
    for line in t1_lines:
        t1_events.extend(adapter.parse_stream_line(line, t1_state))

    assert t1_state.provider_conversation_id == "def85a8e-95e8-4b77-8b13-7b331a2f47a1"
    assert t1_state.final_status == "SUCCESS"
    assert t1_state.final_response == "fastapi==0.115.6"

    init_events = [e for e in t1_events if e.type == NormalizedEventType.SESSION_INIT]
    assert len(init_events) == 1
    assert init_events[0].provider_conversation_id == "def85a8e-95e8-4b77-8b13-7b331a2f47a1"

    tool_uses = [e for e in t1_events if e.type == NormalizedEventType.TOOL_USE]
    assert len(tool_uses) == 1
    assert tool_uses[0].tool_call_id == "2"
    assert tool_uses[0].tool_name == "read_file"
    assert tool_uses[0].tool_input == {
        "EndLine": 5,
        "Path": "/Users/mac/MY/Benevia/agent-relay/requirements.txt",
        "StartLine": 1,
    }

    tool_results = [e for e in t1_events if e.type == NormalizedEventType.TOOL_RESULT]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "2"
    assert tool_results[0].tool_name == "read_file"
    assert tool_results[0].tool_output == "5 lines, 78 bytes"
    assert tool_results[0].is_error is False

    text_deltas = [e.text for e in t1_events if e.type == NormalizedEventType.TEXT_DELTA]
    assert "".join(t for t in text_deltas if t) == "fastapi==0.115.6\n"

    # Turn 2 sample: init, user_input, system_message, text_deltas, result
    t2_lines = (SAMPLES_DIR / "agy-turn2.jsonl").read_text(encoding="utf-8").splitlines()
    t2_state = StreamParseState()
    t2_events = []
    for line in t2_lines:
        t2_events.extend(adapter.parse_stream_line(line, t2_state))

    assert t2_state.provider_conversation_id == "def85a8e-95e8-4b77-8b13-7b331a2f47a1"
    assert t2_state.final_status == "SUCCESS"
    assert t2_state.final_response == "DONE"
    t2_results = [e for e in t2_events if e.type == NormalizedEventType.TURN_RESULT]
    assert len(t2_results) == 1
    assert t2_results[0].text == "DONE"
    assert t2_results[0].is_error is False

    # Also verify conversation_id capture from step_update when init line is absent,
    # and dict error message extraction on result
    no_init_state = StreamParseState()
    step_events = adapter.parse_stream_line(t1_lines[1], no_init_state)
    assert no_init_state.provider_conversation_id == "def85a8e-95e8-4b77-8b13-7b331a2f47a1"
    assert len(step_events) == 1
    assert step_events[0].type == NormalizedEventType.SESSION_INIT

    err_state = StreamParseState()
    adapter.parse_stream_line(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "def85a8e-95e8-4b77-8b13-7b331a2f47a1",
                    "status": "ERROR",
                    "error": {"message": "Quota exceeded for model"},
                },
            }
        ),
        err_state,
    )
    assert err_state.final_status == "ERROR"
    assert err_state.error_message == "Quota exceeded for model"


def test_antigravity_list_models_ignores_plain_words(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy_words.py"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('Available models for current session:')\n"
        "print('  gemini-4-argon-high    Gemini 4 Argon (High) [configured]')\n"
        "print('  gemini-3.8-flash-low   Gemini 3.8 Flash (Low) active')\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)

    adapter = AntigravityAdapter(str(fake_agy))
    models = asyncio.run(adapter.list_models())
    model_ids = [m["id"] for m in models]
    assert "gemini-4-argon-high" in model_ids
    assert "gemini-3.8-flash-low" in model_ids
    for plain_word in ("for", "current", "session", "argon", "high", "flash", "low", "configured", "active"):
        assert plain_word not in model_ids
