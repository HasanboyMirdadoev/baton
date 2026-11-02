from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterator

from fastapi.testclient import TestClient
import pytest

from backend.config import AppConfig, load_config
from backend.db import Database
from backend.main import create_app
from backend.pipeline_engine import PipelineEngine
from backend.pubsub import EventBus
from backend.session_manager import SessionManager
from backend.team_engine import TeamEngine

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FAKE_CLAUDE_BIN = FIXTURES_DIR / "fake_claude.py"
FAKE_AGY_BIN = FIXTURES_DIR / "fake_agy.py"


@pytest.fixture(scope="session", autouse=True)
def ensure_fake_clis_executable() -> None:
    os.chmod(FAKE_CLAUDE_BIN, 0o755)
    os.chmod(FAKE_AGY_BIN, 0o755)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "home"
    monkeypatch.setenv("AGENT_RELAY_HOME", str(home_dir))
    monkeypatch.setenv("CLAUDE_BIN", str(FAKE_CLAUDE_BIN))
    monkeypatch.setenv("AGY_BIN", str(FAKE_AGY_BIN))
    monkeypatch.setenv("AGENT_RELAY_PORT", "8765")
    monkeypatch.setenv("AGENT_RELAY_TURN_TIMEOUT", "60")
    return home_dir


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def app_config(isolated_env: Path) -> AppConfig:
    return load_config()


@pytest.fixture
def db(app_config: AppConfig) -> Database:
    database = Database(app_config.db_path)
    database.recover_orphaned_states()
    return database


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def session_manager(
    app_config: AppConfig, db: Database, bus: EventBus
) -> SessionManager:
    return SessionManager(app_config, db, bus)


@pytest.fixture
def pipeline_engine(
    db: Database, session_manager: SessionManager, bus: EventBus
) -> PipelineEngine:
    return PipelineEngine(db, session_manager, bus)


@pytest.fixture
def client(app_config: AppConfig) -> Iterator[TestClient]:
    app = create_app(app_config)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def team_engine(
    app_config: AppConfig, db: Database, session_manager: SessionManager, bus: EventBus
) -> TeamEngine:
    return TeamEngine(db, session_manager, bus, app_config)


def git_cmd(repo: Path, *args: str) -> str:
    """Runs git in `repo` with a fixed identity and no signing; returns stdout."""
    res = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Baton Test",
            "-c",
            "user.email=baton-test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


def _cleanup_git_repo(repo: Path) -> None:
    # Remove any linked worktrees before deleting the repository itself.
    if (repo / ".git").exists():
        listing = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
        ).stdout
        main_wt = os.path.realpath(str(repo))
        for line in listing.splitlines():
            if line.startswith("worktree "):
                wt = line[len("worktree ") :].strip()
                if os.path.realpath(wt) != main_wt:
                    subprocess.run(
                        ["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                        capture_output=True,
                    )
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)
    shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Iterator[Path]:
    """Temp git repository on branch `main` with one commit (README.md)."""
    repo = tmp_path / "target_repo"
    repo.mkdir(parents=True, exist_ok=True)
    git_cmd(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# Target repo\n", encoding="utf-8")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "Initial commit")
    yield repo
    _cleanup_git_repo(repo)


@pytest.fixture
def empty_git_repo(tmp_path: Path) -> Iterator[Path]:
    """Temp git repository with no commits."""
    repo = tmp_path / "empty_repo"
    repo.mkdir(parents=True, exist_ok=True)
    git_cmd(repo, "init", "-q", "-b", "main")
    yield repo
    _cleanup_git_repo(repo)


@pytest.fixture
def non_git_dir(tmp_path: Path) -> Path:
    """Plain folder that is not inside any git work tree."""
    folder = tmp_path / "plain_folder"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "file.txt").write_text("hello\n", encoding="utf-8")
    return folder
