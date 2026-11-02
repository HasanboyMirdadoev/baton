from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from backend.models import AppRunContract, RoleId, TeamMessageKind, TeamStageState
from backend.roles import get_role_label

MAX_EXCERPT_CHARS: int = 16000
MIN_MOCKUP_BYTES: int = 32
REVIEW_DIFF_FILENAME: str = "review.diff"
REVIEW_DIFF_FILENAME_REL: str = ".baton/review.diff"

_BATON_MSG_FENCE_RE = re.compile(
    r"```baton-msg[ \t]*\r?\n([\s\S]*?)```",
    re.IGNORECASE,
)
_FALLBACK_JSON_FENCE_RE = re.compile(
    r"```(?:json)?[ \t]*\r?\n(\{[\s\S]*?\"kind\"[\s\S]*?\})\r?\n```[ \t]*$",
    re.IGNORECASE,
)

ALLOWED_ROLE_TRANSITIONS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "pm": {
        "question": ("user",),
        "answer": ("ui_designer", "architect", "planner", "implementer"),
        "deliverable": ("ui_designer", "architect"),
        "approval_request": ("user",),
        "approve": ("pm", "architect"),
        "reject": ("ui_designer",),
        "done": ("user",),
    },
    "ui_designer": {
        "question": ("pm",),
        "deliverable": ("pm",),
    },
    "architect": {
        "question": ("pm",),
        "deliverable": ("planner",),
    },
    "planner": {
        "question": ("pm",),
        "deliverable": ("implementer",),
    },
    "implementer": {
        "question": ("pm",),
        "deliverable": ("checkers",),
    },
    "ui_checker": {
        "approve": ("pm",),
        "reject": ("implementer",),
    },
    "reviewer": {
        "approve": ("pm",),
        "reject": ("implementer",),
    },
}

ALLOWED_STAGE_TRANSITIONS: Dict[str, Tuple[Tuple[str, str, str], ...]] = {
    "pm_triage": (
        ("pm", "question", "user"),
        ("pm", "deliverable", "ui_designer"),
        ("pm", "deliverable", "architect"),
    ),
    "ui_design": (
        ("ui_designer", "question", "pm"),
        ("ui_designer", "deliverable", "pm"),
    ),
    "pm_mockup_review": (
        ("pm", "reject", "ui_designer"),
        ("pm", "approval_request", "user"),
        ("pm", "approve", "architect"),
        ("pm", "approve", "pm"),
    ),
    "architecture": (
        ("architect", "question", "pm"),
        ("architect", "deliverable", "planner"),
    ),
    "planning": (
        ("planner", "question", "pm"),
        ("planner", "deliverable", "implementer"),
    ),
    "implementing": (
        ("implementer", "question", "pm"),
        ("implementer", "deliverable", "checkers"),
    ),
    "pm_answer_relay": (
        ("pm", "answer", "ui_designer"),
        ("pm", "answer", "architect"),
        ("pm", "answer", "planner"),
        ("pm", "answer", "implementer"),
        ("pm", "question", "user"),
    ),
    "checking": (
        ("ui_checker", "approve", "pm"),
        ("ui_checker", "reject", "implementer"),
        ("reviewer", "approve", "pm"),
        ("reviewer", "reject", "implementer"),
    ),
    "pm_final_summary": (
        ("pm", "done", "user"),
    ),
}


@dataclass
class ParsedTeamMessage:
    """Result of parsing and validating an agent's trailing baton-msg block."""

    valid: bool
    kind: str = ""
    to: str = ""
    summary: str = ""
    body_markdown: str = ""
    needs_ui_mockup: Optional[bool] = None
    mockup_path: Optional[str] = None
    questions: List[str] = field(default_factory=list)
    answers: List[str] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    app_run: Optional[Dict[str, Any]] = None
    raw_dict: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def _normalize_enum_str(value: Optional[Union[str, RoleId, TeamStageState]]) -> str:
    """Normalizes an enum or string to lowercase stripped text."""
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(getattr(value, "value")).strip().lower()
    return str(value).strip().lower()


