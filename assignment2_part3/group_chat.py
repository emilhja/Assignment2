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
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import part2_bridge  # noqa: F401 — sys.path side effect

from thread_safe_store import ThreadSafeSessionStore as SessionStore

import colors
from budget import Budget, format_usage_summary
from claims import CLAIM_PATTERN, DEFER_PATTERN, RELEASE_PATTERN, Claim, ClaimRegistry, split_claim_target
from code_share import MAX_PROJECTS, most_recent_project_dir, next_project_dir, process_shared_code
from coordination import (
    assignment_guidance,
    fix_blockers_guidance,
    followup_assignment_guidance,
    handoff_guidance,
    private_workspace_guidance,
    status_request_guidance,
)
from console_control import ConsoleControl
from peer import PeerMessage
from peer_task import run_peer_task
from reply_policy import CollisionInfo, should_reply
from task_status import TaskStatus, parse_task_status
from transport import Transport, build_transport


CONFIG_DIR = Path(__file__).resolve().parent / "config"
DATA_DIR = Path(__file__).resolve().parent / "data"
SYSTEM_PROMPT_FILE = CONFIG_DIR / "system_prompt.txt"
DEFAULT_TPM = 100_000
DEFAULT_RPM = 30
DEFAULT_TOTAL = 2_000_000
DEFAULT_CLAIM_CONTINUATION_GRACE_SECONDS = 1.5
DEFAULT_PENDING_FOLLOWUP_SECONDS = 120.0
MAX_RECENT_CONTEXT_ENTRIES = 64

CONFIRMATION_REPLIES = {
    "yes",
    "y",
    "yep",
    "yeah",
    "ok",
    "okay",
    "sure",
    "yes please",
    "please do",
    "go ahead",
    "do it",
    "sounds good",
}
REJECTION_REPLIES = {
    "no",
    "nope",
    "not now",
    "cancel",
    "don't",
    "do not",
}
CONFIRMATION_REQUEST_PATTERN = re.compile(
    r"(?i)\b("
    r"would you like me to|do you want me to|should i|shall i|"
    r"want me to|can i proceed|should we proceed"
    r")\b"
)
TEST_REQUEST_PATTERN = re.compile(r"(?i)\b(?:pytest|tests?|tester)\b")


@dataclass
class PendingFollowup:
    timestamp: float
    message_id: str
    text: str


@dataclass
class _ProjectState:
    active: Path | None = None


def _remote_workspace_guidance(agent_id: str, project_name: str) -> str:
    return (
        f"Remote hub mode (no shared filesystem). Your active project is "
        f"/workspace/{agent_id}/{project_name}/. Write every file you create or "
        f"edit under /workspace/{agent_id}/{project_name}/<filename>. Never "
        "say you wrote to /sandbox or /workspace/shared in remote hub mode; "
        "those are wrong here. Do NOT write to /workspace/shared/ on this hub "
        "— peers cannot see it and there is no point in claiming it. When you "
        "share code in chat, put `# file: <filename>` as the first line inside "
        "each Python/Markdown code block so peers save it under the intended "
        "name. If the runtime says peer code was saved, reference the exact "
        "saved /workspace/<agent>/<project>/<filename> path it reports, not a "
        "filename you expected. Do NOT emit CLAIM, RELEASE, or DEFER protocol "
        "lines on this hub; the system prompt's P3.9 protocol applies only to "
        "the local docker-compose demo. Use visible task-status phrases instead: "
        "for a direct assignment, first reply `Bekräftat, jag tar: <short task>` "
        "or `Confirmed, I'll take: <short task>`; for a self-selected task, use "
        "`Jag tar mig an: <short task>` or `I'm taking on: <short task>`. When "
        "the work is actually complete, use `Klar med: <task>. Filer: ... "
        "Tester: ...` or `Done with: <task>. Files: ... Tests: ...`. The "
        "operator can switch active project "
        "at any time with :project new or :project use N. "
        "Truthful-completion rule: never say 'Done', 'Implemented', 'Created', "
        "'Wrote', or 'Saved' unless a successful create_file/append_text/"
        "edit_section/replace_text/rename_file tool observation for the target "
        "file was returned in this round. Saying you 'will' or 'need to' do the "
        "work is not enough — the runtime will reprompt you until you make the "
        "real tool call. "
        "Show-your-work rule: after a successful write, your final answer MUST "
        "(1) name the exact path you wrote (full /workspace/<agent>/<project>/"
        "<filename>) and (2) paste the file contents in a fenced code block "
        "whose first line inside the fence is `# file: <filename>`. This is how "
        "peers see and reuse what you produced on a no-shared-filesystem hub. "
        "When you have run pytest, also include the pytest result line so "
        "everyone knows what was verified."
    )


