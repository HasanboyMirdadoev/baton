#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import List, Optional, Tuple


def _parse_args(argv: List[str]) -> Tuple[Optional[str], str, str, str]:
    conv_id: Optional[str] = None
    model = "gemini-4-argon-high"
    mode = "plan"
    prompt = ""

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--conversation" and i + 1 < len(argv):
            conv_id = argv[i + 1]
            i += 2
        elif arg == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
        elif arg == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
        elif arg == "-p" and i + 1 < len(argv):
            prompt = argv[i + 1]
            i += 2
        else:
            i += 1

    return conv_id, model, mode, prompt


def main() -> int:
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    argv = sys.argv[1:]
    if argv == ["models"]:
        print("Available models:")
        print("  gemini-4-argon-high")
        print("  gemini-3.8-flash-medium")
        print("  claude-sonnet-5-5-medium")
        return 0

    conv_arg, model, mode, prompt = _parse_args(argv)
    conv_id = conv_arg or "fake-agy-conv-1"
    is_resumed = conv_arg is not None

    # 1. Emit init event and initial thinking step_update (matching real agy-turn1.jsonl)
    print(
        json.dumps(
            {
                "event": "init",
                "conversation_id": conv_id,
                "init": {
                    "model": model,
                    "cwd": os.getcwd(),
                    "tools": ["read_file", "run_command", "edit_file", "write_file"],
                    "permission_mode": mode,
                },
            }
        ),
        flush=True,
    )
    print(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": 0,
                    "state": "DONE",
                    "step_type": "user_input",
                },
            }
        ),
        flush=True,
    )
    print(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "thinking",
                    "text_delta": "Thinking about task...",
                },
            }
        ),
        flush=True,
    )

    # Handle [SLOW] trigger
    if "[SLOW]" in prompt:
        time.sleep(10)

    # Handle [FAIL] trigger
    if "[FAIL]" in prompt:
        sys.stderr.write("Fatal CLI error occurred\n")
        sys.stderr.flush()
        print(
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": conv_id,
                        "status": "ERROR",
                        "error": "Fatal CLI error occurred",
                    },
                }
            ),
            flush=True,
        )
        return 1

    # 2. Emit tool ACTIVE and DONE step updates
    tool_output_str = (
        "X" * 100_000 if "[LARGE_LINE]" in prompt else "File read successfully"
    )
    print(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": 2,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "read_file",
                    "tool_info": {
                        "name": "read_file",
                        "parameters": {
                            "EndLine": 5,
                            "Path": "README.md",
                            "StartLine": 1,
                        },
                    },
                },
            }
        ),
        flush=True,
    )
    print(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": 2,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "read_file",
                    "duration_seconds": 0.034,
                    "tool_info": {
                        "name": "read_file",
                        "parameters": {
                            "EndLine": 5,
                            "Path": "README.md",
                            "StartLine": 1,
                        },
                        "output": tool_output_str,
                    },
                },
            }
        ),
        flush=True,
    )

    # 3. Determine response text based on prompt triggers
    summary_header = (
        f"[agy model={model} mode={mode} resumed={is_resumed} conv={conv_id} cwd={os.path.basename(os.getcwd())}]"
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

    # 4. Emit agent_response delta and final result
    print(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": 3,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": response_text,
                },
            }
        ),
        flush=True,
    )
    print(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "SUCCESS",
                    "response": response_text,
                    "denied_actions": [],
                },
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
