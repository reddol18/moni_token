"""Default locations. Overridable by env vars (used by tests) or CLI flags."""
import os
from pathlib import Path


def claude_projects_dir() -> Path:
    env = os.environ.get("MONI_TOKEN_PROJECTS")
    if env:
        return Path(env)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def data_dir() -> Path:
    env = os.environ.get("MONI_TOKEN_HOME")
    return Path(env) if env else Path.home() / ".moni_token"


def db_path() -> Path:
    return data_dir() / "usage.db"