def _build_project_handler(
    project_state: _ProjectState,
    agent_workspace: Path,
):
    def handler(action: str, rest: list[str]) -> str:
        action = (action or "info").lower()
        if action == "info":
            if project_state.active is None:
                return "active=<none>"
            return f"active={project_state.active.name}"
        if action == "new":
            nxt = next_project_dir(agent_workspace)
            if nxt is None:
                return f"[project error] cap reached ({MAX_PROJECTS} projects)"
            project_state.active = nxt
            return f"active={nxt.name} (new)"
        if action == "use":
            if not rest:
                return "usage: :project use <N>"
            try:
                n = int(rest[0])
            except ValueError:
                return f"[project error] N must be an integer, got {rest[0]!r}"
            if n < 1 or n > MAX_PROJECTS:
                return f"[project error] N must be 1..{MAX_PROJECTS}, got {n}"
            target = agent_workspace / f"project{n}"
            if not target.is_dir():
                return f"[project error] {target.name} does not exist"
            project_state.active = target
            return f"active={target.name}"
        if action == "list":
            entries = []
            for entry in sorted(
                agent_workspace.iterdir() if agent_workspace.exists() else [],
                key=lambda p: p.name,
            ):
                m = re.fullmatch(r"project(\d+)", entry.name)
                if not entry.is_dir() or not m:
                    continue
                marker = "*" if (
                    project_state.active is not None
                    and entry.name == project_state.active.name
                ) else " "
                entries.append(f"  {marker} {entry.name}")
            if not entries:
                return "[no projects]"
            return "\n".join(entries)
        return "usage: :project [info|new|use <N>|list]"

    return handler


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


def _task_status_continuation_message(original: PeerMessage, status: TaskStatus) -> PeerMessage:
    verification = ""
    if TEST_REQUEST_PATTERN.search(original.text or ""):
        verification = (
            " The original request asked for tests or pytest, so call run_tests "
            "before reporting done, unless a concrete blocker prevents it."
        )
    text = (
        "Continue the accepted task now. "
        f"Accepted task: {status.task}. "
        f"Original request: {original.text}\n"
        "Use tools now; do not only describe the work. Do not answer with a "
        "future-tense plan. Only report `Klar med:` or `Done with:` after "
        "successful tool observations for the actual work."
        f"{verification}"
    )
    return PeerMessage(
        id=f"{original.id}:task-status-continuation:{status.kind}",
        sender_id="runtime",
        text=text,
        addressed_to=(original.sender_id,),
    )


def _is_claim_continuation_message(message: PeerMessage) -> bool:
    return message.sender_id == "runtime" and ":claim-continuation:" in message.id


def _context_entry(sender_id: str, text: str, message_id: str | None = None) -> dict[str, str]:
    entry = {
        "sender_id": sender_id,
        "text": text,
    }
    if message_id is not None:
        entry["message_id"] = message_id
    return entry


