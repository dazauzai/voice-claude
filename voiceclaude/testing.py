"""Are we inside a unit-test runner? Then nothing real may start and nothing of the user's may be touched.

Why: a unit test once started a real Claude Code on a real subscription, and another deleted a live daemon.json. So the guard is in the code, not in each test's discipline: under `python -m unittest`
or pytest (or with VOICECLAUDE_TESTING=1) ClaudeSession.start, the access probe, the gateway and the daemon
spawners refuse, and paths.HOME_DIR defaults to a temporary directory instead of ~/.voice-claude. Scripts that
really must start things (selftest, verify_providers, the daemon itself) run as their own __main__ and set
VOICECLAUDE_HOME explicitly, so they are not affected."""
from __future__ import annotations

import os
import sys

REFUSAL = "单元测试里不起真的进程（voiceclaude.testing：在 unittest / pytest 下，或 VOICECLAUDE_TESTING=1）"


def under_test_runner() -> bool:
    if os.environ.get("VOICECLAUDE_TESTING") == "1":
        return True
    main = sys.modules.get("__main__")
    name = str(getattr(getattr(main, "__spec__", None), "name", "") or "")
    return name.startswith(("unittest", "pytest")) or "pytest" in sys.modules
