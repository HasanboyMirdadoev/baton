from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.db import Database
from backend.models import PermissionLevel, RoleId, TurnStatus
from backend.providers import AntigravityAdapter, ClaudeAdapter, StreamParseState
from backend.providers.safe_commands import (
    SAFE_READONLY_COMMANDS,
    agy_allow_rules,
    claude_allowed_tools,
)
from backend.roles import (
    permission_note,
    resolve_system_prompt,
    resolve_team_system_prompt,
)
from backend.session_manager import SessionManager
from backend.team_protocol import (
    build_denied_action_repair_prompt,
    build_format_repair_prompt,
    parse_team_message,
)


def _agy_cmd(permission: PermissionLevel) -> list:
    return AntigravityAdapter("agy").build_command(
        prompt="hi",
        model="gemini-3.8-flash-low",
        permission=permission,
        system_prompt=None,
        provider_conversation_id=None,
        is_first_turn=True,
    )


def _claude_cmd(permission: PermissionLevel) -> list:
    return ClaudeAdapter("claude").build_command(
        prompt="hi",
        model="sonnet",
        permission=permission,
        system_prompt=None,
        provider_conversation_id=None,
        is_first_turn=True,
    )


# ---------------------------------------------------------------------------
# argv: read-only command allow-rules per permission level
# ---------------------------------------------------------------------------

SAFE_WORDS = ["ls", "find", "cat", "head", "tail", "wc", "grep", "rg", "tree", "pwd", "file"]
SAFE_GIT = ["status", "diff", "log", "show", "ls-files"]


def test_safe_command_set_matches_spec() -> None:
    assert SAFE_READONLY_COMMANDS == SAFE_WORDS + [f"git {g}" for g in SAFE_GIT]
    for risky in ("rm", "touch", "mv", "cp", "git push", "git commit", "git checkout", "sh", "python"):
        assert risky not in SAFE_READONLY_COMMANDS


@pytest.mark.parametrize("perm", [PermissionLevel.READONLY, PermissionLevel.EDIT])
def test_agy_argv_has_allow_rules_for_non_full_roles(perm: PermissionLevel) -> None:
    cmd = _agy_cmd(perm)
    assert "--dangerously-skip-permissions" not in cmd
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    allow = settings["permissions"]["allow"]
    assert allow == agy_allow_rules()
    for word in SAFE_WORDS:
        assert f"command({word})" in allow
    for sub in SAFE_GIT:
        assert f"command(git {sub})" in allow
    assert set(settings) == {"permissions"} and set(settings["permissions"]) == {"allow"}
    # The prompt stays the final `-p <prompt>` pair.
    assert cmd[-2:] == ["-p", "hi"]


def test_agy_mode_flags_unchanged_per_level() -> None:
    ro, ed = _agy_cmd(PermissionLevel.READONLY), _agy_cmd(PermissionLevel.EDIT)
    assert ro[ro.index("--mode") + 1] == "plan"
    assert ed[ed.index("--mode") + 1] == "accept-edits"


def test_agy_full_is_unchanged_and_has_no_settings() -> None:
    cmd = _agy_cmd(PermissionLevel.FULL)
    assert "--settings" not in cmd
    assert cmd == [
        "agy",
        "--output-format",
        "stream-json",
        "--mode",
        "accept-edits",
        "--dangerously-skip-permissions",
        "--model",
        "gemini-3.8-flash-low",
        "-p",
        "hi",
    ]


def test_claude_readonly_allows_safe_bash_but_keeps_edit_tools_denied() -> None:
    cmd = _claude_cmd(PermissionLevel.READONLY)
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"  # anything unlisted is denied
    disallowed = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert disallowed == ["Edit", "Write", "NotebookEdit"]
    assert not any(t.startswith("Bash") for t in disallowed)  # would override the allow-list
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert allowed == claude_allowed_tools()
    for rule in ("Bash(ls:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)"):
        assert rule in allowed
    assert "Bash(find:*)" not in allowed  # find -exec/-delete is not understood by prefix rules
    assert not set(allowed) & set(disallowed)
    assert "--" in cmd and cmd.index("--allowedTools") < cmd.index("--")


def test_claude_edit_allows_safe_bash_and_keeps_accept_edits() -> None:
    cmd = _claude_cmd(PermissionLevel.EDIT)
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "--disallowedTools" not in cmd
    assert cmd[cmd.index("--allowedTools") + 1].split(",") == claude_allowed_tools()


def test_claude_full_is_unchanged() -> None:
    cmd = _claude_cmd(PermissionLevel.FULL)
    assert "--allowedTools" not in cmd and "--disallowedTools" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in cmd


# ---------------------------------------------------------------------------
# role prompts tell the role what it can do
# ---------------------------------------------------------------------------


def test_permission_notes_per_level() -> None:
    ro = permission_note(PermissionLevel.READONLY)
    ed = permission_note(PermissionLevel.EDIT)
    assert "cannot edit" in ro and "read-only commands" in ro
    assert "read and edit files" in ed and "read-only commands" in ed
    for note in (ro, ed):
        assert "git status/diff/log" in note and "denied" in note and "never rely on it" in note
    assert permission_note(PermissionLevel.FULL) == ""
    assert permission_note(None) == ""


