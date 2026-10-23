"""Launcher for the background service, used by the Task Scheduler route (schtasks cannot set a working
directory or pass environment variables, so this script restores both itself).

    pythonw -X utf8 scripts\\run_daemon.py [--env-file <json written by daemon.write_launch_env>] [daemon args...]
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

args = sys.argv[1:]
if len(args) >= 2 and args[0] == "--env-file":
    # VOICECLAUDE_HOME and friends: they must be in place before voiceclaude.paths is imported
    os.environ.update(json.loads(Path(args[1]).read_text(encoding="utf-8")))
    args = args[2:]
os.chdir(ROOT)

from voiceclaude.daemon import main  # noqa: E402

sys.exit(main(args))
