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

import colors
from budget import Budget
from claims import CLAIM_PATTERN, Claim, ClaimRegistry, split_claim_target
from console_control import ConsoleControl
from peer import PeerMessage
from peer_task import run_peer_task
from reply_policy import CollisionInfo, should_reply
from transport import Transport, build_transport


CONFIG_DIR = Path(__file__).resolve().parent / "config"
DATA_DIR = Path(__file__).resolve().parent / "data"
SYSTEM_PROMPT_FILE = CONFIG_DIR / "system_prompt.txt"
DEFAULT_TPM = 20_000
DEFAULT_RPM = 30
DEFAULT_TOTAL = 200_000
DEFAULT_CLAIM_CONTINUATION_GRACE_SECONDS = 1.5
MAX_RECENT_CONTEXT_ENTRIES = 64


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_system_prompt(agent_id: str, display_name: str) -> str:
    template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    return template.replace("{AGENT_ID}", agent_id).replace("{AGENT_DISPLAY_NAME}", display_name)


def _log(store: SessionStore, kind: str, content: str) -> None:
    store.record("system", kind, content)


def _claimed_targets(text: str) -> set[str]:
    targets: set[str] = set()
    for match in CLAIM_PATTERN.finditer(text or ""):
        path, scope = split_claim_target(match.group("path"))
        targets.add(f"{path}#{scope}" if scope else path)
    return targets


def _claim_continuation_message(original: PeerMessage, claim: Claim) -> PeerMessage:
    text = (
        "Continue the active shared-file claim you already posted. "
        f"Active claim target: {claim.target}. "
        f"Original request: {original.text}\n"
        "Use tools now; do not post another CLAIM. "
        "Only report a shared-file change after a successful tool observation for /workspace/shared/."
    )
    return PeerMessage(
        id=f"{original.id}:claim-continuation:{claim.target}",
        sender_id="runtime",
        text=text,
    )


def _context_entry(sender_id: str, text: str, message_id: str | None = None) -> dict[str, str]:
    entry = {
        "sender_id": sender_id,
        "text": text,
    }
    if message_id is not None:
        entry["message_id"] = message_id
    return entry


