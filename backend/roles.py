from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Union
from backend.models import PermissionLevel, RoleId


@dataclass(frozen=True)
class RoleDefinition:
    id: RoleId
    name: str
    description: str
    default_permission: PermissionLevel
    system_prompt: str


ROLE_DEFINITIONS: Dict[RoleId, RoleDefinition] = {
    RoleId.DESIGNER: RoleDefinition(
        id=RoleId.DESIGNER,
        name="Designer",
        description="Produces system architecture and UI/UX design specifications. Read-only by default.",
        default_permission=PermissionLevel.READONLY,
        system_prompt=(
            "You are the Software Architect & Product Designer in a multi-agent engineering pipeline.\n"
            "Your job is to inspect the repository and requirements, then output a clear, concrete design "
            "specification covering data models, component structure, user flows, edge cases, and visual/API contracts.\n"
            "Do NOT modify or create project code files. Output your complete design directly in your response so "
            "the next stage (Planner) can act on it."
        ),
    ),
    RoleId.PLANNER: RoleDefinition(
        id=RoleId.PLANNER,
        name="Planner",
        description="Breaks tasks and designs into step-by-step file-level implementation plans. Read-only by default.",
        default_permission=PermissionLevel.READONLY,
        system_prompt=(
            "You are the Lead Engineering Planner in a multi-agent engineering pipeline.\n"
            "Examine the workspace and any prior design output, then produce a concrete, ordered implementation plan.\n"
            "Specify exact file paths to create or modify, function/class signatures, edge cases to handle, "
            "and verification criteria. Do NOT edit files yourself. Output the complete actionable plan in your reply."
        ),
    ),
    RoleId.IMPLEMENTER: RoleDefinition(
        id=RoleId.IMPLEMENTER,
        name="Implementer",
        description="Writes and edits code in the working directory according to the plan or review feedback.",
        default_permission=PermissionLevel.EDIT,
        system_prompt=(
            "You are the Implementer in a multi-agent engineering pipeline.\n"
            "Carry out the requested implementation or address all code review findings directly in the workspace.\n"
            "Follow existing codebase conventions, do not touch unrelated files, and never run git commit/push/reset.\n"
            "End your response with a concise report listing:\n"
            "1. Every file created or modified and a one-line summary of the change.\n"
            "2. How you verified the changes or any constraints encountered."
        ),
    ),
    RoleId.REVIEWER: RoleDefinition(
        id=RoleId.REVIEWER,
        name="Code Reviewer",
        description="Reviews workspace changes against requirements and emits a machine-readable verdict.",
        default_permission=PermissionLevel.READONLY,
        system_prompt=(
            "You are the Code Reviewer in a multi-agent engineering pipeline.\n"
            "Inspect the actual files and diffs in the working directory against the original task and plan.\n"
            "Do NOT modify any files. Check for correctness, regressions, edge cases, security issues, and adherence to spec.\n\n"
            "CRITICAL REQUIREMENT — MACHINE-READABLE VERDICT:\n"
            "You MUST end your response with a verdict block in the exact format below (at the very end of your reply):\n\n"
            "If the implementation is complete and correct:\n"
            "VERDICT: APPROVED\n\n"
            "If changes or fixes are required:\n"
            "VERDICT: CHANGES_REQUESTED\n"
            "FINDINGS:\n"
            "- [file/path:line] Concrete description of issue 1 and how to fix it.\n"
            "- [file/path:line] Concrete description of issue 2 and how to fix it."
        ),
    ),
    RoleId.CUSTOM: RoleDefinition(
        id=RoleId.CUSTOM,
        name="Custom",
        description="Custom role with user-defined system instructions.",
        default_permission=PermissionLevel.READONLY,
        system_prompt="",
    ),
}


def get_role_label(role: Union[RoleId, str]) -> str:
    """Returns the human-readable label for a role ID (e.g. 'Code Reviewer' for 'reviewer')."""
    try:
        role_enum = role if isinstance(role, RoleId) else RoleId(str(role))
        role_def = ROLE_DEFINITIONS.get(role_enum)
        if role_def is not None:
            return role_def.name
    except ValueError:
        pass
    return str(role)


def resolve_system_prompt(role: RoleId, custom_prompt: Optional[str] = None) -> str:
    """
    For RoleId.CUSTOM, returns (custom_prompt or "").strip().
    For built-in roles, returns ROLE_DEFINITIONS[role].system_prompt, appending
    custom_prompt if non-empty.
    """
    cleaned_custom = (custom_prompt or "").strip()
    if role == RoleId.CUSTOM:
        return cleaned_custom

    role_def = ROLE_DEFINITIONS.get(role)
    base_prompt = role_def.system_prompt if role_def else ""
    if cleaned_custom:
        return f"{base_prompt}\n\nAdditional Instructions:\n{cleaned_custom}"
    return base_prompt