def validate_app_run_contract(raw_app_run: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validates an app_run dictionary against AppRunContract and returns (normalized_dict, error)."""
    if not isinstance(raw_app_run, dict):
        return None, "Field 'app_run' must be a JSON object."
    try:
        contract = AppRunContract(
            requires_ui_check=bool(raw_app_run.get("requires_ui_check", True)),
            start_cmd=str(raw_app_run.get("start_cmd") or "").strip(),
            app_url=str(raw_app_run.get("app_url") or "").strip(),
            healthcheck_path=str(raw_app_run.get("healthcheck_path") or "/").strip() or "/",
            stop_signal=str(raw_app_run.get("stop_signal") or "SIGTERM").strip() or "SIGTERM",
        )
        return contract.model_dump(), None
    except Exception as exc:
        return None, f"Invalid 'app_run' contract: {exc}"


def _extract_non_empty_str_list(val: Any) -> Optional[List[str]]:
    """Extracts a list of non-empty strings, or returns None if invalid."""
    if not isinstance(val, list):
        return None
    cleaned: List[str] = []
    for item in val:
        if not isinstance(item, str) or not item.strip():
            return None
        cleaned.append(item.strip())
    return cleaned if cleaned else None


def parse_team_message(
    reply_text: str,
    sender_role: Optional[Union[RoleId, str]] = None,
    stage_state: Optional[Union[TeamStageState, str]] = None,
    *,
    asking_role: Optional[Union[RoleId, str]] = None,
) -> ParsedTeamMessage:
    """Extracts, parses, and validates the trailing baton-msg JSON block from an agent reply."""
    text = reply_text or ""
    matches = list(_BATON_MSG_FENCE_RE.finditer(text))
    match = matches[-1] if matches else _FALLBACK_JSON_FENCE_RE.search(text)

    if match is None:
        return ParsedTeamMessage(
            valid=False,
            body_markdown=text.strip(),
            error="Missing fenced ```baton-msg JSON block at the end of the response.",
        )

    body_markdown = text[: match.start()].strip()
    raw_json = match.group(1).strip()

    try:
        obj = json.loads(raw_json)
    except Exception as exc:
        return ParsedTeamMessage(
            valid=False,
            body_markdown=body_markdown,
            error=f"Malformed JSON in baton-msg block: {exc}",
        )

    if not isinstance(obj, dict):
        return ParsedTeamMessage(
            valid=False,
            body_markdown=body_markdown,
            error="baton-msg JSON payload must be a JSON object.",
        )

    kind = str(obj.get("kind") or "").strip().lower()
    to_target = str(obj.get("to") or "").strip().lower()
    summary = str(obj.get("summary") or "").strip()

    raw_needs_mockup = obj.get("needs_ui_mockup")
    needs_ui_mockup: Optional[bool] = (
        raw_needs_mockup if isinstance(raw_needs_mockup, bool) else None
    )

    raw_mockup_path = obj.get("mockup_path")
    mockup_path: Optional[str] = (
        str(raw_mockup_path).strip()
        if isinstance(raw_mockup_path, str) and raw_mockup_path.strip()
        else None
    )
    if kind == "approval_request" and not mockup_path:
        mockup_path = ".baton/mockup.html"

    questions = [
        str(q).strip()
        for q in (obj.get("questions") if isinstance(obj.get("questions"), list) else [])
        if str(q).strip()
    ]
    answers = [
        str(a).strip()
        for a in (obj.get("answers") if isinstance(obj.get("answers"), list) else [])
        if str(a).strip()
    ]
    findings = [
        str(f).strip()
        for f in (obj.get("findings") if isinstance(obj.get("findings"), list) else [])
        if str(f).strip()
    ]
    files_changed = [
        str(fc).strip()
        for fc in (
            obj.get("files_changed")
            if isinstance(obj.get("files_changed"), list)
            else []
        )
        if str(fc).strip()
    ]

    parsed = ParsedTeamMessage(
        valid=False,
        kind=kind,
        to=to_target,
        summary=summary,
        body_markdown=body_markdown,
        needs_ui_mockup=needs_ui_mockup,
        mockup_path=mockup_path,
        questions=questions,
        answers=answers,
        findings=findings,
        files_changed=files_changed,
        app_run=obj.get("app_run") if isinstance(obj.get("app_run"), dict) else None,
        raw_dict=obj,
    )

    valid_kinds = {k.value for k in TeamMessageKind if k != TeamMessageKind.USER_ACTION}
    if not kind:
        parsed.error = "Missing required field 'kind' in baton-msg block."
        return parsed
    if kind not in valid_kinds:
        parsed.error = f"Unsupported kind '{kind}'. Must be one of: {', '.join(sorted(valid_kinds))}."
        return parsed
    if not to_target:
        parsed.error = "Missing required field 'to' in baton-msg block."
        return parsed
    if not summary:
        parsed.error = "Missing or empty required field 'summary' in baton-msg block."
        return parsed

    role_norm = _normalize_enum_str(sender_role)
    stage_norm = _normalize_enum_str(stage_state)
    asking_norm = _normalize_enum_str(asking_role)

    # Validate kind-specific required keys and constraints
    if kind == "question":
        valid_q = _extract_non_empty_str_list(obj.get("questions"))
        if not valid_q:
            parsed.error = "Kind 'question' requires a non-empty list of non-empty strings in 'questions'."
            return parsed
        parsed.questions = valid_q
        if role_norm:
            if role_norm == "pm" and to_target != "user":
                parsed.error = "PM questions must set 'to': 'user'."
                return parsed
            if role_norm != "pm" and to_target != "pm":
                parsed.error = f"Role '{role_norm}' questions must set 'to': 'pm' (got '{to_target}')."
                return parsed

    elif kind == "answer":
        if to_target not in ("ui_designer", "architect", "planner", "implementer"):
            parsed.error = (
                f"Kind 'answer' must target one of ui_designer, architect, planner, implementer "
                f"(got '{to_target}')."
            )
            return parsed
        if asking_norm and to_target != asking_norm:
            parsed.error = f"Kind 'answer' target '{to_target}' does not match asking_role '{asking_norm}'."
            return parsed

    elif kind == "deliverable":
        if role_norm == "pm" or (not role_norm and "needs_ui_mockup" in obj):
            if not isinstance(raw_needs_mockup, bool):
                parsed.error = "PM deliverable requires boolean field 'needs_ui_mockup' (true or false)."
                return parsed
            expected_to = "ui_designer" if raw_needs_mockup else "architect"
            if to_target != expected_to:
                parsed.error = (
                    f"PM deliverable with needs_ui_mockup={raw_needs_mockup} must set "
                    f"'to': '{expected_to}' (got '{to_target}')."
                )
                return parsed

        if role_norm in ("architect", "planner") or (
            not role_norm and to_target in ("planner", "implementer") and "app_run" in obj
        ):
            app_run_dict, app_err = validate_app_run_contract(obj.get("app_run"))
            if app_err or app_run_dict is None:
                parsed.error = (
                    app_err
                    or f"Role '{role_norm or 'architect/planner'}' deliverable requires a valid 'app_run' object."
                )
                return parsed
            parsed.app_run = app_run_dict
        elif "app_run" in obj and obj.get("app_run") is not None:
            app_run_dict, app_err = validate_app_run_contract(obj.get("app_run"))
            if app_err:
                parsed.error = app_err
                return parsed
            parsed.app_run = app_run_dict

    elif kind == "approval_request":
        if to_target != "user":
            parsed.error = f"Kind 'approval_request' must set 'to': 'user' (got '{to_target}')."
            return parsed

    elif kind == "approve":
        if to_target not in ("pm", "architect"):
            parsed.error = f"Kind 'approve' must set 'to': 'pm' (or 'architect') (got '{to_target}')."
            return parsed

    elif kind == "reject":
        valid_f = _extract_non_empty_str_list(obj.get("findings"))
        if not valid_f:
            parsed.error = "Kind 'reject' requires a non-empty list of non-empty strings in 'findings'."
            return parsed
        parsed.findings = valid_f
        if role_norm == "pm" and to_target != "ui_designer":
            parsed.error = f"PM 'reject' must set 'to': 'ui_designer' (got '{to_target}')."
            return parsed
        if role_norm in ("ui_checker", "reviewer") and to_target != "implementer":
            parsed.error = f"Checker '{role_norm}' 'reject' must set 'to': 'implementer' (got '{to_target}')."
            return parsed

    elif kind == "done":
        if to_target != "user":
            parsed.error = f"Kind 'done' must set 'to': 'user' (got '{to_target}')."
            return parsed

    # Validate sender_role permissions if sender_role was provided
    if role_norm:
        role_allowed = ALLOWED_ROLE_TRANSITIONS.get(role_norm)
        if role_allowed is None:
            parsed.error = f"Unknown team role '{role_norm}'."
            return parsed
        if kind not in role_allowed:
            allowed_kinds = ", ".join(role_allowed.keys())
            parsed.error = f"Role '{role_norm}' cannot emit kind '{kind}' (allowed: {allowed_kinds})."
            return parsed
        if to_target not in role_allowed[kind]:
            allowed_targets = ", ".join(role_allowed[kind])
            parsed.error = (
                f"Role '{role_norm}' with kind '{kind}' cannot target '{to_target}' "
                f"(allowed: {allowed_targets})."
            )
            return parsed

    # Validate stage_state transition if stage_state was provided
    if stage_norm:
        stage_allowed = ALLOWED_STAGE_TRANSITIONS.get(stage_norm)
        if stage_allowed is not None:
            matched = any(
                (not role_norm or r == role_norm) and k == kind and t == to_target
                for (r, k, t) in stage_allowed
            )
            if not matched:
                parsed.error = (
                    f"Transition ({role_norm or '*'}, kind='{kind}', to='{to_target}') "
                    f"is not allowed in stage '{stage_norm}'."
                )
                return parsed

    parsed.valid = True
    return parsed


def _cap_text(
    text: Optional[str],
    max_chars: int = MAX_EXCERPT_CHARS,
    reference_file: Optional[str] = None,
) -> str:
    """Caps text at max_chars with a clear truncation marker pointing to the full file if provided."""
    raw = (text or "").strip()
    if len(raw) <= max_chars:
        return raw
    truncated = raw[:max_chars]
    if reference_file:
        return f"{truncated}\n\n... [truncated at {max_chars} chars — see full file in {reference_file}]"
    return f"{truncated}\n\n... [truncated at {max_chars} chars]"


def cap_text(
    text: Optional[str],
    max_chars: int = MAX_EXCERPT_CHARS,
    reference_file: Optional[str] = None,
) -> str:
    """Public alias for _cap_text."""
    return _cap_text(text, max_chars=max_chars, reference_file=reference_file)


def get_expected_json_example(
    role: Optional[Union[RoleId, str]] = None,
    stage_state: Optional[Union[TeamStageState, str]] = None,
    asking_role: Optional[Union[RoleId, str]] = None,
) -> str:
    """Returns a formatted JSON example string for a given role and stage_state."""
    role_norm = _normalize_enum_str(role)
    stage_norm = _normalize_enum_str(stage_state)
    asking_norm = _normalize_enum_str(asking_role) or "architect"

    if role_norm == "pm":
        if stage_norm == "pm_mockup_review":
            example: Dict[str, Any] = {
                "kind": "approval_request",
                "to": "user",
                "summary": "Mockup verified and ready for user review",
                "mockup_path": ".baton/mockup.html",
            }
        elif stage_norm == "pm_answer_relay":
            example = {
                "kind": "answer",
                "to": asking_norm,
                "summary": f"Answered {asking_norm} question",
            }
        elif stage_norm == "pm_final_summary":
            example = {
                "kind": "done",
                "to": "user",
                "summary": "Executive summary of verified implementation",
            }
        else:
            example = {
                "kind": "deliverable",
                "to": "architect",
                "needs_ui_mockup": False,
                "summary": "Requirements specification complete",
            }
    elif role_norm == "ui_designer":
        example = {
            "kind": "deliverable",
            "to": "pm",
            "mockup_path": ".baton/mockup.html",
            "summary": "Created interactive HTML mockup at .baton/mockup.html",
        }
    elif role_norm == "architect":
        example = {
            "kind": "deliverable",
            "to": "planner",
            "summary": "Completed architecture specification and runtime execution contract",
            "app_run": {
                "requires_ui_check": True,
                "start_cmd": "python3 -m http.server {port}",
                "app_url": "http://127.0.0.1:{port}",
                "healthcheck_path": "/",
                "stop_signal": "SIGTERM",
            },
        }
    elif role_norm == "planner":
        example = {
            "kind": "deliverable",
            "to": "implementer",
            "summary": "Completed file-by-file implementation plan and verification checklist",
            "app_run": {
                "requires_ui_check": True,
                "start_cmd": "python3 -m http.server {port}",
                "app_url": "http://127.0.0.1:{port}",
                "healthcheck_path": "/",
                "stop_signal": "SIGTERM",
            },
        }
    elif role_norm == "implementer":
        example = {
            "kind": "deliverable",
            "to": "checkers",
            "summary": "Implemented feature and verified file changes",
            "files_changed": ["path/to/file.py: summary of change"],
        }
    elif role_norm in ("ui_checker", "reviewer"):
        example = {
            "kind": "approve",
            "to": "pm",
            "summary": "All verification checks passed with no findings",
        }
    else:
        example = {
            "kind": "deliverable",
            "to": "pm",
            "summary": "Summary of completed step",
        }

    return json.dumps(example, indent=2)


def build_format_repair_prompt(
    error_or_parsed: Union[str, ParsedTeamMessage],
    stage_state: Union[TeamStageState, str] = "",
    role: Optional[Union[RoleId, str]] = None,
    *,
    asking_role: Optional[Union[RoleId, str]] = None,
    expected_json_example: Optional[str] = None,
) -> str:
    """Builds the single-retry format repair prompt (Section 4.3)."""
    if isinstance(error_or_parsed, ParsedTeamMessage):
        err_msg = error_or_parsed.error or "Invalid or missing baton-msg block."
    else:
        err_msg = str(error_or_parsed).strip() or "Invalid or missing baton-msg block."

    stage_str = _normalize_enum_str(stage_state) or "current"
    example_str = (
        expected_json_example.strip()
        if (expected_json_example and expected_json_example.strip())
        else get_expected_json_example(role=role, stage_state=stage_state, asking_role=asking_role)
    )

    return (
        "# Format Error — Missing or Invalid `baton-msg` Block\n\n"
        "Your previous response could not be parsed by Baton:\n"
        f"- Error: {err_msg}\n\n"
        "Do NOT repeat your entire analysis or redo file edits unless required. "
        "Reply with any brief correction followed by a valid `baton-msg` fenced JSON block "
        f"at the very end of your response matching one of your allowed transitions for state `{stage_str}`:\n\n"
        "```baton-msg\n"
        f"{example_str}\n"
        "```"
    )


def build_denied_action_repair_prompt(
    denied_actions: Sequence[str],
    error_or_parsed: Union[str, ParsedTeamMessage] = "",
    stage_state: Union[TeamStageState, str] = "",
    role: Optional[Union[RoleId, str]] = None,
    *,
    asking_role: Optional[Union[RoleId, str]] = None,
    expected_json_example: Optional[str] = None,
) -> str:
    """
    Repair prompt for a turn that ended with a missing/invalid `baton-msg` block because the
    CLI auto-denied one or more actions (headless mode cannot prompt for permission).
    Names the denied action(s) and tells the role to continue with file tools and read-only
    commands, then end with the `baton-msg` block.
    """
    if isinstance(error_or_parsed, ParsedTeamMessage):
        err_msg = error_or_parsed.error or ""
    else:
        err_msg = str(error_or_parsed or "").strip()
    err_msg = err_msg or "Invalid or missing baton-msg block."

    names = format_denied_actions(denied_actions)
    stage_str = _normalize_enum_str(stage_state) or "current"
    example_str = (
        expected_json_example.strip()
        if (expected_json_example and expected_json_example.strip())
        else get_expected_json_example(role=role, stage_state=stage_state, asking_role=asking_role)
    )

    return (
        "# Action Denied — Continue Without It\n\n"
        f"Your previous turn tried to use {names}, which was DENIED: Baton runs you headless and "
        "cannot prompt anyone to approve it, so the turn ended without a usable reply "
        f"({err_msg}).\n\n"
        "Do NOT retry the denied action, and do not try to work around it with another shell command. "
        "Proceed using only your file tools (reading and, if your permission allows, editing files) "
        "and read-only shell commands "
        "(ls, find, cat, head, tail, wc, grep, rg, tree, pwd, file, git status/diff/log/show/ls-files). "
        "Do NOT repeat analysis or redo file edits that already succeeded. Finish the task with what you "
        "have, then end your response with a valid `baton-msg` fenced JSON block matching one of your "
        f"allowed transitions for state `{stage_str}`:\n\n"
        "```baton-msg\n"
        f"{example_str}\n"
        "```"
    )


def format_denied_actions(denied_actions: Sequence[str]) -> str:
    """'`RunCommand`' / '`RunCommand`, `WriteFile`' for messages; 'an action' when empty."""
    names: List[str] = []
    for a in denied_actions or []:
        text = str(a).strip()
        if text and text not in names:
            names.append(text)
    if not names:
        return "an action"
    return ", ".join(f"`{n}`" for n in names)


def format_qa_history(qa_history: Optional[Sequence[Dict[str, Any]]]) -> str:
    """Formats Q&A rounds into readable markdown pairs, or 'None' if empty."""
    if not qa_history:
        return "None"
    blocks: List[str] = []
    for idx, entry in enumerate(qa_history, start=1):
        if not isinstance(entry, dict):
            continue
        questions = entry.get("questions") or []
        answers = entry.get("answers") or []
        round_num = entry.get("round", idx)
        from_role = entry.get("from", "pm")
        lines = [f"### Round {round_num} (from `{from_role}`)"]
        max_len = max(len(questions), len(answers))
        for q_idx in range(max_len):
            q_text = questions[q_idx] if q_idx < len(questions) else "(Question)"
            a_text = answers[q_idx] if q_idx < len(answers) else "(No answer provided)"
            lines.append(f"- **Q{q_idx + 1}**: {q_text}\n  **A{q_idx + 1}**: {a_text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "None"


def resolve_app_run_placeholders(
    app_run: Optional[Dict[str, Any]],
    allocated_port: int,
) -> Dict[str, Any]:
    """Resolves {port} and $BATON_APP_PORT placeholders in start_cmd and app_url."""
    raw = app_run if isinstance(app_run, dict) else {}
    port_str = str(allocated_port)
    start_cmd = str(raw.get("start_cmd") or f"python3 -m http.server {port_str}")
    app_url = str(raw.get("app_url") or f"http://127.0.0.1:{port_str}")

    start_cmd_resolved = (
        start_cmd.replace("{port}", port_str)
        .replace("${BATON_APP_PORT}", port_str)
        .replace("$BATON_APP_PORT", port_str)
    )
    app_url_resolved = (
        app_url.replace("{port}", port_str)
        .replace("${BATON_APP_PORT}", port_str)
        .replace("$BATON_APP_PORT", port_str)
    )
    return {
        "requires_ui_check": bool(raw.get("requires_ui_check", True)),
        "start_cmd": start_cmd_resolved,
        "app_url": app_url_resolved,
        "healthcheck_path": str(raw.get("healthcheck_path") or "/").strip() or "/",
        "stop_signal": str(raw.get("stop_signal") or "SIGTERM").strip() or "SIGTERM",
    }


# --- Prompt Template Builders (T1 - T12, Section 4.4) ---


def build_prompt_t1_pm_triage(
    task_prompt: str,
    worktree_path: Union[str, Path],
    base_branch: str,
    allocated_port: int,
    needs_ui_mockup_override: Optional[bool] = None,
    top_level_files: Optional[Sequence[str]] = None,
    mockup_override_description: Optional[str] = None,
) -> str:
    """Template T1: Initial Task to PM (pm_triage, Turn 1)."""
    wt_str = str(worktree_path)
    if mockup_override_description is None:
        if needs_ui_mockup_override is True:
            mockup_override_description = "Forced ON by user (needs_ui_mockup must be true)"
        elif needs_ui_mockup_override is False:
            mockup_override_description = "Forced OFF by user (needs_ui_mockup must be false)"
        else:
            mockup_override_description = "Auto (PM decides based on task)"

    if top_level_files is None:
        wt_path = Path(wt_str)
        if wt_path.exists() and wt_path.is_dir():
            entries = sorted(
                p.name + ("/" if p.is_dir() else "")
                for p in wt_path.iterdir()
                if p.name not in (".git", ".baton", "__pycache__", ".DS_Store")
            )
            top_level_files = entries[:60]
        else:
            top_level_files = []

    if top_level_files:
        file_list_str = "\n".join(f"  - `{f}`" for f in top_level_files)
    else:
        file_list_str = "  - *(empty or not listed)*"

    return (
        "# Team Run Task\n\n"
        f"{_cap_text(task_prompt)}\n\n"
        "---\n\n"
        "# Target Repository Context\n"
        f"- Working Directory (Git Worktree): `{wt_str}`\n"
        f"- Base Branch: `{base_branch}`\n"
        f"- Baton Allocated App Port: `{allocated_port}`\n"
        f"- Mockup Override Flag: `{mockup_override_description}`\n"
        "- Top-Level Repository Files:\n"
        f"{file_list_str}\n\n"
        "Please inspect the repository as needed, decide whether a UI mockup is required "
        "(`needs_ui_mockup`), and either ask clarifying questions to `user` or emit your "
        "`deliverable` to `ui_designer` (if `needs_ui_mockup: true`) or `architect` "
        "(if `needs_ui_mockup: false`)."
    )


def build_prompt_t2_user_answers_to_pm(
    questions: Optional[Sequence[str]] = None,
    answers: Optional[Sequence[str]] = None,
    asking_role: Optional[Union[RoleId, str]] = None,
    *,
    formatted_qa_pairs: Optional[str] = None,
) -> str:
    """Template T2: User Answers Returned to PM (waiting_user_answers -> pm_triage or pm_answer_relay)."""
    if formatted_qa_pairs is None:
        q_list = list(questions or [])
        a_list = list(answers or [])
        max_len = max(len(q_list), len(a_list))
        if max_len == 0:
            qa_str = "*(No questions/answers recorded)*"
        else:
            lines: List[str] = []
            for idx in range(max_len):
                q_item = q_list[idx] if idx < len(q_list) else f"Question {idx + 1}"
                a_item = a_list[idx] if idx < len(a_list) else "(No answer provided)"
                lines.append(f"{idx + 1}. **Q**: {q_item}\n   **A**: {a_item}")
            qa_str = "\n\n".join(lines)
    else:
        qa_str = formatted_qa_pairs.strip()

    asking_norm = _normalize_enum_str(asking_role)
    if not asking_norm:
        next_step = "If everything is now clear, emit your deliverable to 'ui_designer' or 'architect'."
    else:
        next_step = f"Relay the resolved answer to '{asking_norm}' using kind='answer' and to='{asking_norm}'."

    return (
        "# User Answers to Your Clarifying Questions\n\n"
        f"{_cap_text(qa_str)}\n\n"
        "---\n\n"
        f"{next_step}"
    )


def build_prompt_t3_pm_to_ui_designer(
    task_prompt: str,
    pm_requirements_markdown: str,
    qa_transcript_or_none: Optional[Union[str, Sequence[Dict[str, Any]]]] = None,
) -> str:
    """Template T3: PM -> UI Designer (ui_design, Turn 1)."""
    if isinstance(qa_transcript_or_none, str):
        qa_text = qa_transcript_or_none.strip() or "None"
    else:
        qa_text = format_qa_history(qa_transcript_or_none)

    return (
        "# UI Mockup Task from PM\n\n"
        "## Original User Request\n"
        f"{_cap_text(task_prompt)}\n\n"
        "## Clarified Requirements & Acceptance Criteria (from PM)\n"
        f"{_cap_text(pm_requirements_markdown, MAX_EXCERPT_CHARS, '.baton/requirements.md')}\n\n"
        "## User Q&A Transcript\n"
        f"{_cap_text(qa_text, MAX_EXCERPT_CHARS, '.baton/requirements.md')}\n\n"
        "---\n\n"
        "# Instructions\n"
        "Inspect the existing frontend styles in the workspace and write a single, "
        "self-contained HTML5 mockup to `.baton/mockup.html`.\n"
        "Do NOT modify any files outside `.baton/`. Conclude with your `baton-msg` block (`to: \"pm\"`)."
    )


def build_prompt_t4_mockup_revision(
    mockup_version: int,
    feedback_source: str,
    feedback_text: Union[str, Sequence[str]],
) -> str:
    """Template T4: Mockup Revision Request to UI Designer."""
    if isinstance(feedback_text, (list, tuple)):
        fb_str = "\n".join(f"- {item}" for item in feedback_text if str(item).strip())
    else:
        fb_str = str(feedback_text or "").strip()

    return (
        f"# Mockup Changes Requested (Iteration {mockup_version + 1})\n\n"
        f"## Feedback from {feedback_source}\n"
        f"{_cap_text(fb_str)}\n\n"
        "---\n\n"
        "Please update `.baton/mockup.html` to address all feedback above and conclude "
        "with your `baton-msg` deliverable block (`to: \"pm\"`)."
    )


def _format_mockup_status_section(
    needs_ui_mockup: Optional[bool],
    mockup_approved: bool = False,
    mockup_path: str = ".baton/mockup.html",
) -> str:
    if needs_ui_mockup:
        status_str = "Approved" if mockup_approved else "Generated"
        return f"- UI Mockup ({status_str}): `{mockup_path}` (inspect this file in the workspace for exact UI structure and states)."
    return "- No UI mockup required for this task."


def _format_mockup_reference_line(
    needs_ui_mockup: Optional[bool],
    mockup_path: str = ".baton/mockup.html",
) -> str:
    if needs_ui_mockup:
        return f"- Approved UI Mockup: `{mockup_path}`"
    return "- UI Mockup: None (not applicable)"


def build_prompt_t5_pm_to_architect(
    task_prompt: str,
    pm_requirements_markdown: str,
    allocated_port: int,
    qa_transcript_or_none: Optional[Union[str, Sequence[Dict[str, Any]]]] = None,
    needs_ui_mockup: Optional[bool] = False,
    mockup_approved: bool = False,
    mockup_status_section: Optional[str] = None,
) -> str:
    """Template T5: PM -> Code Architect (architecture, Turn 1)."""
    if isinstance(qa_transcript_or_none, str):
        qa_text = qa_transcript_or_none.strip() or "None"
    else:
        qa_text = format_qa_history(qa_transcript_or_none)

    eff_mockup_section = (
        mockup_status_section.strip()
        if (mockup_status_section and mockup_status_section.strip())
        else _format_mockup_status_section(needs_ui_mockup, mockup_approved)
    )

    return (
        "# Architecture Task from PM\n\n"
        "## Original User Request\n"
        f"{_cap_text(task_prompt)}\n\n"
        "## Clarified Requirements & Acceptance Criteria (from PM)\n"
        f"{_cap_text(pm_requirements_markdown, MAX_EXCERPT_CHARS, '.baton/requirements.md')}\n\n"
        "## User Q&A Transcript\n"
        f"{_cap_text(qa_text, MAX_EXCERPT_CHARS, '.baton/requirements.md')}\n\n"
        "## UI Mockup Status\n"
        f"{eff_mockup_section}\n\n"
        "## Baton Runtime Port Assignment\n"
        f"- Allocated Safe Local Port: `{allocated_port}` "
        f"(Use `127.0.0.1:{allocated_port}` in your `app_run` contract; never use ports 5000, 7000, 5090, 5186, or 8765.)\n\n"
        "---\n\n"
        "# Instructions\n"
        "Inspect the repository and produce the complete Technical Architecture Specification "
        "(which Baton will save to `.baton/architecture.md`). Conclude with your `baton-msg` "
        "block (`to: \"planner\"`, including `app_run`)."
    )


def build_prompt_t6_specialist_question_to_pm(
    asking_role: Union[RoleId, str],
    previous_stage_state: Union[TeamStageState, str],
    questions: Union[Sequence[str], str],
    specialist_body_markdown: str = "",
    *,
    asking_role_label: Optional[str] = None,
    force_binding_decision: bool = False,
) -> str:
    """Template T6: Specialist Question -> PM (pm_answer_relay)."""
    role_str = _normalize_enum_str(asking_role)
    label_str = asking_role_label or get_role_label(role_str)
    prev_state_str = _normalize_enum_str(previous_stage_state)

    if isinstance(questions, str):
        numbered_q = questions.strip()
    else:
        numbered_q = "\n".join(
            f"{idx}. {q}" for idx, q in enumerate(questions, start=1) if str(q).strip()
        )

    extra_limit_note = ""
    if force_binding_decision:
        extra_limit_note = (
            "\n\nNOTE: Maximum question rounds for this stage have been reached. "
            f"You MUST emit a binding `kind: \"answer\"` with `to: \"{role_str}\"` so work can proceed."
        )

    return (
        f"# Question from {label_str} (`{role_str}`)\n\n"
        f"The {label_str} paused work in state `{prev_state_str}` and asked:\n\n"
        f"{numbered_q}\n\n"
        f"## Context from {label_str}\n"
        f"{_cap_text(specialist_body_markdown) or '(No additional context)'}\n\n"
        "---\n\n"
        "# Instructions\n"
        "If you can answer these questions using the original task, prior user Q&A, "
        f"or by inspecting the repository (read-only), emit `kind: \"answer\"` with `to: \"{role_str}\"`.\n"
        "Only if a product decision from the user is strictly required, escalate by emitting "
        f"`kind: \"question\"` with `to: \"user\"`.{extra_limit_note}"
    )


def build_prompt_t7_pm_answer_to_specialist(
    asking_role: Union[RoleId, str],
    pm_answer_markdown: str,
    *,
    force_deliverable: bool = False,
) -> str:
    """Template T7: PM Answer -> Specialist (pm_answer_relay -> asking_role)."""
    role_str = _normalize_enum_str(asking_role)
    force_note = (
        " Maximum question rounds reached — you MUST now complete your work and emit a `deliverable` block."
        if force_deliverable
        else ""
    )
    return (
        "# Answer from PM\n\n"
        f"{_cap_text(pm_answer_markdown)}\n\n"
        "---\n\n"
        f"Please resume your role (`{role_str}`) and emit your `deliverable` `baton-msg` block when finished.{force_note}"
    )


def build_prompt_t8_architect_to_planner(
    task_prompt: str,
    pm_requirements_excerpt: str,
    architecture_excerpt: str,
    app_run: Union[Dict[str, Any], str],
    allocated_port: int,
    needs_ui_mockup: Optional[bool] = False,
    mockup_approved: bool = False,
    mockup_status_section: Optional[str] = None,
) -> str:
    """Template T8: Code Architect -> Planner (planning, Turn 1)."""
    eff_mockup_section = (
        mockup_status_section.strip()
        if (mockup_status_section and mockup_status_section.strip())
        else _format_mockup_status_section(needs_ui_mockup, mockup_approved)
    )
    if isinstance(app_run, str):
        app_run_json = app_run.strip()
    else:
        app_run_json = json.dumps(app_run or {}, indent=2)

    return (
        "# Planning Task\n\n"
        "## Original User Request\n"
        f"{_cap_text(task_prompt)}\n\n"
        "## PM Requirements Summary (`.baton/requirements.md`)\n"
        f"{_cap_text(pm_requirements_excerpt, MAX_EXCERPT_CHARS, '.baton/requirements.md')}\n\n"
        "## UI Mockup Status\n"
        f"{eff_mockup_section}\n\n"
        "## Code Architect Specification (`.baton/architecture.md`)\n"
        f"{_cap_text(architecture_excerpt, MAX_EXCERPT_CHARS, '.baton/architecture.md')}\n\n"
        f"## Architect `app_run` Contract (Port `{allocated_port}`)\n"
        "```json\n"
        f"{app_run_json}\n"
        "```\n\n"
        "---\n\n"
        "# Instructions\n"
        "Produce the ordered, file-by-file Implementation Plan and verification checklist "
        "(saved by Baton to `.baton/plan.md`). Conclude with your `baton-msg` deliverable "
        "block (`to: \"implementer\"`, including `app_run`)."
    )


def build_prompt_t9_planner_to_implementer(
    task_prompt: str,
    plan_excerpt: str,
    needs_ui_mockup: Optional[bool] = False,
    mockup_file_reference_line: Optional[str] = None,
) -> str:
    """Template T9: Planner -> Implementer (implementing, Turn 1)."""
    eff_mockup_line = (
        mockup_file_reference_line.strip()
        if (mockup_file_reference_line and mockup_file_reference_line.strip())
        else _format_mockup_reference_line(needs_ui_mockup)
    )

    return (
        "# Implementation Task\n\n"
        "## Original User Request\n"
        f"{_cap_text(task_prompt)}\n\n"
        "## Key Reference Files in Workspace\n"
        "- Requirements: `.baton/requirements.md`\n"
        "- Architecture: `.baton/architecture.md`\n"
        "- Implementation Plan: `.baton/plan.md`\n"
        f"{eff_mockup_line}\n\n"
        "## Implementation Plan (`.baton/plan.md`)\n"
        f"{_cap_text(plan_excerpt, MAX_EXCERPT_CHARS, '.baton/plan.md')}\n\n"
        "---\n\n"
        "# Instructions\n"
        "Execute the implementation plan in the working directory. Do NOT modify files in "
        "`.baton/` and never run `git commit`, `git push`, `git reset`, or `git checkout`. "
        "Conclude with your `baton-msg` deliverable block (`to: \"checkers\"`)."
    )


def build_prompt_t10_ui_checker(
    task_prompt: str,
    allocated_port: int,
    app_run: Optional[Dict[str, Any]],
    implementer_output_excerpt: str,
    plan_excerpt: str,
    rework_loop_count: int = 0,
    needs_ui_mockup: Optional[bool] = False,
    mockup_file_reference_line: Optional[str] = None,
) -> str:
    """Template T10 (UI Checker): Implementer -> UI Checker (checking)."""
    resolved = resolve_app_run_placeholders(app_run, allocated_port)
    eff_mockup_line = (
        mockup_file_reference_line.strip()
        if (mockup_file_reference_line and mockup_file_reference_line.strip())
        else _format_mockup_reference_line(needs_ui_mockup)
    )

    return (
        f"# UI & Runtime Verification Task (Check Pass #{rework_loop_count + 1})\n\n"
        "## Original User Request\n"
        f"{_cap_text(task_prompt)}\n\n"
        "## Runtime Execution Contract (`app_run`)\n"
        f"- Allocated Port (`$BATON_APP_PORT`): `{allocated_port}`\n"
        f"- Start Command: `{resolved['start_cmd']}`\n"
        f"- App URL (`$BATON_APP_URL`): `{resolved['app_url']}`\n"
        f"- Healthcheck Path: `{resolved['healthcheck_path']}`\n"
        f"- Requires Headless Browser UI Check: `{resolved['requires_ui_check']}`\n"
        f"{eff_mockup_line}\n\n"
        "## Implementer Report\n"
        f"{_cap_text(implementer_output_excerpt)}\n\n"
        "## Planned Verification Scenarios (from `.baton/plan.md`)\n"
        f"{_cap_text(plan_excerpt, MAX_EXCERPT_CHARS, '.baton/plan.md')}\n\n"
        "---\n\n"
        "# Instructions\n"
        f"Write and run `.baton/ui_check.py` (run it with `\"$BATON_PYTHON\" .baton/ui_check.py`, not bare `python3`) to start the app on port `{allocated_port}`, "
        "verify the UI/runtime behavior using headless Playwright, and terminate the app "
        "process group in a `finally:` block before finishing. Conclude with `kind: \"approve\"` "
        "(`to: \"pm\"`) or `kind: \"reject\"` (`to: \"implementer\"`, with `findings`)."
    )


def build_prompt_t10_reviewer(
    task_prompt: str,
    implementer_output_excerpt: str,
    git_diff_stat: str,
    rework_loop_count: int = 0,
    needs_ui_mockup: Optional[bool] = False,
    mockup_file_reference_line: Optional[str] = None,
) -> str:
    """Template T10 (Code Reviewer): Implementer -> Code Reviewer (checking)."""
    eff_mockup_line = (
        mockup_file_reference_line.strip()
        if (mockup_file_reference_line and mockup_file_reference_line.strip())
        else _format_mockup_reference_line(needs_ui_mockup)
    )
    stat_text = (git_diff_stat or "").strip() or "(No diff stat available)"

    return (
        f"# Code Review Task (Check Pass #{rework_loop_count + 1})\n\n"
        "## Original User Request\n"
        f"{_cap_text(task_prompt)}\n\n"
        "## Implementer Report\n"
        f"{_cap_text(implementer_output_excerpt)}\n\n"
        "## Git Diff Stat in Worktree\n"
        "```text\n"
        f"{_cap_text(stat_text)}\n"
        "```\n\n"
        "## Full Diff\n"
        f"The complete unified diff against the base commit is in `{REVIEW_DIFF_FILENAME_REL}` "
        "(read it with your file-reading tool; it includes deleted lines and removed files).\n\n"
        "## Reference Specifications\n"
        "- Architecture: `.baton/architecture.md`\n"
        "- Implementation Plan: `.baton/plan.md`\n"
        f"{eff_mockup_line}\n\n"
        "---\n\n"
        "# Instructions\n"
        "Read the full diff first, then inspect the modified and created files in the workspace against the requirements, "
        "architecture, and plan. Do NOT modify any files. Conclude with `kind: \"approve\"` "
        "(`to: \"pm\"`) or `kind: \"reject\"` (`to: \"implementer\"`, with `findings`)."
    )


def build_prompt_t10_checkers(
    task_prompt: str,
    allocated_port: int,
    app_run: Optional[Dict[str, Any]],
    implementer_output_excerpt: str,
    plan_excerpt: str,
    git_diff_stat: str,
    rework_loop_count: int = 0,
    needs_ui_mockup: Optional[bool] = False,
) -> Tuple[str, str]:
    """Convenience builder returning (ui_checker_prompt, reviewer_prompt) for Template T10."""
    ui_p = build_prompt_t10_ui_checker(
        task_prompt=task_prompt,
        allocated_port=allocated_port,
        app_run=app_run,
        implementer_output_excerpt=implementer_output_excerpt,
        plan_excerpt=plan_excerpt,
        rework_loop_count=rework_loop_count,
        needs_ui_mockup=needs_ui_mockup,
    )
    rev_p = build_prompt_t10_reviewer(
        task_prompt=task_prompt,
        implementer_output_excerpt=implementer_output_excerpt,
        git_diff_stat=git_diff_stat,
        rework_loop_count=rework_loop_count,
        needs_ui_mockup=needs_ui_mockup,
    )
    return ui_p, rev_p


def _format_checker_findings_section(
    verdict: str,
    findings: Optional[Union[Sequence[str], str]] = None,
) -> str:
    if verdict.upper() == "APPROVED":
        return "Passed with no findings."
    if isinstance(findings, str):
        text = findings.strip()
        return text if text else "- Changes requested."
    items = [str(f).strip() for f in (findings or []) if str(f).strip()]
    if not items:
        return "- Changes requested."
    return "\n".join(f if f.startswith("-") else f"- {f}" for f in items)


def build_prompt_t11_rework(
    rework_loop_count: int,
    max_rework_loops: int,
    ui_verdict: str,
    ui_findings: Optional[Union[Sequence[str], str]],
    code_verdict: str,
    code_findings: Optional[Union[Sequence[str], str]],
) -> str:
    """Template T11: Checkers -> Implementer Rework (checking -> implementing)."""
    ui_v = "APPROVED" if ui_verdict.upper() in ("APPROVED", "APPROVE") else "FAILED"
    code_v = "APPROVED" if code_verdict.upper() in ("APPROVED", "APPROVE") else "FAILED"
    ui_section = _format_checker_findings_section(ui_v, ui_findings)
    code_section = _format_checker_findings_section(code_v, code_findings)

    return (
        f"# Verification & Review Feedback (Rework Loop {rework_loop_count} of {max_rework_loops})\n\n"
        "One or both verification stages requested changes.\n\n"
        f"## UI Checker Status: {ui_v}\n"
        f"{_cap_text(ui_section)}\n\n"
        f"## Code Reviewer Status: {code_v}\n"
        f"{_cap_text(code_section)}\n\n"
        "---\n\n"
        "Please fix every finding above in the working directory without introducing regressions, "
        "and conclude with your `baton-msg` deliverable block (`to: \"checkers\"`)."
    )


def build_prompt_t12_pm_final_summary(
    rework_loop_count: int,
    implementer_summary: str,
    ui_checker_summary: str,
    code_reviewer_summary: str,
    git_diff_stat: str,
) -> str:
    """Template T12: All Approved -> PM Final Report (checking -> pm_final_summary)."""
    stat_text = (git_diff_stat or "").strip() or "(No diff stat available)"
    return (
        "# All Verification Checks Approved — Prepare Final Sign-off Summary\n\n"
        f"Both the UI Checker and Code Reviewer have approved the implementation "
        f"(after {rework_loop_count} rework loop(s)).\n\n"
        "## Implementer Summary\n"
        f"{_cap_text(implementer_summary)}\n\n"
        "## UI Checker Report\n"
        f"{_cap_text(ui_checker_summary)}\n\n"
        "## Code Reviewer Report\n"
        f"{_cap_text(code_reviewer_summary)}\n\n"
        "## Workspace Git Diff Summary\n"
        "```text\n"
        f"{_cap_text(stat_text)}\n"
        "```\n\n"
        "---\n\n"
        "Write a clear, concise final report for the user summarizing what was built, "
        "how it was verified, and what files changed. Conclude with `kind: \"done\"` and `to: \"user\"`."
    )


def build_prompt_pm_mockup_review(
    ui_designer_summary: str,
    ui_designer_body: str = "",
    mockup_path: str = ".baton/mockup.html",
    mockup_version: int = 1,
) -> str:
    """Builds the prompt sent to PM when UI Designer delivers .baton/mockup.html."""
    notes = _cap_text(ui_designer_body or ui_designer_summary)
    return (
        f"# UI Mockup Delivered for Review (v{mockup_version})\n\n"
        f"The UI Designer wrote `{mockup_path}`.\n\n"
        "## UI Designer Notes\n"
        f"{notes}\n\n"
        "---\n\n"
        "# Instructions\n"
        f"Inspect `{mockup_path}` in the workspace to verify it is a complete, self-contained HTML "
        "file matching the requirements. If it is broken or misses core requirements, emit "
        "`kind: \"reject\"` with `to: \"ui_designer\"` and `findings`. If it satisfies the "
        "requirements, emit `kind: \"approval_request\"` with `to: \"user\"` and "
        f"`\"mockup_path\": \"{mockup_path}\"`."
    )


# --- Short Aliases for Template Builders ---

build_prompt_t1 = build_prompt_t1_pm_triage
build_prompt_t2 = build_prompt_t2_user_answers_to_pm
build_prompt_t3 = build_prompt_t3_pm_to_ui_designer
build_prompt_t4 = build_prompt_t4_mockup_revision
build_prompt_t5 = build_prompt_t5_pm_to_architect
build_prompt_t6 = build_prompt_t6_specialist_question_to_pm
build_prompt_t7 = build_prompt_t7_pm_answer_to_specialist
build_prompt_t8 = build_prompt_t8_architect_to_planner
build_prompt_t9 = build_prompt_t9_planner_to_implementer
build_prompt_t10 = build_prompt_t10_checkers
build_prompt_t11 = build_prompt_t11_rework
build_prompt_t12 = build_prompt_t12_pm_final_summary


# --- .baton/ Artifact & Browser Helper Utilities (Sections 4.4, 5.2, 5.3) ---

BATON_BROWSER_HELPER_SOURCE: str = '''"""Baton headless browser and app lifecycle helper for UI Checker.

Run scripts that import this helper with Baton's interpreter (it has Playwright):
    "$BATON_PYTHON" .baton/ui_check.py
"""
from __future__ import annotations

from contextlib import contextmanager
from html.parser import HTMLParser
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Generator, List, Optional


class _FallbackPage:
    """Lightweight urllib + HTMLParser fallback when playwright is not installed."""

    def __init__(self, app_url: str) -> None:
        self.url = app_url
        self.html = ""
        self.status_code = 0
        self.console_errors: List[str] = []
        self.page_errors: List[str] = []

    def goto(self, url: str, timeout: int = 10000) -> "_FallbackPage":
        self.url = url
        req = urllib.request.Request(url, headers={"User-Agent": "BatonUIChecker/1.0"})
        with urllib.request.urlopen(req, timeout=max(1.0, timeout / 1000.0)) as resp:
            self.status_code = getattr(resp, "status", 200)
            self.html = resp.read().decode("utf-8", errors="replace")
        return self

    def content(self) -> str:
        return self.html

    def title(self) -> str:
        lower = self.html.lower()
        start = lower.find("<title>")
        end = lower.find("</title>")
        if start != -1 and end != -1 and end > start:
            return self.html[start + 7 : end].strip()
        return ""

    def screenshot(self, path: Optional[str] = None, **kwargs: Any) -> bytes:
        return b""


def _wait_for_healthcheck(url: str, timeout_sec: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BatonHealthcheck/1.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= status < 500:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _terminate_process_group(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass

    deadline = time.monotonic() + 3.0
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)

    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


@contextmanager
def run_app_and_check(
    start_cmd: str,
    app_url: str,
    healthcheck_path: str = "/",
    check_fn: Optional[Callable[[Any], Any]] = None,
    *,
    allocated_port: Optional[int] = None,
    startup_timeout_sec: float = 15.0,
    screenshot_path: str = ".baton/screenshots/final.png",
    app_log_path: str = ".baton/app.log",
) -> Generator[Dict[str, Any], None, None]:
    """Starts start_cmd in a new process group, polls healthcheck, runs Playwright (or fallback), and kills on exit."""
    env = dict(os.environ)
    if allocated_port is not None:
        env["PORT"] = str(allocated_port)
        env["BATON_APP_PORT"] = str(allocated_port)

    cmd_args = shlex.split(start_cmd)
    # Send app output to a file: an undrained PIPE blocks a chatty dev server once the
    # pipe buffer (~64 KiB) fills, which would hang the app mid-check.
    log_path = Path(app_log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            cmd_args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except Exception:
        log_file.close()
        raise
    result: Dict[str, Any] = {
        "ready": False,
        "used_playwright": False,
        "console_errors": [],
        "page_errors": [],
        "screenshot_path": screenshot_path,
        "app_log_path": app_log_path,
    }
    try:
        hc_url = app_url.rstrip("/") + "/" + (healthcheck_path or "/").lstrip("/")
        result["ready"] = _wait_for_healthcheck(hc_url, timeout_sec=startup_timeout_sec)
        if not result["ready"]:
            raise RuntimeError(f"Application did not become ready at {hc_url} within {startup_timeout_sec}s")

        try:
            from playwright.sync_api import sync_playwright  # type: ignore

            has_playwright = True
        except ImportError:
            has_playwright = False

        if has_playwright:
            from playwright.sync_api import sync_playwright  # type: ignore

            result["used_playwright"] = True
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.on("pageerror", lambda err: result["page_errors"].append(str(err)))
                page.on(
                    "console",
                    lambda msg: (
                        result["console_errors"].append(msg.text)
                        if getattr(msg, "type", "") == "error"
                        else None
                    ),
                )
                page.goto(app_url)
                if check_fn is not None:
                    check_fn(page)
                if screenshot_path:
                    shot_file = Path(screenshot_path)
                    shot_file.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shot_file))
                browser.close()
        else:
            fallback_page = _FallbackPage(app_url)
            fallback_page.goto(app_url)
            if check_fn is not None:
                check_fn(fallback_page)

        yield result
    finally:
        _terminate_process_group(proc)
        log_file.close()
'''


def _baton_dir(worktree_path: Union[str, Path]) -> Path:
    """Returns and ensures <worktree_path>/.baton directory exists."""
    baton_path = Path(worktree_path) / ".baton"
    baton_path.mkdir(parents=True, exist_ok=True)
    return baton_path


def write_baton_artifact(
    worktree_path: Union[str, Path],
    filename: str,
    content: str,
) -> Path:
    """Writes a markdown or text artifact inside <worktree_path>/.baton/<filename>."""
    safe_name = Path(filename).name
    target = _baton_dir(worktree_path) / safe_name
    target.write_text(content or "", encoding="utf-8")
    return target


def read_baton_artifact(
    worktree_path: Union[str, Path],
    filename: str,
) -> str:
    """Reads an artifact from <worktree_path>/.baton/<filename>, returning '' if missing."""
    safe_name = Path(filename).name
    target = Path(worktree_path) / ".baton" / safe_name
    if not target.exists() or not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def write_requirements_artifact(
    worktree_path: Union[str, Path],
    requirements_markdown: str,
    qa_history: Optional[Sequence[Dict[str, Any]]] = None,
) -> Path:
    """Writes .baton/requirements.md combining PM requirements and optional Q&A history."""
    body = (requirements_markdown or "").strip()
    if qa_history:
        qa_str = format_qa_history(qa_history)
        if qa_str and qa_str != "None":
            body = f"{body}\n\n## User Q&A Transcript\n\n{qa_str}"
    return write_baton_artifact(worktree_path, "requirements.md", body)


def read_requirements_artifact(worktree_path: Union[str, Path]) -> str:
    """Reads .baton/requirements.md from the worktree."""
    return read_baton_artifact(worktree_path, "requirements.md")


def write_architecture_artifact(
    worktree_path: Union[str, Path],
    architecture_markdown: str,
) -> Path:
    """Writes .baton/architecture.md in the worktree."""
    return write_baton_artifact(worktree_path, "architecture.md", architecture_markdown)


def read_architecture_artifact(worktree_path: Union[str, Path]) -> str:
    """Reads .baton/architecture.md from the worktree."""
    return read_baton_artifact(worktree_path, "architecture.md")


def write_plan_artifact(
    worktree_path: Union[str, Path],
    plan_markdown: str,
) -> Path:
    """Writes .baton/plan.md in the worktree."""
    return write_baton_artifact(worktree_path, "plan.md", plan_markdown)


def read_plan_artifact(worktree_path: Union[str, Path]) -> str:
    """Reads .baton/plan.md from the worktree."""
    return read_baton_artifact(worktree_path, "plan.md")


def check_mockup_deliverable(
    worktree_path: Union[str, Path],
    mockup_rel_path: str = ".baton/mockup.html",
    min_bytes: int = MIN_MOCKUP_BYTES,
) -> Tuple[bool, Optional[str]]:
    """Checks that .baton/mockup.html exists inside worktree_path and is non-trivial (>= min_bytes)."""
    wt_root = Path(worktree_path).resolve()
    baton_root = (wt_root / ".baton").resolve()
    rel = (mockup_rel_path or ".baton/mockup.html").strip().lstrip("/")
    candidate = (wt_root / rel).resolve()

    try:
        candidate.relative_to(baton_root)
    except ValueError:
        return False, f"Mockup path '{mockup_rel_path}' must reside inside '.baton/'."

    if not candidate.exists() or not candidate.is_file():
        return (
            False,
            f"UI Designer emitted deliverable, but '{mockup_rel_path}' does not exist on disk. "
            "Please write the HTML mockup to '.baton/mockup.html' before emitting your deliverable.",
        )

    try:
        content = candidate.read_text(encoding="utf-8", errors="replace").strip()
        size_bytes = len(content.encode("utf-8"))
    except OSError as exc:
        return False, f"Could not read mockup file '{mockup_rel_path}': {exc}"

    if size_bytes < min_bytes:
        return (
            False,
            f"Mockup file '{mockup_rel_path}' is empty or trivial ({size_bytes} bytes < {min_bytes} bytes). "
            "Please write a complete, self-contained HTML5 mockup to '.baton/mockup.html'.",
        )

    return True, None


check_mockup_exists = check_mockup_deliverable
validate_mockup_file = check_mockup_deliverable


def snapshot_mockup_version(
    worktree_path: Union[str, Path],
    version: int,
    mockup_rel_path: str = ".baton/mockup.html",
) -> Optional[Path]:
    """Copies .baton/mockup.html to .baton/mockup_v{version}.html and returns the destination path."""
    ok, _ = check_mockup_deliverable(worktree_path, mockup_rel_path=mockup_rel_path)
    if not ok:
        return None
    wt_root = Path(worktree_path).resolve()
    src = (wt_root / mockup_rel_path.strip().lstrip("/")).resolve()
    dst = _baton_dir(wt_root) / f"mockup_v{version}.html"
    shutil.copyfile(src, dst)
    return dst


def write_browser_helper(worktree_path: Union[str, Path]) -> Path:
    """Writes .baton/baton_browser_helper.py into the worktree and ensures .baton/screenshots/ exists."""
    baton_path = _baton_dir(worktree_path)
    (baton_path / "screenshots").mkdir(parents=True, exist_ok=True)
    helper_path = baton_path / "baton_browser_helper.py"
    helper_path.write_text(BATON_BROWSER_HELPER_SOURCE, encoding="utf-8")
    return helper_path
