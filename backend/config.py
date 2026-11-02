from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Optional, Set

FORBIDDEN_PORTS: Set[int] = {5000, 7000, 5090, 5186}
DEFAULT_PORT: int = 8765
DEFAULT_HOST: str = "127.0.0.1"


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    data_dir: Path
    db_path: Path
    turns_dir: Path
    claude_bin: str
    agy_bin: str
    default_turn_timeout_sec: int = 600


def _resolve_binary(env_var: str, default_name: str) -> str:
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val
    local_path = os.path.expanduser(f"~/.local/bin/{default_name}")
    if os.path.isfile(local_path) and os.access(local_path, os.X_OK):
        return local_path
    which_path = shutil.which(default_name)
    if which_path:
        return which_path
    return default_name


def load_config() -> AppConfig:
    """
    Reads environment variables:
      - AGENT_RELAY_PORT (default 8765; raises ValueError if in FORBIDDEN_PORTS or out of range)
      - AGENT_RELAY_HOME (default ~/.agent-relay)
      - CLAUDE_BIN (default: ~/.local/bin/claude or shutil.which('claude') or 'claude')
      - AGY_BIN (default: ~/.local/bin/agy or shutil.which('agy') or 'agy')
      - AGENT_RELAY_TURN_TIMEOUT (default 600)
    Creates data_dir and turns_dir with mode 0o700 if they do not exist.
    """
    port_raw = os.environ.get("AGENT_RELAY_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_raw)
    except ValueError:
        raise ValueError(f"AGENT_RELAY_PORT must be an integer, got: {port_raw!r}")

    if port in FORBIDDEN_PORTS:
        raise ValueError(f"Port {port} is a forbidden port ({sorted(FORBIDDEN_PORTS)})")
    if not (1 <= port <= 65535):
        raise ValueError(f"Port {port} is out of valid range (1..65535)")

    home_raw = os.environ.get("AGENT_RELAY_HOME", "~/.agent-relay")
    data_dir = Path(os.path.expanduser(home_raw)).resolve()
    turns_dir = data_dir / "turns"
    db_path = data_dir / "state.db"

    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    turns_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(turns_dir, 0o700)

    claude_bin = _resolve_binary("CLAUDE_BIN", "claude")
    agy_bin = _resolve_binary("AGY_BIN", "agy")

    timeout_raw = os.environ.get("AGENT_RELAY_TURN_TIMEOUT", "600")
    try:
        default_turn_timeout_sec = int(timeout_raw)
    except ValueError:
        raise ValueError(f"AGENT_RELAY_TURN_TIMEOUT must be an integer, got: {timeout_raw!r}")
    if default_turn_timeout_sec <= 0:
        raise ValueError(f"AGENT_RELAY_TURN_TIMEOUT must be positive, got: {default_turn_timeout_sec}")

    return AppConfig(
        host=DEFAULT_HOST,
        port=port,
        data_dir=data_dir,
        db_path=db_path,
        turns_dir=turns_dir,
        claude_bin=claude_bin,
        agy_bin=agy_bin,
        default_turn_timeout_sec=default_turn_timeout_sec,
    )


def validate_working_dir(path_str: str) -> str:
    """
    Expands ~ and resolves symlinks via os.path.realpath.
    Raises ValueError if:
      - Path is empty, does not exist, or is not a directory.
      - Path is "/" or starts with a system prefix:
        ("/System", "/etc", "/private/etc", "/var", "/private/var", "/usr", "/bin", "/sbin", "/dev", "/Library")
    Allows /private/tmp and /private/var/folders/ for testing and temp workspaces.
    Returns the canonical absolute path string.
    """
    if not path_str or not path_str.strip():
        raise ValueError("Working directory path cannot be empty")

    real = os.path.realpath(os.path.expanduser(path_str.strip()))
    if not os.path.exists(real) or not os.path.isdir(real):
        raise ValueError(f"Working directory does not exist or is not a directory: {path_str}")

    if real == "/":
        raise ValueError("Root directory '/' is not allowed as a working directory")

    # Whitelist temporary directories before checking blocked prefixes
    if (
        real == "/private/tmp"
        or real.startswith("/private/tmp/")
        or real.startswith("/private/var/folders/")
        or real == "/tmp"
        or real.startswith("/tmp/")
    ):
        return real

    blocked_prefixes = (
        "/System",
        "/etc",
        "/private/etc",
        "/var",
        "/private/var",
        "/usr",
        "/bin",
        "/sbin",
        "/dev",
        "/Library",
    )
    for prefix in blocked_prefixes:
        if real == prefix or real.startswith(prefix + "/"):
            raise ValueError(f"Working directory is inside protected system path: {prefix}")

    return real
