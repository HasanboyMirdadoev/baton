from __future__ import annotations

from typing import Any, Dict, List

from backend.config import AppConfig
from backend.models import ProviderId
from backend.providers.antigravity import AntigravityAdapter
from backend.providers.base import ProviderAdapter, StreamParseState
from backend.providers.claude import ClaudeAdapter

__all__ = [
    "ProviderAdapter",
    "StreamParseState",
    "ClaudeAdapter",
    "AntigravityAdapter",
    "get_adapter",
    "list_providers_metadata",
]


def get_adapter(provider: ProviderId, config: AppConfig) -> ProviderAdapter:
    """Returns ClaudeAdapter(config.claude_bin) or AntigravityAdapter(config.agy_bin)."""
    provider_enum = ProviderId(provider)
    if provider_enum == ProviderId.CLAUDE:
        return ClaudeAdapter(config.claude_bin)
    if provider_enum == ProviderId.ANTIGRAVITY:
        return AntigravityAdapter(config.agy_bin)
    raise ValueError(f"Unsupported provider: {provider}")


async def list_providers_metadata(config: AppConfig) -> List[Dict[str, Any]]:
    """
    Returns the `providers` list for `GET /api/meta`:
    [
        {"id": "claude", "name": "Claude Code", "models": await claude_adapter.list_models()},
        {"id": "antigravity", "name": "Antigravity (AGY)", "models": await agy_adapter.list_models()},
    ]
    """
    claude_adapter = ClaudeAdapter(config.claude_bin)
    agy_adapter = AntigravityAdapter(config.agy_bin)
    return [
        {
            "id": claude_adapter.provider_id.value,
            "name": claude_adapter.display_name,
            "models": await claude_adapter.list_models(),
        },
        {
            "id": agy_adapter.provider_id.value,
            "name": agy_adapter.display_name,
            "models": await agy_adapter.list_models(),
        },
    ]
