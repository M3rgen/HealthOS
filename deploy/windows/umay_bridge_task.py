"""Run scripts/start_codex_bridge.py for the Windows sign-in task without touching .env.

The Windows counterpart of `start_codex_bridge.py --foreground` (which is POSIX only): the
bridge is started detached (or a healthy one is reused) exactly as by hand, but `.env` is never
rewritten. install.ps1 writes CODEX_BRIDGE_TOKEN and CODEX_BRIDGE_URL once, keeps .env readable
only by you, and deploy/windows/start-umay-bridge.ps1 passes the token in the environment, so
the bridge and the app always use the token that is already in .env.

Started by deploy/windows/start-umay-bridge.ps1; not meant to be run on its own.
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "start_codex_bridge.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("start_codex_bridge", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def keep_env(_token):
    """Replaces update_env: the sign-in task never rewrites .env."""


def main():
    if not os.environ.get("CODEX_BRIDGE_TOKEN", "").strip():
        raise SystemExit(
            "CODEX_BRIDGE_TOKEN is empty: run install.ps1 -Umay yes once "
            "(it writes the token to .env)."
        )
    launcher = load_launcher()
    launcher.update_env = keep_env
    sys.argv = [str(LAUNCHER)]
    return launcher.main()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
