"""Part 3 main loop.

Tiny by design: every concern lives in its own module. This file only:

  1. Reads env (AGENT_ID, AGENT_DISPLAY_NAME, AGENT_MODE, budget limits).
  2. Builds Budget, Transport, ConsoleControl, SessionStore.
  3. Loads `config/system_prompt.txt` and templates the identity in.
  4. Loops: recv → should_reply → run_peer_task → transport.send.

Local console only handles operator commands (`:budget`, `:limit`,
`:pause`, `:resume`, `:approve`, `:deny`, `:stop`). It never carries the
agent's conversation — that goes through the transport only (P3.4).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import part2_bridge  # noqa: F401 — sys.path side effect

from thread_safe_store import ThreadSafeSessionStore as SessionStore

from budget import Budget
from console_control import ConsoleControl
from peer import PeerMessage
from peer_task import run_peer_task
from reply_policy import should_reply
from transport import Transport, build_transport


CONFIG_DIR = Path(__file__).resolve().parent / "config"
DATA_DIR = Path(__file__).resolve().parent / "data"
SYSTEM_PROMPT_FILE = CONFIG_DIR / "system_prompt.txt"
DEFAULT_TPM = 20_000
DEFAULT_RPM = 30
DEFAULT_TOTAL = 200_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_system_prompt(agent_id: str, display_name: str) -> str:
    template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    return template.replace("{AGENT_ID}", agent_id).replace("{AGENT_DISPLAY_NAME}", display_name)


def _log(store: SessionStore, kind: str, content: str) -> None:
    store.record("system", kind, content)


def run_group_chat(
    *,
    transport: Transport | None = None,
    budget: Budget | None = None,
    console: ConsoleControl | None = None,
    store: SessionStore | None = None,
    stop_event: threading.Event | None = None,
    idle_sleep: float = 0.05,
) -> None:
    """Main loop. Arguments are injection points for the tests."""

    DATA_DIR.mkdir(exist_ok=True)
    agent_id = os.environ.get("AGENT_ID", "local")
    display_name = os.environ.get("AGENT_DISPLAY_NAME", f"{agent_id}-swe")
    mode = os.environ.get("AGENT_MODE", "stub").lower()

    owns_store = store is None
    if store is None:
        store = SessionStore(os.environ.get("AGENT_SESSION_DB", str(DATA_DIR / "session_history.sqlite3")))

    if budget is None:
        budget = Budget.load(
            DATA_DIR / f"budget_{agent_id}.json",
            tokens_per_minute=_env_int("AGENT_TPM_LIMIT", DEFAULT_TPM),
            requests_per_minute=_env_int("AGENT_RPM_LIMIT", DEFAULT_RPM),
            lifetime_tokens=_env_int("AGENT_TOTAL_TOKEN_LIMIT", DEFAULT_TOTAL),
        )

    if stop_event is None:
        stop_event = threading.Event()

    if transport is None:
        transport = build_transport(mode, agent_id, DATA_DIR)

    if console is None:
        console = ConsoleControl(
            budget=budget,
            stop_event=stop_event,
            send_fn=transport.send,
        )
        console.start()

    system_prompt = load_system_prompt(agent_id, display_name)
    recent_replies: list[tuple[float, str]] = []
    _log(store, "session_start", f"agent_id={agent_id} display={display_name} mode={mode}")
    print(
        f"[part3] {display_name} (id={agent_id}) listening via {mode}. "
        f"Type :help for console commands.",
        flush=True,
    )

    runpod = mode == "runpod"

    def _hub_echo(arrow: str, text: str) -> None:
        snippet = text[:160].replace("\n", " ")
        print(f"[hub{arrow}] {snippet}", flush=True)

    try:
        while not stop_event.is_set():
            message = transport.recv(timeout=1.0)
            if message is None:
                time.sleep(idle_sleep)
                continue

            if not runpod:
                _hub_echo("<-", f"{message.sender_id}: {message.text}")

            decision = should_reply(message, agent_id, display_name, recent_replies)
            _log(
                store,
                "reply_decision",
                f"respond={decision.respond} reason={decision.reason} msg_id={message.id} sender={message.sender_id}",
            )
            if not decision.respond:
                continue

            if decision.delay_seconds > 0:
                time.sleep(decision.delay_seconds)

            answer = run_peer_task(
                message,
                store=store,
                budget=budget,
                system_prompt=system_prompt,
                console=console,
            )
            transport.send(answer)
            if not runpod:
                _hub_echo("->", answer)
            recent_replies.append((time.time(), message.id))
            if len(recent_replies) > 64:
                recent_replies = recent_replies[-64:]
            budget.save()
    except KeyboardInterrupt:
        print("\n[part3] keyboard interrupt — shutting down", file=sys.stderr)
    finally:
        _log(store, "session_end", f"agent_id={agent_id}")
        try:
            transport.close()
        except Exception:
            pass
        console.stop()
        if owns_store:
            store.close()
