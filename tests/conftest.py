from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from fastapi.testclient import TestClient
import pytest

from backend.config import AppConfig, load_config
from backend.db import Database
from backend.main import create_app
from backend.pipeline_engine import PipelineEngine
from backend.pubsub import EventBus
from backend.session_manager import SessionManager

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
