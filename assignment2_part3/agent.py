"""Part 3 entry point.

Sets agent identity + workspace-namespacing env vars, then hands control to
the group-chat orchestrator. No other Part 3 module should set env vars at
import time.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


def _bootstrap_env() -> None:
    """Pin AGENT_ID/AGENT_WORKSPACE before importing Part 2's tools."""

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

    agent_id = os.environ.setdefault("AGENT_ID", "local")
    os.environ.setdefault("AGENT_DISPLAY_NAME", f"{agent_id}-swe")

    if not os.environ.get("AGENT_WORKSPACE"):
        base = Path(__file__).resolve().parent / "workspace" / agent_id
        base.mkdir(parents=True, exist_ok=True)
        os.environ["AGENT_WORKSPACE"] = str(base)

    if not os.environ.get("SHARED_WORKSPACE"):
        shared = Path(__file__).resolve().parent / "workspace" / "shared"
        shared.mkdir(parents=True, exist_ok=True)
        os.environ["SHARED_WORKSPACE"] = str(shared)

    data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(exist_ok=True)
    os.environ.setdefault("AGENT_SESSION_DB", str(data_dir / "session_history.sqlite3"))


def main() -> None:
    _bootstrap_env()

    # Imported after env bootstrap so Part 2's tools see the namespaced workspace.
    from group_chat import run_group_chat

    run_group_chat()


if __name__ == "__main__":
    main()
