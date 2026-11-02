from __future__ import annotations

import asyncio
import json
import re
from typing import Dict, List, Optional, Set

from backend.models import (
    NormalizedEvent,
    NormalizedEventType,
    PermissionLevel,
    ProviderId,
    utc_now_iso,
)
from backend.providers.base import ProviderAdapter, StreamParseState
from backend.providers.safe_commands import agy_allow_rules


PERMISSION_FLAGS: Dict[PermissionLevel, List[str]] = {
    PermissionLevel.READONLY: [
        "--mode",
        "plan",
    ],
    PermissionLevel.EDIT: [
        "--mode",
        "accept-edits",
    ],
    PermissionLevel.FULL: [
        "--mode",
        "accept-edits",
        "--dangerously-skip-permissions",
    ],
}

def agy_settings_json(permission: PermissionLevel) -> Optional[str]:
    """
    Per-session `--settings` JSON for non-full roles: allow-lists read-only shell commands,
    because headless agy auto-denies (and silently ends the turn on) any unlisted command.
    `full` already passes --dangerously-skip-permissions, so it gets no settings.
    """
    if permission == PermissionLevel.FULL:
        return None
    return json.dumps(
        {"permissions": {"allow": agy_allow_rules()}},
        separators=(",", ":"),
    )


FALLBACK_AGY_MODELS: List[Dict[str, str]] = [
    {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
    {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)"},
    {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5 (Medium via AGY)"},
]

_MODEL_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)+$")
_IGNORED_MODEL_WORDS: Set[str] = {
    "available",
    "models",
    "model",
    "default",
    "usage",
    "id",
    "name",
    "list",
    "agy",
    "antigravity",
}


class AntigravityAdapter(ProviderAdapter):
    def __init__(self, agy_bin: str = "agy") -> None:
        self.provider_id: ProviderId = ProviderId.ANTIGRAVITY
        self.display_name: str = "Antigravity (AGY)"
        self.agy_bin: str = agy_bin

    def build_command(
        self,
        prompt: str,
        model: str,
        permission: PermissionLevel,
        system_prompt: Optional[str],
        provider_conversation_id: Optional[str],
        is_first_turn: bool,
    ) -> List[str]:
        perm_level = PermissionLevel(permission)
        perm_flags = PERMISSION_FLAGS[perm_level]
        use_first_turn = bool(is_first_turn or not provider_conversation_id)

        if use_first_turn and system_prompt and system_prompt.strip():
            effective_prompt = f"{system_prompt.strip()}\n\n# Task\n\n{prompt}"
        else:
            effective_prompt = prompt

        cmd: List[str] = [
            self.agy_bin,
            "--output-format",
            "stream-json",
        ]
        cmd.extend(perm_flags)
        settings_json = agy_settings_json(perm_level)
        if settings_json:
            cmd.extend(["--settings", settings_json])
        if model and model.strip():
            cmd.extend(["--model", model])
        if not use_first_turn and provider_conversation_id:
            cmd.extend(["--conversation", provider_conversation_id])
        cmd.extend(["-p", effective_prompt])
        return cmd

    def parse_stream_line(
        self,
        line: str,
        state: StreamParseState,
    ) -> List[NormalizedEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        if not isinstance(obj, dict):
            return []

        events: List[NormalizedEvent] = []

        # 1. Conversation ID capture
        res_obj = obj.get("result")
        res_dict = res_obj if isinstance(res_obj, dict) else {}
        step_update = obj.get("step_update")
        step_dict = step_update if isinstance(step_update, dict) else {}
        conv_id = (
            obj.get("conversation_id")
            or step_dict.get("conversation_id")
            or res_dict.get("conversation_id")
        )
        if conv_id and str(conv_id).strip() and state.provider_conversation_id is None:
            state.provider_conversation_id = str(conv_id).strip()
            events.append(
                NormalizedEvent(
                    seq=state.next_seq(),
                    timestamp=utc_now_iso(),
                    type=NormalizedEventType.SESSION_INIT,
                    provider_conversation_id=state.provider_conversation_id,
                )
            )

        # 2. Step updates ("step_update" in obj)
        if step_dict:
            step_type = step_dict.get("step_type")
            if step_type == "agent_response":
                delta = step_dict.get("text_delta") or ""
                if delta:
                    delta_str = str(delta)
                    state.saw_text_delta = True
                    state.accumulated_text.append(delta_str)
                    events.append(
                        NormalizedEvent(
                            seq=state.next_seq(),
                            timestamp=utc_now_iso(),
                            type=NormalizedEventType.TEXT_DELTA,
                            provider_conversation_id=state.provider_conversation_id,
                            text=delta_str,
                        )
                    )
            elif step_type == "thinking":
                delta = step_dict.get("text_delta") or ""
                if delta:
                    state.saw_thinking_delta = True
                    events.append(
                        NormalizedEvent(
                            seq=state.next_seq(),
                            timestamp=utc_now_iso(),
                            type=NormalizedEventType.THINKING_DELTA,
                            provider_conversation_id=state.provider_conversation_id,
                            text=str(delta),
                        )
                    )
            elif step_type == "tool":
                raw_idx = step_dict.get("step_index")
                step_idx = str(raw_idx) if raw_idx is not None else ""
                tool_name = str(step_dict.get("tool_name") or "tool")
                tool_state = str(step_dict.get("state") or "RUNNING")
                raw_tool_info = step_dict.get("tool_info")
                tool_info = raw_tool_info if isinstance(raw_tool_info, dict) else {}
                raw_params = tool_info.get("parameters")
                params = raw_params if isinstance(raw_params, dict) else {}

                if step_idx not in state.seen_tool_steps:
                    state.seen_tool_steps[step_idx] = "RUNNING"
                    events.append(
                        NormalizedEvent(
                            seq=state.next_seq(),
                            timestamp=utc_now_iso(),
                            type=NormalizedEventType.TOOL_USE,
                            provider_conversation_id=state.provider_conversation_id,
                            tool_call_id=step_idx,
                            tool_name=tool_name,
                            tool_input=params,
                        )
                    )

                if (
                    tool_state in ("DONE", "ERROR", "COMPLETED")
                    and state.seen_tool_steps.get(step_idx) != tool_state
                ):
                    state.seen_tool_steps[step_idx] = tool_state
                    is_err = tool_state == "ERROR"
                    raw_err = tool_info.get("error")
                    err_obj = raw_err if isinstance(raw_err, dict) else {}
                    if is_err:
                        if err_obj.get("message") is not None:
                            out_str = str(err_obj.get("message"))
                        elif isinstance(raw_err, str) and raw_err:
                            out_str = raw_err
                        else:
                            out_str = str(tool_info.get("output") or "ERROR")
                    else:
                        out_str = str(tool_info.get("output") or "OK")

                    events.append(
                        NormalizedEvent(
                            seq=state.next_seq(),
                            timestamp=utc_now_iso(),
                            type=NormalizedEventType.TOOL_RESULT,
                            provider_conversation_id=state.provider_conversation_id,
                            tool_call_id=step_idx,
                            tool_name=tool_name,
                            tool_input=params,
                            tool_output=out_str,
                            is_error=is_err,
                        )
                    )

        # 3. Final Result (obj.get("event") == "result")
        if obj.get("event") == "result":
            status = str(res_dict.get("status") or "ERROR")
            state.final_status = status
            response_text = str(
                res_dict.get("response") or "".join(state.accumulated_text)
            ).strip()
            state.final_response = response_text

            raw_denied = res_dict.get("denied_actions") or []
            denied: List[str] = []
            if isinstance(raw_denied, list):
                for a in raw_denied:
                    if isinstance(a, dict):
                        denied.append(
                            str(a.get("display_name") or a.get("action") or "unknown")
                        )
                    elif a is not None:
                        denied.append(str(a))
            state.denied_actions = denied

            is_err = status != "SUCCESS"
            if is_err:
                raw_res_err = res_dict.get("error")
                if isinstance(raw_res_err, dict) and raw_res_err.get("message"):
                    state.error_message = str(raw_res_err["message"])
                else:
                    state.error_message = str(
                        raw_res_err or "Turn ended without SUCCESS status"
                    )

            events.append(
                NormalizedEvent(
                    seq=state.next_seq(),
                    timestamp=utc_now_iso(),
                    type=NormalizedEventType.TURN_RESULT,
                    provider_conversation_id=state.provider_conversation_id,
                    text=response_text,
                    is_error=is_err,
                    denied_actions=denied,
                    raw=obj,
                )
            )

        return events

    async def list_models(self) -> List[Dict[str, str]]:
        fallback_labels: Dict[str, str] = {
            m["id"]: m["label"] for m in FALLBACK_AGY_MODELS
        }
        discovered_ids: List[str] = []
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.agy_bin,
                "models",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0 and stdout_bytes:
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")
                for line in stdout_text.splitlines():
                    stripped_line = line.strip()
                    if not stripped_line:
                        continue
                    for raw_token in stripped_line.split():
                        token = raw_token.strip(",:;()[]{}\"'*")
                        if (
                            "-" in token
                            and _MODEL_ID_RE.match(token)
                            and token.lower() not in _IGNORED_MODEL_WORDS
                        ):
                            if token not in discovered_ids:
                                discovered_ids.append(token)
        except Exception:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

        models: List[Dict[str, str]] = []
        seen_ids: Set[str] = set()
        for model_id in discovered_ids:
            seen_ids.add(model_id)
            models.append(
                {
                    "id": model_id,
                    "label": fallback_labels.get(model_id, model_id),
                }
            )
        for fallback in FALLBACK_AGY_MODELS:
            if fallback["id"] not in seen_ids:
                seen_ids.add(fallback["id"])
                models.append(dict(fallback))
        return models
