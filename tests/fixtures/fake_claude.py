#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import List, Optional, Tuple


def _parse_args(argv: List[str]) -> Tuple[Optional[str], str, str, bool, str]:
    resume_id: Optional[str] = None
    model = "sonnet"
    perm_mode = "default"
    has_sys_prompt = False
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
            has_sys_prompt = bool(flag_args[i + 1].strip())
            i += 2
        else:
            i += 1

    return resume_id, model, perm_mode, has_sys_prompt, prompt


def main() -> int:
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    resume_id, model, perm_mode, has_sys_prompt, prompt = _parse_args(sys.argv[1:])
    session_id = resume_id or "fake-claude-uuid-1"
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
    elif "[CHANGES_REQUESTED]" in prompt and not is_resumed and "# Re-Review Request" not in prompt:
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
