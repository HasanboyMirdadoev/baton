from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from backend.models import (
    NormalizedEvent,
    NormalizedEventType,
    PermissionLevel,
    ProviderId,
    utc_now_iso,
)
from backend.providers.base import ProviderAdapter, StreamParseState
from backend.providers.safe_commands import claude_allowed_tools


PERMISSION_FLAGS: Dict[PermissionLevel, List[str]] = {
    PermissionLevel.READONLY: [
        "--permission-mode",
        "plan",
        "--permission-prompts",
        "none",
        "--disallowedTools",
        "Edit,Write,NotebookEdit",
        "--allowedTools",
        ",".join(claude_allowed_tools()),
    ],
    PermissionLevel.EDIT: [
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--allowedTools",
        ",".join(claude_allowed_tools()),
    ],
    PermissionLevel.FULL: [
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
    ],
}

CLAUDE_MODELS: List[Dict[str, str]] = [
    {"id": "sonnet", "label": "Claude Sonnet (latest alias)"},
    {"id": "opus", "label": "Claude Opus (latest alias)"},
    {"id": "haiku", "label": "Claude Haiku (latest alias)"},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5"},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1"},
]


def _extract_tool_result_content(raw_content: Any) -> str:
    if raw_content is None:
        return ""
    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, list):
        parts: List[str] = []
        for item in raw_content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if text is None:
                    text = item.get("content")
                if text is not None:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    return str(raw_content)


