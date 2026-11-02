from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from backend.models import NormalizedEvent, PermissionLevel, ProviderId


class StreamParseState:
    """Mutable per-turn parser state held while reading a CLI stdout stream."""

    def __init__(self) -> None:
        self.seq: int = 0
        self.provider_conversation_id: Optional[str] = None
        self.accumulated_text: List[str] = []
        self.saw_text_delta: bool = False
        self.saw_thinking_delta: bool = False
        self.seen_tool_steps: Dict[str, str] = {}  # step_key -> last state
        self.final_status: Optional[str] = None    # "SUCCESS" | "ERROR"
        self.final_response: Optional[str] = None
        self.error_message: Optional[str] = None
        self.denied_actions: List[str] = []

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class ProviderAdapter(ABC):
    provider_id: ProviderId
    display_name: str

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model: str,
        permission: PermissionLevel,
        system_prompt: Optional[str],
        provider_conversation_id: Optional[str],
        is_first_turn: bool,
    ) -> List[str]:
        """Returns the exact argv list for asyncio.create_subprocess_exec (never shell=True)."""

    @abstractmethod
    def parse_stream_line(
        self,
        line: str,
        state: StreamParseState,
    ) -> List[NormalizedEvent]:
        """
        Parses a single NDJSON line from the CLI stdout and updates `state`.
        Returns zero or more NormalizedEvent objects.
        Malformed non-JSON lines are ignored safely.
        """

    @abstractmethod
    async def list_models(self) -> List[Dict[str, str]]:
        """Returns [{"id": "...", "label": "..."}, ...] available for this provider."""
