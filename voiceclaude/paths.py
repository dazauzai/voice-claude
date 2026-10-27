"""Where voice-claude keeps its own data: ~/.voice-claude/ (never inside the repo)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import testing


def _default_home() -> Path:
    if testing.under_test_runner():  # a unit test must never read or write the user's ~/.voice-claude
        return Path(tempfile.gettempdir()) / f"voice-claude-tests-{os.getpid()}"
    return Path.home() / ".voice-claude"


HOME_DIR = Path(os.environ.get("VOICECLAUDE_HOME") or _default_home())
MANAGER_DIR = HOME_DIR / "manager"  # the manager line's working directory (its own Claude Code memory)
MANAGER_LOG_DIR = MANAGER_DIR / "log"  # plain-text log of every exchange with the manager
SCRATCH_DIR = HOME_DIR / "scratch"  # the "杂项" line's working directory
JOBS_DIR = HOME_DIR / "jobs"  # one log file per detached background job
LINES_DIR = HOME_DIR / "lines"  # per-line mcp-config.json
DAEMON_INFO = HOME_DIR / "daemon.json"
DAEMON_LOG = HOME_DIR / "daemon.log"
LINES_FILE = HOME_DIR / "lines.json"  # project cwd -> the session id the voice app used last
PROJECTS_FILE = HOME_DIR / "projects.toml"  # user-editable aliases
JOBS_FILE = HOME_DIR / "jobs.json"
CONFIG_FILE = HOME_DIR / "config.toml"  # the user's settings (first run: a config.toml next to the program, if any)
AUTH_FILE = HOME_DIR / "auth.json"  # API keys; never inside the repo, readable by this user only
BOARDS_DIR = HOME_DIR / "boards"  # the shared whiteboard: one <key>.json scene per project, and its snapshots (board.py)
RECORDS_DIR = HOME_DIR / "records"  # voice-claude's long-term memory: one <project>.md per project (records.py)
USAGE_FILE = HOME_DIR / "usage.json"  # tokens per day and provider, and the latest quota snapshot
MANAGER_PROMPT_FILE = HOME_DIR / "manager-prompt.md"  # your own manager prompt, replacing the built-in one
LAUNCH_ENV_FILE = HOME_DIR / "launch-env.json"  # VOICECLAUDE_* for the Task Scheduler route (no secrets)
TASK_XML_FILE = HOME_DIR / "daemon-task.xml"  # the scheduled task's definition (no time limit, no battery rules)
GATEWAY_CONFIG = HOME_DIR / "gateway.yaml"  # the LiteLLM translation gateway's config (names no key)
GATEWAY_LOG = HOME_DIR / "gateway.log"
REPO_DIR = Path(__file__).resolve().parent.parent
REPO_CONFIG = REPO_DIR / "config.toml"
EXAMPLE_CONFIG = REPO_DIR / "config.example.toml"
CLAUDE_PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"


def ensure() -> Path:
    for d in (HOME_DIR, MANAGER_DIR, MANAGER_LOG_DIR, SCRATCH_DIR, JOBS_DIR, LINES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return HOME_DIR