def _normalized_short_reply(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return normalized.strip(" .!?")


def _followup_reply_kind(text: str) -> str | None:
    normalized = _normalized_short_reply(text)
    if not normalized or len(normalized.split()) > 3:
        return None
    if normalized in CONFIRMATION_REPLIES:
        return "confirmation"
    if normalized in REJECTION_REPLIES:
        return "rejection"
    return None


def _looks_like_operator_sender(sender_id: str, agent_id: str) -> bool:
    sender = (sender_id or "").strip().lower()
    return bool(sender) and sender != agent_id.lower() and not sender.endswith("-swe")


def _answer_invites_followup(answer: str) -> bool:
    text = (answer or "").strip()
    return bool(text) and (text.endswith("?") or CONFIRMATION_REQUEST_PATTERN.search(text) is not None)


def _stale_claim_guidance(active_claims: list[Claim]) -> str | None:
    """Build a nudge for active claims that weren't satisfied on a prior turn.

    The runtime's `_continue_claims` only fires for CLAIMs in the just-sent
    reply, so an agent that posted CLAIM but then deferred or said "I will…"
    has no built-in reminder. This guidance string is appended to
    `runtime_guidance` before each task run so the model sees the open
    obligation alongside the new inbound message.
    """

    if not active_claims:
        return None
    targets = ", ".join(claim.target for claim in active_claims)
    return (
        "You have unsatisfied active CLAIM(s) from a previous turn: "
        f"{targets}. On this turn either complete the write with "
        "create_file/append_text/edit_section/rename_file/replace_text for each, or post "
        "`RELEASE <target>` to give it up. Do not re-post the same CLAIM."
    )


def _released_without_write_guidance(released_claims: list[Claim]) -> str | None:
    """Build a nudge for claims this agent RELEASEd before completing the write.

    `claims.recently_released_unsatisfied_for` returns claims that were
    released without ever being marked satisfied by a shared write. This
    surfaces on the next inbound turn so the model knows it abandoned work
    and should re-claim + write, rather than silently dropping the task.
    """

    if not released_claims:
        return None
    targets = ", ".join(claim.target for claim in released_claims)
    return (
        "You previously CLAIMed and then RELEASEd without a successful "
        f"write: {targets}. The work was abandoned. If you still intend "
        "to do it, post a fresh CLAIM for the target and then complete the "
        "write on the runtime continuation. Otherwise explain in chat why "
        "you are dropping the task."
    )


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
    aliases = tuple(
        a.strip()
        for a in os.environ.get("AGENT_ALIASES", "").split(",")
        if a.strip()
    )
    mode = os.environ.get("AGENT_MODE", "stub").lower()
    runpod = mode == "runpod"

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

    project_state = _ProjectState()
    project_handler = None
    if runpod:
        ws_env = os.environ.get("AGENT_WORKSPACE")
        if ws_env:
            agent_workspace = Path(ws_env)
            agent_workspace.mkdir(parents=True, exist_ok=True)
            project_handler = _build_project_handler(project_state, agent_workspace)
            most_recent = most_recent_project_dir(agent_workspace)
            if most_recent is None:
                # First boot in this workspace — no choice to make.
                initial = next_project_dir(agent_workspace)
                project_state.active = initial
                active_name = initial.name if initial is not None else "<none>"
                print(
                    colors.dim(
                        f"[project] active={active_name} (new) — :project new for fresh, "
                        ":project use N to switch, :project list to enumerate."
                    ),
                    flush=True,
                )
            else:
                # Existing projects found — defer to operator so reconnect is explicit.
                existing_names = ", ".join(
                    sorted(
                        (
                            e.name for e in agent_workspace.iterdir()
                            if e.is_dir() and re.fullmatch(r"project\d+", e.name)
                        ),
                        key=lambda n: int(n[len("project"):]),
                    )
                )
                print(
                    colors.dim(
                        f"[project] existing: {existing_names} — no active project. "
                        "Type :project new to start fresh, or :project use N to "
                        "reconnect. Inbound messages are skipped until you choose."
                    ),
                    flush=True,
                )

    if console is None:
        console = ConsoleControl(
            budget=budget,
            stop_event=stop_event,
            send_fn=transport.send,
            project_handler=project_handler,
        )
        console.start()

    system_prompt = load_system_prompt(agent_id, display_name)
    recent_replies: list[tuple[float, str]] = []
    recent_context: list[dict[str, str]] = []
    claim_grace_seconds = max(
        0.0,
        _env_float("CLAIM_CONTINUATION_GRACE_SECONDS", DEFAULT_CLAIM_CONTINUATION_GRACE_SECONDS),
    )
    pending_followup_seconds = max(
        0.0,
        _env_float("PENDING_FOLLOWUP_SECONDS", DEFAULT_PENDING_FOLLOWUP_SECONDS),
    )
    pending_followup: PendingFollowup | None = None
    _log(
        store,
        "session_start",
        f"agent_id={agent_id} display={display_name} aliases={','.join(aliases)} mode={mode}",
    )
    alias_note = f" aliases=[{', '.join(aliases)}]" if aliases else ""
    print(
        colors.dim(
            f"[part3] {display_name} (id={agent_id}){alias_note} listening via {mode}. "
            f"Type :help for console commands."
        ),
        flush=True,
    )

    def _hub_echo(arrow: str, sender: str, text: str) -> None:
        snippet = text[:160].replace("\n", " ")
        tag = colors.dim(f"[hub{arrow}]")
        print(f"{colors.ts()} {tag} {colors.agent_label(sender)}: {snippet}", flush=True)

    def _peer_console_log(kind: str, detail: str) -> None:
        """Live-attach trace of LLM/tool/refusal events. Runpod mode only."""
        if not runpod:
            return
        print(f"{colors.ts()} {colors.dim(f'[{kind}]')} {detail}", flush=True)

    def _send_answer(answer: str, msg_id: str) -> None:
        nonlocal pending_followup
        transport.send(answer)
        if not runpod:
            _hub_echo("->", display_name, answer)
        recent_context.append(_context_entry(display_name, answer, msg_id))
        if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
            del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
        recent_replies.append((time.time(), msg_id))
        if len(recent_replies) > 64:
            del recent_replies[:-64]
        if _answer_invites_followup(answer):
            pending_followup = PendingFollowup(timestamp=time.time(), message_id=msg_id, text=answer)
            _log(store, "pending_followup", f"msg_id={msg_id}")
        else:
            pending_followup = None
        budget.save()

    def _run_task_for_message(
        message: PeerMessage,
        prior_context: list[dict[str, str]] | None = None,
        collision: CollisionInfo | None = None,
        code_guidance: str | None = None,
    ) -> str | None:
        runtime_guidance = []
        if not _is_claim_continuation_message(message):
            guidance = assignment_guidance(
                message.text,
                agent_id=agent_id,
                display_name=display_name,
            )
            if guidance:
                runtime_guidance.append(guidance)
        guidance = followup_assignment_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = handoff_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
        )
        if guidance:
            runtime_guidance.append(guidance)
        unsatisfied = claims.unsatisfied_claims_for(agent_id)
        status_guidance = status_request_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
            open_claim_targets=[claim.target for claim in unsatisfied] or None,
        )
        if status_guidance:
            runtime_guidance.append(status_guidance)
            # Status guidance already folds open claims into the Blockers line,
            # so skip the separate stale-claim nudge that would otherwise push
            # the agent toward RELEASE (the collision the previous run hit).
        else:
            stale_guidance = _stale_claim_guidance(unsatisfied)
            if stale_guidance:
                runtime_guidance.append(stale_guidance)
        guidance = _released_without_write_guidance(
            claims.recently_released_unsatisfied_for(agent_id)
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = fix_blockers_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = private_workspace_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
        )
        if guidance:
            runtime_guidance.append(guidance)
        if runpod and project_state.active is not None:
            if code_guidance:
                runtime_guidance.append(code_guidance)
            runtime_guidance.append(
                _remote_workspace_guidance(agent_id, project_state.active.name)
            )
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
                runtime_guidance=runtime_guidance,
                console_log=_peer_console_log,
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
        nonlocal pending_followup
        if not runpod:
            _hub_echo("<-", message.sender_id, message.text)

        prior_context = _remember_inbound(message)

        if runpod and project_state.active is None:
            _absorb_inbound_claims(message)
            _log(
                store,
                "reply_decision",
                f"respond=False reason=no_active_project msg_id={message.id} sender={message.sender_id}",
            )
            print(
                f"{colors.ts()} {colors.dim('[skip] no active project — :project new or :project use N to choose')}",
                flush=True,
            )
            return

        # Save peer-shared code blocks to the active project even when the
        # reply gate will skip this message. Otherwise broadcasts of code
        # (e.g. peers posting `hangman.py` without addressing this agent)
        # never land on disk and we can't read_file them later.
        code_guidance: str | None = None
        if runpod and project_state.active is not None:
            code_guidance = process_shared_code(
                message.text, agent_id, project_state.active
            )
            if code_guidance:
                _log(
                    store,
                    "code_saved_on_arrival",
                    f"msg_id={message.id} sender={message.sender_id}",
                )

        now = time.time()
        followup_kind = _followup_reply_kind(message.text)
        if (
            followup_kind is not None
            and pending_followup is not None
            and _looks_like_operator_sender(message.sender_id, agent_id)
            and now - pending_followup.timestamp <= pending_followup_seconds
        ):
            reason = f"follow-up {followup_kind}"
            _log(
                store,
                "reply_decision",
                f"respond=True reason={reason} msg_id={message.id} sender={message.sender_id}",
            )
            pending_followup = None
            answer = _run_task_for_message(
                message, prior_context, code_guidance=code_guidance
            )
            if answer is None:
                _absorb_inbound_claims(message)
                return
            _absorb_inbound_claims(message)
            _send_answer(answer, message.id)

            if allow_claim_continuation:
                _continue_claims(message, answer)
            return

        if pending_followup is not None and now - pending_followup.timestamp > pending_followup_seconds:
            _log(store, "pending_followup_expired", f"msg_id={pending_followup.message_id}")
            pending_followup = None

        decision = should_reply(
            message, agent_id, display_name, recent_replies,
            claims=claims, aliases=aliases,
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

        answer = _run_task_for_message(
            message, prior_context, decision.collision, code_guidance=code_guidance
        )
        if answer is None:
            _absorb_inbound_claims(message)
            return
        _absorb_inbound_claims(message)
        _send_answer(answer, message.id)

        if allow_claim_continuation:
            _continue_task_status(message, answer)
            _continue_claims(message, answer)

    def _continue_task_status(original: PeerMessage, answer: str, depth: int = 0) -> None:
        if depth >= 3:
            _log(store, "task_status_continuation_skipped", "maximum nested task continuation depth reached")
            return
        status = parse_task_status(answer)
        if status is None or status.kind not in {"taking", "accepted"}:
            return

        continuation = _task_status_continuation_message(original, status)
        _log(
            store,
            "task_status_continuation",
            f"kind={status.kind} language={status.language} task={status.task} from_msg={original.id}",
        )
        prior_context = list(recent_context)
        recent_context.append(_context_entry(continuation.sender_id, continuation.text, continuation.id))
        if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
            del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
        continuation_answer = _run_task_for_message(continuation, prior_context)
        if continuation_answer is None:
            return
        _send_answer(continuation_answer, continuation.id)
        _continue_task_status(continuation, continuation_answer, depth + 1)
        _continue_claims(continuation, continuation_answer, depth + 1)

    def _continue_claims(original: PeerMessage, answer: str, depth: int = 0) -> None:
        if depth >= 3:
            _log(store, "claim_continuation_skipped", "maximum nested claim continuation depth reached")
            return
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
                _continue_claims(continuation, continuation_answer, depth + 1)
            # If the agent still holds this claim AND it hasn't been satisfied by
            # a successful write, AND the continuation answer was neither a
            # legitimate DEFER/RELEASE nor a follow-on CLAIM, the continuation
            # just died silently — the peer-task reprompt loop didn't fire
            # (e.g. prose final that didn't match the pending-write detector).
            # Surface that in audit so the stall is visible without timeline
            # cross-referencing.
            answer_text = continuation_answer or ""
            silent_final = not (
                CLAIM_PATTERN.search(answer_text)
                or RELEASE_PATTERN.search(answer_text)
                or DEFER_PATTERN.search(answer_text)
            )
            still_unsatisfied = any(
                c.target == claim.target for c in claims.unsatisfied_claims_for(agent_id)
            )
            if still_unsatisfied and silent_final:
                _log(
                    store,
                    "claim_continuation_ended_without_progress",
                    f"target={claim.target} from_msg={original.id}",
                )

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
        budget.save()
        print(format_usage_summary(display_name, budget.snapshot()), flush=True)
        try:
            transport.close()
        except Exception:
            pass
        console.stop()
        if owns_store:
            store.close()