def run_group_chat(
    *,
    transport: Transport | None = None,
    budget: Budget | None = None,
    console: ConsoleControl | None = None,
    store: SessionStore | None = None,
    stop_event: threading.Event | None = None,
    idle_sleep: float = 0.05,
    claims: ClaimRegistry | None = None,
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

    if claims is None:
        claims = ClaimRegistry()

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
    recent_context: list[dict[str, str]] = []
    claim_grace_seconds = max(
        0.0,
        _env_float("CLAIM_CONTINUATION_GRACE_SECONDS", DEFAULT_CLAIM_CONTINUATION_GRACE_SECONDS),
    )
    _log(store, "session_start", f"agent_id={agent_id} display={display_name} mode={mode}")
    print(
        colors.dim(
            f"[part3] {display_name} (id={agent_id}) listening via {mode}. "
            f"Type :help for console commands."
        ),
        flush=True,
    )

    runpod = mode == "runpod"

    def _hub_echo(arrow: str, sender: str, text: str) -> None:
        snippet = text[:160].replace("\n", " ")
        tag = colors.dim(f"[hub{arrow}]")
        print(f"{colors.ts()} {tag} {colors.agent_label(sender)}: {snippet}", flush=True)

    def _send_answer(answer: str, msg_id: str) -> None:
        transport.send(answer)
        if not runpod:
            _hub_echo("->", display_name, answer)
        recent_context.append(_context_entry(display_name, answer, msg_id))
        if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
            del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
        recent_replies.append((time.time(), msg_id))
        if len(recent_replies) > 64:
            del recent_replies[:-64]
        budget.save()

    def _run_task_for_message(
        message: PeerMessage,
        prior_context: list[dict[str, str]] | None = None,
        collision: CollisionInfo | None = None,
    ) -> str | None:
        try:
            return run_peer_task(
                message,
                store=store,
                budget=budget,
                system_prompt=system_prompt,
                console=console,
                claims=claims,
                agent_id=agent_id,
                recent_context=prior_context,
                absorb_claims=False,
                collision=collision,
            )
        except RuntimeError as exc:
            # Most often: every LLM provider was rate-limited or unreachable.
            # Logging the failure and continuing means the agent stays online
            # and will retry on the next inbound message.
            print(
                colors.paint(
                    f"[llm!] {display_name} failed on msg {message.id}: {exc}",
                    colors.BRIGHT_RED,
                ),
                file=sys.stderr,
                flush=True,
            )
            _log(store, "llm_failure", f"msg_id={message.id} error={exc}")
            return None

    def _remember_inbound(message: PeerMessage) -> list[dict[str, str]]:
        prior_context = list(recent_context)
        recent_context.append(_context_entry(message.sender_id, message.text, message.id))
        if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
            del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
        return prior_context

    def _absorb_inbound_claims(message: PeerMessage) -> None:
        for claim in claims.absorb_text(message.sender_id, message.text):
            _log(
                store,
                "claim_observed",
                (
                    f"claimant={claim.claimant} path={claim.path} "
                    f"scope={claim.scope or ''} target={claim.target}"
                ),
            )

    def _process_message(message: PeerMessage, *, allow_claim_continuation: bool = True) -> None:
        if not runpod:
            _hub_echo("<-", message.sender_id, message.text)

        prior_context = _remember_inbound(message)

        decision = should_reply(
            message, agent_id, display_name, recent_replies, claims=claims
        )
        _log(
            store,
            "reply_decision",
            f"respond={decision.respond} reason={decision.reason} msg_id={message.id} sender={message.sender_id}",
        )
        if not decision.respond:
            _absorb_inbound_claims(message)
            if runpod:
                print(
                    f"{colors.ts()} {colors.dim(f'[skip] {decision.reason}')}",
                    flush=True,
                )
            return

        if decision.delay_seconds > 0:
            time.sleep(decision.delay_seconds)

        answer = _run_task_for_message(message, prior_context, decision.collision)
        if answer is None:
            _absorb_inbound_claims(message)
            return
        _absorb_inbound_claims(message)
        _send_answer(answer, message.id)

        if allow_claim_continuation:
            _continue_claims(message, answer)

    def _continue_claims(original: PeerMessage, answer: str) -> None:
        targets = _claimed_targets(answer)
        if not targets:
            return

        active = [
            claim for claim in claims.active_claims_for(agent_id)
            if claim.target in targets
        ]
        for claim in active:
            deadline = time.time() + claim_grace_seconds
            while time.time() < deadline and not stop_event.is_set():
                remaining = max(0.0, deadline - time.time())
                peer_message = transport.recv(timeout=min(remaining, 0.5))
                if peer_message is None:
                    time.sleep(min(remaining, idle_sleep))
                    continue
                _process_message(peer_message, allow_claim_continuation=False)
                if claims.is_claimed_by_other(claim.target, agent_id) is not None:
                    _log(store, "claim_continuation_skipped", f"conflict on {claim.target}")
                    return
                if claims.own_claim_for_write(claim.path, agent_id) is None:
                    _log(store, "claim_continuation_skipped", f"claim released for {claim.target}")
                    return

            if claims.is_claimed_by_other(claim.target, agent_id) is not None:
                _log(store, "claim_continuation_skipped", f"conflict on {claim.target}")
                return
            if claims.own_claim_for_write(claim.path, agent_id) is None:
                _log(store, "claim_continuation_skipped", f"claim released for {claim.target}")
                return

            continuation = _claim_continuation_message(original, claim)
            _log(store, "claim_continuation", f"target={claim.target} from_msg={original.id}")
            prior_context = list(recent_context)
            recent_context.append(_context_entry(continuation.sender_id, continuation.text, continuation.id))
            if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
                del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
            continuation_answer = _run_task_for_message(continuation, prior_context)
            if continuation_answer is not None:
                _send_answer(continuation_answer, continuation.id)

    try:
        while not stop_event.is_set():
            message = transport.recv(timeout=1.0)
            if message is None:
                time.sleep(idle_sleep)
                continue
            _process_message(message)
    except KeyboardInterrupt:
        print(
            colors.dim("\n[part3] keyboard interrupt — shutting down"),
            file=sys.stderr,
        )
    finally:
        _log(store, "session_end", f"agent_id={agent_id}")
        try:
            transport.close()
        except Exception:
            pass
        console.stop()
        if owns_store:
            store.close()
