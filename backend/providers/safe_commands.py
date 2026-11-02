"""Read-only shell commands that non-`full` roles may run.

Headless CLIs cannot prompt for permission, so any shell command that is not explicitly
allow-listed is auto-denied and the turn silently ends (often with an empty reply). Roles
with `readonly` / `edit` permission therefore get a narrow allow-list of inspection commands.

Matching semantics (verified against the real `agy` CLI, see docs/TEAM_MODE.md section 12):
a rule matches on a whole-word command prefix (`ls` allows `ls -la`, not `lsof`; `git status`
allows `git status -s`, not `git push`), and chained / piped / redirected / substituted
commands (`ls; rm x`, `ls && rm x`, `ls | xargs rm`, `ls $(rm x)`, `ls > f`) are denied.
"""

from __future__ import annotations

from typing import List

# Order is cosmetic. Each entry is a command name or `git <subcommand>`.
SAFE_READONLY_COMMANDS: List[str] = [
    "ls",
    "find",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "rg",
    "tree",
    "pwd",
    "file",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git ls-files",
]

# Commands left off the Claude allow-list: Claude's Bash rules are plain prefix matches that do
# not understand `find -exec ... ;` / `find -delete`, and Claude already has the Glob tool.
CLAUDE_EXCLUDED_COMMANDS = frozenset({"find"})


def agy_allow_rules() -> List[str]:
    """`permissions.allow` entries for `agy --settings` (e.g. `command(git status)`)."""
    return [f"command({c})" for c in SAFE_READONLY_COMMANDS]


def claude_allowed_tools() -> List[str]:
    """`--allowedTools` entries for the `claude` CLI (e.g. `Bash(git status:*)`)."""
    return [
        f"Bash({c}:*)"
        for c in SAFE_READONLY_COMMANDS
        if c not in CLAUDE_EXCLUDED_COMMANDS
    ]