class ClaudeAdapter(ProviderAdapter):
    def __init__(self, claude_bin: str = "claude") -> None:
        self.provider_id: ProviderId = ProviderId.CLAUDE
        self.display_name: str = "Claude Code"
        self.claude_bin: str = claude_bin

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

        cmd: List[str] = [
            self.claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if not use_first_turn and provider_conversation_id:
            cmd.extend(["--resume", provider_conversation_id])
        if model:
            cmd.extend(["--model", model])
        cmd.extend(perm_flags)
        if use_first_turn and system_prompt and system_prompt.strip():
            cmd.extend(["--append-system-prompt", system_prompt])
        cmd.extend(["--", prompt])
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
        session_id = obj.get("session_id")
        if isinstance(session_id, str) and session_id.strip() and state.provider_conversation_id is None:
            state.provider_conversation_id = session_id.strip()

        msg_type = obj.get("type")

        # 2. System init
        if msg_type == "system" and obj.get("subtype") == "init":
            events.append(
                NormalizedEvent(
                    seq=state.next_seq(),
                    timestamp=utc_now_iso(),
                    type=NormalizedEventType.SESSION_INIT,
                    provider_conversation_id=state.provider_conversation_id,
                    raw=obj,
                )
            )
            return events

        # 3. Stream event deltas
        if msg_type == "stream_event":
            inner = obj.get("event")
            if isinstance(inner, dict) and inner.get("type") == "content_block_delta":
                delta = inner.get("delta")
                if isinstance(delta, dict):
                    delta_type = delta.get("type")
                    if delta_type == "text_delta" and delta.get("text"):
                        text_val = str(delta["text"])
                        state.saw_text_delta = True
                        state.accumulated_text.append(text_val)
                        events.append(
                            NormalizedEvent(
                                seq=state.next_seq(),
                                timestamp=utc_now_iso(),
                                type=NormalizedEventType.TEXT_DELTA,
                                provider_conversation_id=state.provider_conversation_id,
                                text=text_val,
                            )
                        )
                    elif delta_type == "thinking_delta" and delta.get("thinking"):
                        thinking_val = str(delta["thinking"])
                        state.saw_thinking_delta = True
                        events.append(
                            NormalizedEvent(
                                seq=state.next_seq(),
                                timestamp=utc_now_iso(),
                                type=NormalizedEventType.THINKING_DELTA,
                                provider_conversation_id=state.provider_conversation_id,
                                text=thinking_val,
                            )
                        )
            return events

        # 4. Assistant message blocks
        if msg_type == "assistant":
            message = obj.get("message")
            content_blocks = message.get("content") if isinstance(message, dict) else None
            if obj.get("is_api_error_message") or obj.get("error"):
                err_texts: List[str] = []
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "text"
                            and block.get("text")
                        ):
                            err_texts.append(str(block["text"]))
                state.error_message = (
                    "\n".join(err_texts)
                    if err_texts
                    else str(obj.get("error") or "")
                )
                return events
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text" and block.get("text") and not state.saw_text_delta:
                        text_val = str(block["text"])
                        state.accumulated_text.append(text_val)
                        events.append(
                            NormalizedEvent(
                                seq=state.next_seq(),
                                timestamp=utc_now_iso(),
                                type=NormalizedEventType.TEXT_DELTA,
                                provider_conversation_id=state.provider_conversation_id,
                                text=text_val,
                            )
                        )
                    elif btype == "thinking" and block.get("thinking") and not state.saw_thinking_delta:
                        thinking_val = str(block["thinking"])
                        events.append(
                            NormalizedEvent(
                                seq=state.next_seq(),
                                timestamp=utc_now_iso(),
                                type=NormalizedEventType.THINKING_DELTA,
                                provider_conversation_id=state.provider_conversation_id,
                                text=thinking_val,
                            )
                        )
                    elif btype == "tool_use":
                        tool_id = str(block.get("id") or state.next_seq())
                        if tool_id not in state.seen_tool_steps:
                            state.seen_tool_steps[tool_id] = "RUNNING"
                            raw_input = block.get("input")
                            tool_input = raw_input if isinstance(raw_input, dict) else {}
                            events.append(
                                NormalizedEvent(
                                    seq=state.next_seq(),
                                    timestamp=utc_now_iso(),
                                    type=NormalizedEventType.TOOL_USE,
                                    provider_conversation_id=state.provider_conversation_id,
                                    tool_call_id=tool_id,
                                    tool_name=block.get("name"),
                                    tool_input=tool_input,
                                )
                            )
            return events

        # 5. User message blocks (tool results)
        if msg_type == "user":
            message = obj.get("message")
            content_blocks = message.get("content") if isinstance(message, dict) else None
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        content_str = _extract_tool_result_content(block.get("content"))
                        events.append(
                            NormalizedEvent(
                                seq=state.next_seq(),
                                timestamp=utc_now_iso(),
                                type=NormalizedEventType.TOOL_RESULT,
                                provider_conversation_id=state.provider_conversation_id,
                                tool_call_id=str(block.get("tool_use_id") or ""),
                                tool_output=content_str,
                                is_error=bool(block.get("is_error")),
                            )
                        )
            return events

        # 6. Result event
        if msg_type == "result":
            is_err = bool(obj.get("is_error")) or obj.get("subtype") != "success"
            state.final_status = "ERROR" if is_err else "SUCCESS"
            if is_err:
                state.error_message = str(
                    obj.get("error")
                    or obj.get("result")
                    or state.error_message
                    or "Claude CLI reported an error"
                )
                result_text = "".join(state.accumulated_text)
            else:
                result_text = str(obj.get("result") or "".join(state.accumulated_text))
            state.final_response = result_text

            raw_denied = obj.get("denied_actions") or obj.get("permission_denials") or []
            if isinstance(raw_denied, list) and raw_denied:
                denied: List[str] = []
                for item in raw_denied:
                    if isinstance(item, dict):
                        denied.append(
                            str(
                                item.get("display_name")
                                or item.get("tool_name")
                                or item.get("action")
                                or "unknown"
                            )
                        )
                    elif item is not None:
                        denied.append(str(item))
                state.denied_actions = denied

            events.append(
                NormalizedEvent(
                    seq=state.next_seq(),
                    timestamp=utc_now_iso(),
                    type=NormalizedEventType.TURN_RESULT,
                    provider_conversation_id=state.provider_conversation_id,
                    text=result_text,
                    is_error=is_err,
                    denied_actions=list(state.denied_actions),
                    raw=obj,
                )
            )
            return events

        return events

    async def list_models(self) -> List[Dict[str, str]]:
        return [dict(m) for m in CLAUDE_MODELS]