def test_team_and_standalone_prompts_include_note_except_full() -> None:
    team_ro = resolve_team_system_prompt(RoleId.PM, None, PermissionLevel.READONLY)
    team_ed = resolve_team_system_prompt(RoleId.UI_DESIGNER, "Extra.", PermissionLevel.EDIT)
    team_full = resolve_team_system_prompt(RoleId.IMPLEMENTER, None, PermissionLevel.FULL)
    assert team_ro.endswith(permission_note(PermissionLevel.READONLY))
    assert team_ed.endswith(permission_note(PermissionLevel.EDIT))
    assert "Additional Instructions:\nExtra." in team_ed
    assert team_full == resolve_team_system_prompt(RoleId.IMPLEMENTER)  # unchanged
    assert "Your permissions:" not in team_full

    sa_ed = resolve_system_prompt(RoleId.IMPLEMENTER, None, PermissionLevel.EDIT)
    assert sa_ed.endswith(permission_note(PermissionLevel.EDIT))
    assert resolve_system_prompt(RoleId.IMPLEMENTER) == resolve_system_prompt(
        RoleId.IMPLEMENTER, None, PermissionLevel.FULL
    )


def test_session_manager_passes_session_permission_into_first_turn_prompt(
    db: Database,
    session_manager: SessionManager,
    workspace_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list = []
    original = AntigravityAdapter.build_command

    def _spy(self: AntigravityAdapter, **kwargs: object) -> list:
        cmd = original(self, **kwargs)  # type: ignore[arg-type]
        captured.append(cmd)
        return cmd

    monkeypatch.setattr(AntigravityAdapter, "build_command", _spy)

    async def _run() -> None:
        for perm in ("edit", "full"):
            sess = db.create_session(
                name=f"agy {perm}",
                provider="antigravity",
                model="gemini-3.8-flash-low",
                role="implementer",
                custom_prompt="",
                permission_level=perm,
                working_dir=str(workspace_dir),
            )
            turn = await session_manager.run_turn_and_wait(sess["id"], "do it")
            assert turn["status"] == TurnStatus.COMPLETED.value

    asyncio.run(_run())
    edit_cmd, full_cmd = captured
    assert "--settings" in edit_cmd
    assert "Your permissions: you can read and edit files" in edit_cmd[-1]
    assert "--settings" not in full_cmd and "--dangerously-skip-permissions" in full_cmd
    assert "Your permissions:" not in full_cmd[-1]


# ---------------------------------------------------------------------------
# adapters surface denied actions; standalone turns record them
# ---------------------------------------------------------------------------

REAL_AGY_DENIED_RESULT = {
    "event": "result",
    "result": {
        "conversation_id": "c1",
        "status": "SUCCESS",
        "response": "",
        "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
    },
}


def test_agy_adapter_parses_real_denied_result_shape() -> None:
    state = StreamParseState()
    events = AntigravityAdapter().parse_stream_line(json.dumps(REAL_AGY_DENIED_RESULT), state)
    assert state.final_status == "SUCCESS"
    assert state.final_response == ""
    assert state.denied_actions == ["RunCommand"]
    assert events[-1].denied_actions == ["RunCommand"]


def test_claude_adapter_parses_permission_denials() -> None:
    state = StreamParseState()
    line = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "s1",
        "result": "",
        "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "ls"}}],
    }
    ClaudeAdapter().parse_stream_line(json.dumps(line), state)
    assert state.denied_actions == ["Bash"]


@pytest.mark.parametrize(
    "provider,model,expected",
    [("antigravity", "gemini-3.8-flash-low", "RunCommand"), ("claude", "sonnet", "Bash")],
)
def test_standalone_turn_records_denied_action(
    db: Database,
    session_manager: SessionManager,
    workspace_dir: Path,
    provider: str,
    model: str,
    expected: str,
) -> None:
    async def _run() -> None:
        sess = db.create_session(
            name="Denied",
            provider=provider,
            model=model,
            role="implementer",
            custom_prompt="",
            permission_level="edit",
            working_dir=str(workspace_dir),
        )
        turn = await session_manager.run_turn_and_wait(sess["id"], "[DENIED] explore the repo")
        assert turn["status"] == TurnStatus.COMPLETED.value
        assert turn["response_text"] == ""
        assert turn["denied_actions"] == [expected]
        stored = db.get_turn(turn["id"])
        assert stored is not None and stored["denied_actions"] == [expected]
        # The normalized event stream carries it too (the chat UI reads both).
        events = session_manager.read_turn_events(turn["id"])
        results = [e for e in events if e.type.value == "turn_result"]
        assert results and results[-1].denied_actions == [expected]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# denial repair prompt
# ---------------------------------------------------------------------------


def test_denied_action_repair_prompt_names_action_and_asks_for_block() -> None:
    parsed = parse_team_message("", sender_role="ui_designer", stage_state="ui_design")
    prompt = build_denied_action_repair_prompt(
        ["RunCommand"], parsed, stage_state="ui_design", role="ui_designer"
    )
    assert prompt.startswith("# Action Denied")
    assert "`RunCommand`" in prompt
    assert "DENIED" in prompt and "file tools" in prompt and "read-only shell commands" in prompt
    assert "```baton-msg" in prompt and "ui_design" in prompt
    assert parsed.error in prompt
    assert not prompt.startswith("# Format Error")
    assert prompt != build_format_repair_prompt(parsed, stage_state="ui_design", role="ui_designer")
