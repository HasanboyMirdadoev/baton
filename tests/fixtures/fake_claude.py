#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

KNOWN_TRIGGERS: Tuple[str, ...] = (
    "[TEAM_WITH_MOCKUP]",
    "[TEAM_ASK_USER]",
    "[TEAM_ARCH_QUESTION_ANSWERED_BY_PM]",
    "[TEAM_ARCH_QUESTION_ESCALATED]",
    "[TEAM_UI_REJECT_ONCE]",
    "[TEAM_CODE_REJECT_ONCE]",
    "[TEAM_MALFORMED_ONCE]",
    "[TEAM_ALWAYS_REJECT]",
    "[ALWAYS_REJECT]",
    "[TEAM_MOCKUP_REJECT_ONCE]",
)


def _load_fake_state() -> Dict[str, Any]:
    state_file = os.path.join(".baton", ".fake_state.json")
    if os.path.isfile(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {
        "triggers": [],
        "ui_check_passes": 0,
        "review_passes": 0,
        "malformed_done": {},
        "mockup_rejected": False,
        "roles": {},
    }


def _save_fake_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(".baton", exist_ok=True)
        state_file = os.path.join(".baton", ".fake_state.json")
        # Atomic replace: parallel checkers share this file and must never read a torn write.
        tmp_file = f"{state_file}.{os.getpid()}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, state_file)
    except Exception:
        pass


def _is_team_mode(sys_prompt: str, prompt: str) -> bool:
    full = f"{sys_prompt}\n{prompt}"
    if "7-agent software engineering team in Baton" in full:
        return True
    if "[TEAM_" in full:
        return True
    team_headers = (
        "# Team Run Task",
        "# UI Mockup Task from PM",
        "# Architecture Task from PM",
        "# Planning Task",
        "# Implementation Task",
        "# UI & Runtime Verification Task",
        "# Code Review Task",
        "# Mockup Changes Requested",
        "# Verification & Review Feedback",
        "# All Verification Checks Approved",
        "# User Answers to Your Clarifying Questions",
        "# Question from ",
        "# Answer from PM",
    )
    for h in team_headers:
        if h in prompt:
            return True
    if "# Format Error" in prompt and os.path.isdir(".baton"):
        return True
    return False


def _detect_team_role(
    sys_prompt: str,
    prompt: str,
    session_id: Optional[str] = None,
) -> str:
    # 1. From session_id if already tagged
    if session_id:
        for r in (
            "ui_designer",
            "architect",
            "planner",
            "implementer",
            "ui_checker",
            "reviewer",
            "pm",
        ):
            if f"-{r}" in session_id:
                return r

    # 2. From system prompt
    sp = sys_prompt.lower()
    if "product manager (pm)" in sp or "role: pm" in sp:
        return "pm"
    # Planner prompt also mentions "Code Architect", so test it first.
    if "lead engineering planner" in sp:
        return "planner"
    if "ui designer" in sp:
        return "ui_designer"
    if "code architect" in sp:
        return "architect"
    if "you are the implementer in a 7-agent" in sp or "role: implementer" in sp:
        return "implementer"
    if "ui checker" in sp:
        return "ui_checker"
    if "you are the code reviewer in a 7-agent" in sp or "role: reviewer" in sp:
        return "reviewer"

    # 3. From prompt headers
    p = prompt
    if "# Team Run Task" in p:
        return "pm"
    if "# User Answers to Your Clarifying Questions" in p:
        return "pm"
    if "# All Verification Checks Approved" in p:
        return "pm"
    if "# Question from " in p or p.startswith("# Question from "):
        return "pm"
    if "# UI Mockup Task from PM" in p or "# Mockup Changes Requested" in p:
        return "ui_designer"
    if "# Architecture Task from PM" in p:
        return "architect"
    if "# Planning Task" in p:
        return "planner"
    if "# Implementation Task" in p or "# Verification & Review Feedback" in p:
        return "implementer"
    if "# UI & Runtime Verification Task" in p:
        return "ui_checker"
    if "# Code Review Task" in p:
        return "reviewer"
    if "# Answer from PM" in p:
        if "'architect'" in p:
            return "architect"
        if "'ui_designer'" in p:
            return "ui_designer"
        if "'planner'" in p:
            return "planner"
        if "'implementer'" in p:
            return "implementer"

    # 4. From format error repair prompt
    if "# Format Error" in p:
        if "'pm'" in p or "state 'pm_" in p:
            return "pm"
        if "'ui_designer'" in p or "state 'ui_design'" in p:
            return "ui_designer"
        if "'architect'" in p or "state 'architecture'" in p:
            return "architect"
        if "'planner'" in p or "state 'planning'" in p:
            return "planner"
        if "'implementer'" in p or "state 'implementing'" in p:
            return "implementer"
        if "'ui_checker'" in p:
            return "ui_checker"
        if "'reviewer'" in p:
            return "reviewer"

    # 5. Check saved role in .fake_state.json
    state = _load_fake_state()
    if session_id and session_id in state.get("roles", {}):
        return state["roles"][session_id]

    return "pm"


def _generate_team_response(
    role: str,
    sys_prompt: str,
    prompt: str,
    state: Dict[str, Any],
) -> str:
    triggers: List[str] = state.get("triggers", [])
    full_text = f"{sys_prompt}\n{prompt}"
    for t in KNOWN_TRIGGERS:
        if t in full_text and t not in triggers:
            triggers.append(t)
    state["triggers"] = triggers

    # Check [TEAM_MALFORMED_ONCE]
    is_malformed_trigger = "[TEAM_MALFORMED_ONCE]" in triggers
    is_repair_turn = (
        "# Format Error" in prompt
        or "Missing or Invalid `baton-msg` Block" in prompt
    )
    malformed_done = state.get("malformed_done", {}).get(role, False)

    if is_malformed_trigger and not malformed_done and not is_repair_turn:
        if "malformed_done" not in state:
            state["malformed_done"] = {}
        state["malformed_done"][role] = True
        _save_fake_state(state)
        # Emit plain text without a baton-msg block to trigger 1-retry repair
        return (
            f"# {role.title()} In Progress\n\n"
            "I have reviewed the task requirements and am working on deliverables."
        )

    if role == "pm":
        # 1. Final Summary: # All Verification Checks Approved
        if (
            "# All Verification Checks Approved" in prompt
            or "Prepare Final Sign-off Summary" in prompt
        ):
            _save_fake_state(state)
            return (
                "# Final Sign-off Report\n\n"
                "All implementation and verification steps have completed successfully.\n"
                "UI Checker and Code Reviewer have both approved the changes.\n\n"
                "```baton-msg\n"
                "{\n"
                '  "kind": "done",\n'
                '  "to": "user",\n'
                '  "summary": "All verification checks approved. Ready for sign-off."\n'
                "}\n"
                "```"
            )

        # 2. Specialist question relay: # Question from ...
        if "# Question from " in prompt:
            asking_role = "architect"
            for candidate in ("architect", "ui_designer", "planner", "implementer"):
                if candidate in prompt.lower():
                    asking_role = candidate
                    break

            if "[TEAM_ARCH_QUESTION_ESCALATED]" in triggers:
                _save_fake_state(state)
                return (
                    f"# Clarification Question Escalated to User\n\n"
                    f"The {asking_role} has a technical question requiring your input.\n\n"
                    "```baton-msg\n"
                    "{\n"
                    '  "kind": "question",\n'
                    '  "to": "user",\n'
                    '  "summary": "Specialist question escalated to user",\n'
                    '  "questions": [\n'
                    '    "Should the endpoint return 200 or 201?"\n'
                    '  ]\n'
                    "}\n"
                    "```"
                )
            else:
                _save_fake_state(state)
                return (
                    f"# Answer from PM\n\n"
                    "Proceed with HTTP 200 and standard JSON envelope.\n\n"
                    "```baton-msg\n"
                    "{\n"
                    '  "kind": "answer",\n'
                    f'  "to": "{asking_role}",\n'
                    '  "summary": "Return 200 with result payload"\n'
                    "}\n"
                    "```"
                )

        # 3. User answers returned: # User Answers to Your Clarifying Questions
        if "# User Answers to Your Clarifying Questions" in prompt:
            # Check if this was an escalated specialist question
            # The triage variant of this prompt also mentions 'architect', so only the
            # explicit relay instruction identifies an escalated specialist question.
            if "Relay the resolved answer to" in prompt:
                _save_fake_state(state)
                return (
                    "# Answer from PM\n\n"
                    "The user confirmed: return 200.\n\n"
                    "```baton-msg\n"
                    "{\n"
                    '  "kind": "answer",\n'
                    '  "to": "architect",\n'
                    '  "summary": "User confirmed: return 200"\n'
                    "}\n"
                    "```"
                )
            # Otherwise, user answered PM triage questions
            if "[TEAM_WITH_MOCKUP]" in triggers:
                _save_fake_state(state)
                return (
                    "# Product Requirements Specification\n\n"
                    "User requirements clarified. Proceeding to UI mockup.\n\n"
                    "```baton-msg\n"
                    "{\n"
                    '  "kind": "deliverable",\n'
                    '  "to": "ui_designer",\n'
                    '  "needs_ui_mockup": true,\n'
                    '  "summary": "Requirements defined with user answers, proceeding to UI mockup"\n'
                    "}\n"
                    "```"
                )
            else:
                _save_fake_state(state)
                return (
                    "# Product Requirements Specification\n\n"
                    "User requirements clarified. Proceeding to architecture.\n\n"
                    "```baton-msg\n"
                    "{\n"
                    '  "kind": "deliverable",\n'
                    '  "to": "architect",\n'
                    '  "needs_ui_mockup": false,\n'
                    '  "summary": "Requirements defined with user answers, proceeding to architecture"\n'
                    "}\n"
                    "```"
                )

        # 4. Mockup Review (when prompt mentions mockup.html / mockup review, or after mockup created)
        is_mockup_review = (
            "# Mockup Review" in prompt
            or "mockup.html" in prompt
            or "mockup_path" in prompt
            or (
                "# Team Run Task" not in prompt
                and "[TEAM_WITH_MOCKUP]" in triggers
                and os.path.exists(".baton/mockup.html")
            )
        )
        if is_mockup_review:
            if "[TEAM_MOCKUP_REJECT_ONCE]" in triggers and not state.get("mockup_rejected"):
                state["mockup_rejected"] = True
                _save_fake_state(state)
                return (
                    "# UI Mockup Review Feedback\n\n"
                    "Mockup requires adjustments.\n\n"
                    "```baton-msg\n"
                    "{\n"
                    '  "kind": "reject",\n'
                    '  "to": "ui_designer",\n'
                    '  "summary": "Mockup rejected with findings",\n'
                    '  "findings": [\n'
                    '    "Fix mockup header alignment and color contrast"\n'
                    '  ]\n'
                    "}\n"
                    "```"
                )
            else:
                _save_fake_state(state)
                return (
                    "# UI Mockup Approval Request\n\n"
                    "The UI Designer has produced an interactive HTML mockup at `.baton/mockup.html`.\n\n"
                    "```baton-msg\n"
                    "{\n"
                    '  "kind": "approval_request",\n'
                    '  "to": "user",\n'
                    '  "mockup_path": ".baton/mockup.html",\n'
                    '  "summary": "Interactive HTML mockup ready for user approval"\n'
                    "}\n"
                    "```"
                )

        # 5. Triage (initial turn)
        if "[TEAM_ASK_USER]" in triggers:
            _save_fake_state(state)
            return (
                "# Product Requirements Clarification\n\n"
                "Clarifying questions for the user before proceeding.\n\n"
                "```baton-msg\n"
                "{\n"
                '  "kind": "question",\n'
                '  "to": "user",\n'
                '  "summary": "Clarifying questions for user requirements",\n'
                '  "questions": [\n'
                '    "Which storage key should we use?"\n'
                '  ]\n'
                "}\n"
                "```"
            )
        elif "[TEAM_WITH_MOCKUP]" in triggers:
            _save_fake_state(state)
            return (
                "# Product Requirements Specification\n\n"
                "Task requires visual UI changes. Routing to UI Designer.\n\n"
                "```baton-msg\n"
                "{\n"
                '  "kind": "deliverable",\n'
                '  "to": "ui_designer",\n'
                '  "needs_ui_mockup": true,\n'
                '  "summary": "Requirements defined; UI mockup required"\n'
                "}\n"
                "```"
            )
        else:
            _save_fake_state(state)
            return (
                "# Product Requirements Specification\n\n"
                "Task is pure backend/logic. No UI mockup needed.\n\n"
                "```baton-msg\n"
                "{\n"
                '  "kind": "deliverable",\n'
                '  "to": "architect",\n'
                '  "needs_ui_mockup": false,\n'
                '  "summary": "Requirements defined, proceeding to architecture"\n'
                "}\n"
                "```"
            )

    elif role == "ui_designer":
        # Always write .baton/mockup.html
        try:
            os.makedirs(".baton", exist_ok=True)
            mockup_html = (
                "<!DOCTYPE html>\n"
                "<html>\n"
                "<head><title>Mockup</title></head>\n"
                "<body><h1>Mockup</h1><p>Interactive preview mockup.</p></body>\n"
                "</html>\n"
            )
            with open(".baton/mockup.html", "w", encoding="utf-8") as f:
                f.write(mockup_html)
        except Exception:
            pass

        _save_fake_state(state)
        return (
            "# UI Mockup Deliverable\n\n"
            "Created interactive HTML mockup at `.baton/mockup.html`.\n\n"
            "```baton-msg\n"
            "{\n"
            '  "kind": "deliverable",\n'
            '  "to": "pm",\n'
            '  "mockup_path": ".baton/mockup.html",\n'
            '  "summary": "Created interactive HTML mockup at .baton/mockup.html"\n'
            "}\n"
            "```"
        )

    elif role == "architect":
        is_q_trigger = (
            "[TEAM_ARCH_QUESTION_ANSWERED_BY_PM]" in triggers
            or "[TEAM_ARCH_QUESTION_ESCALATED]" in triggers
        )
        is_resumed_answer = "# Answer from PM" in prompt

        if is_q_trigger and not is_resumed_answer:
            _save_fake_state(state)
            return (
                "# Architecture Clarification Question\n\n"
                "Need guidance from PM on endpoint contract.\n\n"
                "```baton-msg\n"
                "{\n"
                '  "kind": "question",\n'
                '  "to": "pm",\n'
                '  "summary": "Clarification needed for architecture",\n'
                '  "questions": [\n'
                '    "Should the endpoint return 200 or 201?"\n'
                '  ]\n'
                "}\n"
                "```"
            )

        _save_fake_state(state)
        return (
            "# Technical Architecture Specification\n\n"
            "## Component Architecture\n"
            "System components and data models designed.\n\n"
            "## Runtime Contract\n"
            "Application start and test contracts established.\n\n"
            "```baton-msg\n"
            "{\n"
            '  "kind": "deliverable",\n'
            '  "to": "planner",\n'
            '  "summary": "Completed architecture specification and runtime execution contract",\n'
            '  "app_run": {\n'
            '    "requires_ui_check": true,\n'
            '    "start_cmd": "python3 -m uvicorn backend.main:app --host 127.0.0.1 --port {port}",\n'
            '    "app_url": "http://127.0.0.1:{port}",\n'
            '    "healthcheck_path": "/",\n'
            '    "stop_signal": "SIGTERM"\n'
            '  }\n'
            "}\n"
            "```"
        )

    elif role == "planner":
        _save_fake_state(state)
        return (
            "# Implementation Plan\n\n"
            "## Ordered Tasks\n"
            "1. Implement changes in workspace.\n"
            "2. Verify with unit tests.\n\n"
            "```baton-msg\n"
            "{\n"
            '  "kind": "deliverable",\n'
            '  "to": "implementer",\n'
            '  "summary": "Completed file-by-file implementation plan and verification checklist",\n'
            '  "app_run": {\n'
            '    "requires_ui_check": true,\n'
            '    "start_cmd": "python3 -m uvicorn backend.main:app --host 127.0.0.1 --port {port}",\n'
            '    "app_url": "http://127.0.0.1:{port}",\n'
            '    "healthcheck_path": "/",\n'
            '    "stop_signal": "SIGTERM"\n'
            '  }\n'
            "}\n"
            "```"
        )

    elif role == "implementer":
        # Always write feature_output.txt
        try:
            with open("feature_output.txt", "w", encoding="utf-8") as f:
                f.write("feature implementation complete\n")
        except Exception:
            pass

        _save_fake_state(state)
        return (
            "# Implementation Report\n\n"
            "Implemented feature and created feature_output.txt.\n\n"
            "```baton-msg\n"
            "{\n"
            '  "kind": "deliverable",\n'
            '  "to": "checkers",\n'
            '  "summary": "Implemented feature and verified file changes",\n'
            '  "files_changed": [\n'
            '    "feature_output.txt: Created output file"\n'
            '  ]\n'
            "}\n"
            "```"
        )

    elif role == "ui_checker":
        # Write .baton/ui_check.py
        try:
            os.makedirs(".baton", exist_ok=True)
            with open(".baton/ui_check.py", "w", encoding="utf-8") as f:
                f.write("# UI check verification script\n")
        except Exception:
            pass

        # Prefer the pass number in the prompt: the state file is shared with the parallel
        # checker, so a counter can be lost to an interleaved read-modify-write.
        is_pass_1 = ("Pass #1" in prompt) or (
            "Pass #" not in prompt and state.get("ui_check_passes", 0) == 0
        )
        if "[TEAM_UI_REJECT_ONCE]" in triggers and is_pass_1:
            state["ui_check_passes"] = state.get("ui_check_passes", 0) + 1
            _save_fake_state(state)
            return (
                "# UI Verification Findings\n\n"
                "UI verification failed with findings on Pass #1.\n\n"
                "```baton-msg\n"
                "{\n"
                '  "kind": "reject",\n'
                '  "to": "implementer",\n'
                '  "summary": "UI verification failed with 1 finding",\n'
                '  "findings": [\n'
                '    "- Fix issue"\n'
                '  ]\n'
                "}\n"
                "```"
            )

        state["ui_check_passes"] = state.get("ui_check_passes", 0) + 1
        _save_fake_state(state)
        return (
            "# UI Verification Report\n\n"
            "Application started cleanly and passed all headless browser UI checks.\n\n"
            "```baton-msg\n"
            "{\n"
            '  "kind": "approve",\n'
            '  "to": "pm",\n'
            '  "summary": "Application started cleanly and passed all headless browser UI checks"\n'
            "}\n"
            "```"
        )

    elif role == "reviewer":
        if "[TEAM_ALWAYS_REJECT]" in triggers or "[ALWAYS_REJECT]" in triggers:
            _save_fake_state(state)
            return (
                "# Code Review Report\n\n"
                "Review found persistent blocking findings.\n\n"
                "```baton-msg\n"
                "{\n"
                '  "kind": "reject",\n'
                '  "to": "implementer",\n'
                '  "summary": "Code review requested changes [ALWAYS_REJECT]",\n'
                '  "findings": [\n'
                '    "- Still broken [ALWAYS_REJECT]"\n'
                '  ]\n'
                "}\n"
                "```"
            )

        # Prefer the pass number in the prompt: the state file is shared with the parallel
        # checker, so a counter can be lost to an interleaved read-modify-write.
        is_pass_1 = ("Pass #1" in prompt) or (
            "Pass #" not in prompt and state.get("review_passes", 0) == 0
        )
        if "[TEAM_CODE_REJECT_ONCE]" in triggers and is_pass_1:
            state["review_passes"] = state.get("review_passes", 0) + 1
            _save_fake_state(state)
            return (
                "# Code Review Findings\n\n"
                "Requested changes for code review findings on Pass #1.\n\n"
                "```baton-msg\n"
                "{\n"
                '  "kind": "reject",\n'
                '  "to": "implementer",\n'
                '  "summary": "Requested changes for 1 code review finding",\n'
                '  "findings": [\n'
                '    "- Fix issue"\n'
                '  ]\n'
                "}\n"
                "```"
            )

        state["review_passes"] = state.get("review_passes", 0) + 1
        _save_fake_state(state)
        return (
            "# Code Review Report\n\n"
            "Code review passed with no findings.\n\n"
            "```baton-msg\n"
            "{\n"
            '  "kind": "approve",\n'
            '  "to": "pm",\n'
            '  "summary": "Code review passed with no findings"\n'
            "}\n"
            "```"
        )

    # Fallback
    _save_fake_state(state)
    return (
        f"# {role.title()} Deliverable\n\n"
        "Turn complete.\n\n"
        "```baton-msg\n"
        "{\n"
        '  "kind": "deliverable",\n'
        '  "to": "pm",\n'
        f'  "summary": "{role} turn completed"\n'
        "}\n"
        "```"
    )


def _parse_args(argv: List[str]) -> Tuple[Optional[str], str, str, str, str]:
    resume_id: Optional[str] = None
    model = "sonnet"
    perm_mode = "default"
    sys_prompt = ""
    prompt = ""

    if "--" in argv:
        sep_idx = argv.index("--")
        flag_args = argv[:sep_idx]
        prompt_args = argv[sep_idx + 1 :]
        prompt = " ".join(prompt_args)
    else:
        flag_args = argv
        prompt = argv[-1] if argv else ""

    i = 0
    while i < len(flag_args):
        arg = flag_args[i]
        if arg == "--resume" and i + 1 < len(flag_args):
            resume_id = flag_args[i + 1]
            i += 2
        elif arg == "--model" and i + 1 < len(flag_args):
            model = flag_args[i + 1]
            i += 2
        elif arg == "--permission-mode" and i + 1 < len(flag_args):
            perm_mode = flag_args[i + 1]
            i += 2
        elif arg == "--append-system-prompt" and i + 1 < len(flag_args):
            sys_prompt = flag_args[i + 1]
            i += 2
        else:
            i += 1

    return resume_id, model, perm_mode, sys_prompt, prompt


def main() -> int:
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    resume_id, model, perm_mode, sys_prompt, prompt = _parse_args(sys.argv[1:])
    has_sys_prompt = bool(sys_prompt.strip())
    is_team = _is_team_mode(sys_prompt, prompt)
    team_role = _detect_team_role(sys_prompt, prompt, resume_id) if is_team else None
    session_id = resume_id or (
        f"fake-claude-{team_role}" if is_team and team_role else "fake-claude-uuid-1"
    )
    is_resumed = resume_id is not None

    # 1. Emit system init event
    print(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_id,
                "cwd": os.getcwd(),
            }
        ),
        flush=True,
    )

    # Handle [SLOW] trigger
    if "[SLOW]" in prompt:
        time.sleep(10)

    # Handle [FAIL] trigger (matches real claude-turn1.jsonl sequence)
    if "[FAIL]" in prompt:
        sys.stderr.write("Fatal CLI error occurred\n")
        sys.stderr.flush()
        print(
            json.dumps(
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "error": "authentication_failed",
                    "is_api_error_message": True,
                    "message": {
                        "model": "<synthetic>",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "Fatal CLI error occurred",
                            }
                        ],
                    },
                }
            ),
            flush=True,
        )
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "session_id": session_id,
                    "result": "Fatal CLI error occurred",
                }
            ),
            flush=True,
        )
        return 1

    # 2. Emit thinking delta
    print(
        json.dumps(
            {
                "type": "stream_event",
                "session_id": session_id,
                "event": {
                    "type": "content_block_delta",
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "Analyzing workspace and prompt...",
                    },
                },
            }
        ),
        flush=True,
    )

    # 3. Emit tool_use and tool_result
    print(
        json.dumps(
            {
                "type": "assistant",
                "session_id": session_id,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_fake_1",
                            "name": "Read",
                            "input": {"file_path": "README.md"},
                        }
                    ]
                },
            }
        ),
        flush=True,
    )
    tool_output_content = (
        "X" * 100_000 if "[LARGE_LINE]" in prompt else "File read successfully"
    )
    print(
        json.dumps(
            {
                "type": "user",
                "session_id": session_id,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_fake_1",
                            "content": tool_output_content,
                            "is_error": False,
                        }
                    ]
                },
            }
        ),
        flush=True,
    )

    # 4. Determine response text based on prompt triggers
    if is_team:
        state = _load_fake_state()
        if "roles" not in state:
            state["roles"] = {}
        if team_role:
            state["roles"][session_id] = team_role
        response_text = _generate_team_response(
            team_role or "pm", sys_prompt, prompt, state
        )
    else:
        summary_header = (
            f"[claude model={model} mode={perm_mode} resumed={is_resumed} sysprompt={has_sys_prompt} conv={session_id}]"
        )
        if "[MISSING_VERDICT]" in prompt:
            response_text = f"Looks okay to me {summary_header} [MISSING_VERDICT]"
        elif "[ALWAYS_REJECT]" in prompt:
            response_text = (
                f"{summary_header} Reviewing [ALWAYS_REJECT]\n"
                "VERDICT: CHANGES_REQUESTED\n"
                "FINDINGS:\n"
                "- Still broken [ALWAYS_REJECT]"
            )
        elif (
            "[CHANGES_REQUESTED]" in prompt
            and not is_resumed
            and "# Re-Review Request" not in prompt
        ):
            response_text = (
                f"{summary_header} Initial review for [CHANGES_REQUESTED]\n"
                "VERDICT: CHANGES_REQUESTED\n"
                "FINDINGS:\n"
                "- [foo.py:10] Fix bug in foo.py"
            )
        elif "# Re-Review Request" in prompt:
            response_text = (
                f"{summary_header} Re-review complete, fixes verified.\n"
                "VERDICT: APPROVED"
            )
        else:
            response_text = (
                f"{summary_header} Processed prompt: {prompt[:120]}\n"
                "VERDICT: APPROVED"
            )

    # 5. Emit text delta and final result
    print(
        json.dumps(
            {
                "type": "stream_event",
                "session_id": session_id,
                "event": {
                    "type": "content_block_delta",
                    "delta": {
                        "type": "text_delta",
                        "text": response_text,
                    },
                },
            }
        ),
        flush=True,
    )
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session_id,
                "result": response_text,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
