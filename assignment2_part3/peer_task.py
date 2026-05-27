"""One peer-message LLM round-trip.

Trimmed sibling of Part 2's `run_task`:

- No execution-detection heuristics (no auto post-edit pytest).
- Re-runs `peer_intent_refusal` on every round AND on tool args, so a
  leak attempt that survives the model is still caught.
- Gates every LLM call through the Budget.
- Scrubs the final answer through `peer.scrub_outbound` and logs the
  raw + scrubbed forms for audit.
- Bash calls go through `console_control.request_bash_approval` so the
  operator still gates destructive commands from the local console.
"""

from __future__ import annotations

import inspect
import json
import re
import shlex
import threading
from typing import Callable, Optional

import part2_bridge  # noqa: F401 — sys.path side effect; needed before Part 2 imports

from llm_client import complete_chat_with_metadata
from parser import parse_response
from safety import safety_check
from session_store import SessionStore
from tools import MAX_OUTPUT_CHARS, TOOL_REGISTRY, _resolve_workspace_path, run_tool

from budget import Budget, BudgetExceeded, estimate_tokens
from claims import CLAIM_PATTERN, DEFER_PATTERN, RELEASE_PATTERN, ClaimRegistry, split_claim_target
from console_control import ConsoleControl
from peer import PeerMessage, peer_intent_refusal, scrub_outbound
from reply_policy import CollisionInfo
from task_status import parse_task_status


MAX_STEPS = 8
MAX_CLAIM_CONTINUATION_STEPS = 12
# Two nudges before giveup: "describe instead of call" is a common LLM failure
# mode and one reprompt often isn't enough to course-correct. See plan
# i-think-this-went-serene-manatee.md for the calculator session that motivated this.
MAX_CONTINUATION_REPROMPTS_PER_REASON = 2
MAX_CONTEXT_MESSAGES = 24
MAX_CONTEXT_CHARS = 2000

CLAIM_GATED_TOOLS = {"create_file", "append_text", "edit_section", "rename_file", "replace_text"}
SHARED_PATH_PREFIX = "/workspace/shared/"
_AUTO_APPROVAL_CONTROL_CHARS = re.compile(r"[;&|]|\r|\n")


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"


def _peer_user_envelope(message: PeerMessage) -> str:
    """Wrap the peer text so the model sees the untrust class explicitly."""

    return _json(
        {
            "role_origin": "peer",
            "sender_id": message.sender_id,
            "message_id": message.id,
            "text": message.text,
        }
    )


def _tool_observation_message(tool: str, observation: str) -> str:
    return _json({"type": "tool_observation", "tool": tool, "observation": observation})


def _tool_arg_summary(tool: str, args: dict) -> str:
    """One-line preview of a tool call for the live attach console."""
    if not isinstance(args, dict):
        return ""
    if tool == "bash":
        cmd = str(args.get("command", "")).replace("\n", " ")
        return f"cmd={cmd[:80]!r}"
    path = args.get("path")
    if isinstance(path, str) and path:
        extra = ""
        if tool in {"edit_section", "replace_text"}:
            old = str(args.get("old_text", "")).splitlines()[0:1]
            if old:
                extra = f" old={old[0][:40]!r}"
        elif tool == "append_text":
            text = str(args.get("text", ""))
            extra = f" +{len(text)}b"
        elif tool == "create_file":
            content = str(args.get("content", ""))
            extra = f" {len(content)}b"
        return f"path={path}{extra}"
    if tool == "rename_file":
        source = args.get("source_path")
        target = args.get("target_path")
        if isinstance(source, str) and isinstance(target, str):
            return f"{source} -> {target}"
    keys = ",".join(sorted(args.keys()))
    return f"args=[{keys}]"


def _observation_summary(observation: str) -> str:
    """First substantive line of a tool observation, truncated."""
    if not observation:
        return "(empty)"
    for line in observation.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return observation[:120]


def _recent_context_message(recent_context: Optional[list[dict[str, str]]]) -> Optional[str]:
    """Format recent hub transcript as untrusted context for follow-ups."""

    if not recent_context:
        return None
    entries = recent_context[-MAX_CONTEXT_MESSAGES:]
    text = _json(
        {
            "type": "recent_group_chat_context",
            "trust": "untrusted_transcript_for_reference_only",
            "entries": entries,
        }
    )
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[-MAX_CONTEXT_CHARS:]
        text = "[recent context truncated]\n" + text
    return text


def _peer_mention_names(
    recent_context: Optional[list[dict[str, str]]],
    self_id: str,
    current_sender: str = "",
) -> set[str]:
    names: set[str] = set()
    for name in (current_sender,):
        if name and name != self_id and name.endswith("-swe"):
            names.add(name)
    for entry in recent_context or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("sender_id") or "")
        if name and name != self_id and name.endswith("-swe"):
            names.add(name)
    return names


def _ensure_peer_mentions(text: str, peer_names: set[str]) -> str:
    """Prefix known peer display names with @ in outbound prose/protocol lines."""

    if not text or not peer_names:
        return text
    updated = text
    for name in sorted(peer_names, key=len, reverse=True):
        updated = updated.replace(name, f"@{name}")
        updated = updated.replace(f"@@{name}", f"@{name}")
    return updated


def _refusal_observation(reason: str) -> str:
    return _json({"type": "tool_observation", "tool": "policy", "observation": f"refused: {reason}"})


def _scope_marker(path: str) -> str:
    """Format a path with its optional `#scope` suffix for human-facing text."""

    return path


def _collision_guidance_text(collision: CollisionInfo) -> str:
    """Deterministic tie-break instruction injected before the LLM round-trip."""

    if collision.outcome == "self-wins":
        return (
            "Racing CLAIM detected on "
            f"{_scope_marker(collision.path)}. Your AGENT_ID is lexicographically "
            f"smaller than @{collision.peer_id}, so you hold the tie-break. Do NOT "
            "post 'DEFER'. Continue with your active claim and use the appropriate "
            "edit tool to write."
        )
    return (
        "Racing CLAIM detected on "
        f"{_scope_marker(collision.path)}. You lost the tie-break to "
        f"@{collision.peer_id} (their AGENT_ID is lexicographically smaller). "
        "Reply with exactly two lines and stop: first 'DEFER to "
        f"@{collision.peer_id}', then 'RELEASE {collision.path}'. Propose a "
        "non-overlapping scope on your next turn."
    )


def _mutual_defer_guidance_text(self_id: str, peer_id: str) -> str:
    winner = peer_id if peer_id.lower() < self_id.lower() else self_id
    loser = peer_id if winner == self_id else self_id
    return (
        f"Mutual-defer detected between @{self_id} and @{peer_id}. Apply the "
        f"P3.9 tie-break: @{winner} re-claims the contested scope and proceeds; "
        f"@{loser} must release any conflicting claim and propose a "
        "non-overlapping scope. Do not post another bare 'DEFER' line."
    )


def _runtime_guidance_message(text: str) -> dict[str, str]:
    """Wrap runtime guidance so the model sees it as an authoritative note."""

    return {
        "role": "user",
        "content": _json(
            {
                "role_origin": "runtime",
                "trust": "authoritative",
                "text": text,
            }
        ),
    }


def _usage_value(usage: object, key: str) -> Optional[int]:
    if usage is None:
        return None
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _maybe_scrub_args_refusal(args: dict) -> Optional[str]:
    """Check tool args for peer-refusal-class leak attempts."""

    try:
        text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return peer_intent_refusal(text)


def _is_auto_approvable_bash_command(command: str) -> bool:
    """Return True for the narrow Part 3 bash approval bypass.

    The actual execution still goes through Part 2's run_tool/run_bash path,
    so this only decides whether the local operator prompt can be skipped.
    """

    command = command.strip()
    allowed, _reason = safety_check(command)
    if not allowed:
        return False
    if _AUTO_APPROVAL_CONTROL_CHARS.search(command):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    return bool(tokens) and tokens[0] == "ls"


def _run_tool_with_approval(
    tool: str,
    args: dict,
    console: Optional[ConsoleControl],
) -> str:
    if tool == "bash":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return "Tool error: bash requires a non-empty string command."
        if console is not None and not _is_auto_approvable_bash_command(command):
            if not console.request_bash_approval(command):
                return "The command was denied by the operator, so I did not run it."
    return run_tool(tool, args)


def _maybe_shared_write_refusal(
    tool: str,
    args: dict,
    claims: Optional[ClaimRegistry],
    self_id: str,
) -> Optional[str]:
    """Return a policy refusal for invalid shared writes."""

    if claims is None or tool not in CLAIM_GATED_TOOLS:
        return None
    path = args.get("path")
    if tool == "rename_file":
        source_path = args.get("source_path")
        target_path = args.get("target_path")
        if isinstance(source_path, str) and source_path.startswith(SHARED_PATH_PREFIX):
            path = source_path
        elif isinstance(target_path, str) and target_path.startswith(SHARED_PATH_PREFIX):
            path = target_path
    if not isinstance(path, str) or not path.startswith(SHARED_PATH_PREFIX):
        return None
    own_claim = claims.own_claim_for_write(path, self_id)
    if own_claim is None:
        return (
            f"no active claim for {path}. Post `CLAIM {path}#<scope>: <reason>` "
            "first, wait for the runtime continuation, and do not write unrelated shared files."
        )
    if tool == "create_file" and own_claim.scope is not None:
        try:
            target = _resolve_workspace_path(path)
        except ValueError:
            target = None
        if target is not None and target.exists():
            return (
                f"scoped claim {own_claim.target} cannot recreate existing shared file {path}. "
                "Read the current file and use edit_section or replace_text so peer work is preserved."
            )
    claim = claims.is_claimed_by_other(own_claim.target, self_id)
    if claim is None:
        return None
    return (
        f"deferred: @{claim.claimant} already claimed {claim.target}. "
        f"Reply with `DEFER to @{claim.claimant}` and offer review instead of writing."
    )


def _looks_like_failed_write(observation: str) -> bool:
    return observation.startswith(
        (
            "Edit blocked:",
            "Tool error:",
            "refused:",
            "Command exited with code",
            "The command was denied",
        )
    )


def _looks_like_write_success_claim(answer: str) -> bool:
    lowered = (answer or "").lower()
    return (
        "/workspace/shared/" in answer
        and any(word in lowered for word in ("created", "added", "updated", "wrote", "implemented"))
    )


def _is_claim_continuation(message: PeerMessage) -> bool:
    return message.sender_id == "runtime" and ":claim-continuation:" in message.id


def _claim_continuation_target(message: PeerMessage) -> str | None:
    if not _is_claim_continuation(message):
        return None
    _prefix, _marker, target = message.id.partition(":claim-continuation:")
    target = target.strip()
    if not target:
        return None
    path, scope = split_claim_target(target)
    return f"{path}#{scope}" if scope else path


_PYTEST_REQUEST_RE = re.compile(r"(?i)\b(?:pytest|tests?)\b")


def _pytest_was_requested(text: str) -> bool:
    return bool(_PYTEST_REQUEST_RE.search(text or ""))


_ACTION_REQUEST_RE = re.compile(
    r"(?i)\b("
    r"run\s+(?:the\s+)?(?:tests?|pytest)"
    r"|verify(?:\s+(?:the\s+)?(?:tests?|implementation|code))?"
    r"|ensure(?:\s+(?:that\s+)?[\w\s'/-]+)?\s+(?:is|are|gets?|be|being)?\s*(?:implemented|fixed|updated|defined)"
    r"|implement(?:ed|s|ing)?"
    r"|fix(?:ed|es|ing)?"
    r"|repair(?:ed|s|ing)?"
    r"|update(?:d|s|ing)?"
    r"|execute"
    r"|use\s+tools?"
    r"|call\s+run_tests"
    r"|run\s+pytest"
    r"|go\s+ahead"
    r"|please\s+(?:run|verify|test|execute|ensure|implement|fix|repair|update)"
    r"|gör"
    r"|skapa"
    r"|skriv"
    r"|implementera"
    r"|kör"
    r"|testa"
    r"|verifiera"
    r"|fixa"
    r")\b"
)


def _action_was_requested(text: str) -> bool:
    # Stricter than _pytest_was_requested: only True when the operator/peer
    # told the agent to do something, not when tests are merely discussed.
    return bool(_ACTION_REQUEST_RE.search(text or ""))


def _test_target_for_claim(target: str | None) -> str | None:
    if not target:
        return None
    path, scope = split_claim_target(target)
    if not path.startswith(SHARED_PATH_PREFIX) or not path.endswith(".py"):
        return None
    directory, _separator, filename = path.rpartition("/")
    stem = filename[:-3]
    test_path = path if stem.startswith("test_") else f"{directory}/test_{stem}.py"
    if scope:
        return f"{test_path}#{scope}-tests"
    return f"{test_path}#tests"


def _run_tests_path_for_target(target: str | None) -> str | None:
    """Return the bare /workspace/shared/test_<stem>.py path (no #scope) for a claim target."""

    if not target:
        return None
    path, _scope = split_claim_target(target)
    if not path.startswith(SHARED_PATH_PREFIX) or not path.endswith(".py"):
        return None
    directory, _separator, filename = path.rpartition("/")
    stem = filename[:-3]
    if stem.startswith("test_"):
        return path
    return f"{directory}/test_{stem}.py"


_TESTS_NOT_RUN_MARKERS = (
    "tests: not run",
    "tests not run",
    "have not run",
    "haven't run",
    "did not run",
    "didn't run",
)


def _looks_like_done_without_tests(answer: str) -> bool:
    """Detect Done-style finals that admit pytest was never executed.

    Returns False when the answer reports a real pytest outcome (`ran and
    passed` / `ran and failed`) or when it is a legitimate continuation exit
    (RELEASE/DEFER protocol lines).
    """

    text = answer or ""
    lowered = text.lower()
    if not lowered:
        return False
    if "ran and passed" in lowered or "ran and failed" in lowered:
        return False
    if RELEASE_PATTERN.search(text) or DEFER_PATTERN.search(text):
        return False
    return any(marker in lowered for marker in _TESTS_NOT_RUN_MARKERS)


def _states_test_blocker(answer: str) -> bool:
    lowered = (answer or "").lower()
    blocker_markers = (
        "blocker",
        "blocked",
        "cannot run",
        "can't run",
        "unable to run",
        "kunde inte",
        "kan inte",
        "blockerad",
    )
    test_markers = ("pytest", "test", "tests", "tester")
    return any(marker in lowered for marker in blocker_markers) and any(
        marker in lowered for marker in test_markers
    )


def _claim_targets_from_text(text: str) -> set[str]:
    targets: set[str] = set()
    for match in CLAIM_PATTERN.finditer(text or ""):
        path, scope = split_claim_target(match.group("path"))
        targets.add(f"{path}#{scope}" if scope else path)
    return targets


def _looks_like_pending_shared_write(answer: str) -> bool:
    """Detect declarative no-op finals during a shared-claim continuation.

    The reprompt loop already short-circuits CLAIM-repeats, RELEASE, and DEFER
    before reaching this check (see the RELEASE branch around line 736 and the
    repeated-CLAIM branch above). So any *other* prose final that names the
    shared path during an unsatisfied claim is a stall — including
    "I need to re-read X" or "I'll review X" prose that doesn't yet mention a
    write verb. See plan this-was-quite-a-refactored-balloon.md for the alice
    stall that motivated dropping the write-verb conjunction.
    """

    if SHARED_PATH_PREFIX not in (answer or ""):
        return False
    if RELEASE_PATTERN.search(answer) or DEFER_PATTERN.search(answer):
        # RELEASE has its own reprompt branch downstream; DEFER is a legitimate
        # collision response. Neither should be misread as a write stall.
        return False
    lowered = answer.lower()
    pending_markers = (
        "i will",
        "i'll",
        "i need to",
        "i am going to",
        "i'm going to",
        "jag ska",
        "jag kommer att",
        "jag behöver",
        "jag tänker",
        "going to",
        "will create",
        "will implement",
        "will write",
        "need to create",
        "ready to create",
        "does not exist",
        "doesn't exist",
        "re-read",
        "reread",
        "look at",
        "review",
        "need to read",
        "have to read",
        "should read",
    )
    return bool(_PENDING_ACTION_PROMISE_RE.search(answer)) or any(
        marker in lowered for marker in pending_markers
    )


_PENDING_WRITE_MARKERS_ANY_PATH = (
    "i will create",
    "i'll create",
    "i will implement",
    "i'll implement",
    "i will write",
    "i'll write",
    "i will add",
    "i'll add",
    "i will start",
    "i'll start",
    "i need to create",
    "i need to implement",
    "i need to write",
    "i need to re-read",
    "i need to reread",
    "i need to read",
    "i am going to",
    "i'm going to",
    "jag ska",
    "jag kommer att",
    "jag behöver",
    "jag tänker",
    "going to create",
    "going to implement",
    "going to write",
    "let me create",
    "let me implement",
    "let me write",
    "let me re-read",
    "let me read",
    "start the implementation",
    "start this implementation",
    "begin the implementation",
)


_PENDING_ACTION_PROMISE_RE = re.compile(
    r"(?i)\b(?:"
    r"i\s+will"
    r"|i'll"
    r"|i\s+am\s+going\s+to"
    r"|i'm\s+going\s+to"
    r"|i\s+need\s+to"
    r"|jag\s+ska"
    r"|jag\s+kommer\s+att"
    r"|jag\s+behöver"
    r"|jag\s+tänker"
    r")\b"
)


def _looks_like_pending_write_any_path(answer: str) -> bool:
    """Path-agnostic counterpart of `_looks_like_pending_shared_write`.

    Used so remote-hub mode (writes go to /workspace/<agent_id>/projectN/)
    gets the same stall coverage as the shared-workspace flow. Tighter
    marker list than the shared-path version on purpose: this fires even
    when the answer mentions no /workspace/ path at all, so false
    positives are costlier here.
    """

    if not answer:
        return False
    if RELEASE_PATTERN.search(answer) or DEFER_PATTERN.search(answer):
        return False
    if CLAIM_PATTERN.search(answer):
        return False
    lowered = answer.lower()
    return bool(_PENDING_ACTION_PROMISE_RE.search(answer)) or any(
        marker in lowered for marker in _PENDING_WRITE_MARKERS_ANY_PATH
    )


_COMPLETION_CLAIM_MARKERS = (
    "done:",
    "done with:",
    "klar med:",
    "i have implemented",
    "i have created",
    "i have written",
    "i have added",
    "i have saved",
    "i've implemented",
    "i've created",
    "i've written",
    "i've added",
    "i've saved",
    "successfully implemented",
    "successfully created",
    "successfully wrote",
    "implementation is complete",
    "the implementation is complete",
    "has been implemented",
    "has been created",
    "has been written",
    "the file was created",
    "the script was created",
    "the file has been created",
    "the script has been created",
)


def _looks_like_completion_claim_any_path(answer: str) -> bool:
    """Detect first-person claims that work has been completed.

    Dangerous when no successful write tool observation happened this
    round — the model is fabricating context. Reused both for the
    user-action reprompt branch (force a real tool call) and for the
    final-answer correction layer (rewrite the lie if the model still
    won't comply).
    """

    if not answer:
        return False
    if RELEASE_PATTERN.search(answer) or DEFER_PATTERN.search(answer):
        return False
    status = parse_task_status(answer)
    if status is not None and status.kind == "done":
        return True
    lowered = answer.lower()
    return any(marker in lowered for marker in _COMPLETION_CLAIM_MARKERS)


def _looks_like_pending_test_work(answer: str) -> bool:
    lowered = (answer or "").lower()
    if "claim /workspace/shared/" in lowered:
        return False
    pending_markers = (
        "i will",
        "i'll",
        "i need to",
        "i am going to",
        "i'm going to",
        "jag ska",
        "jag kommer att",
        "jag behöver",
        "jag tänker",
        "going to",
        "next steps",
        "now i will",
    )
    test_markers = ("pytest", "test", "tests")
    return (
        bool(_PENDING_ACTION_PROMISE_RE.search(answer))
        or any(marker in lowered for marker in pending_markers)
    ) and any(
        marker in lowered for marker in test_markers
    )


def _edit_recovery_guidance(tool: str, observation: str) -> str | None:
    if tool not in {"edit_section", "replace_text"}:
        return None
    if not observation.startswith("Edit blocked:"):
        return None
    if (
        "old_text must be a non-empty string" not in observation
        and "old_text was not found as a complete line section" not in observation
    ):
        return None
    return (
        "The edit failed because old_text did not identify an existing whole-line "
        "section. Do not retry the same edit. To append new code or tests to an "
        "existing shared file, call append_text with only the text to add. If you "
        "must rewrite existing content, call read_file first and then use "
        "edit_section with old_text equal to an exact complete section from that "
        "observation."
    )


def _parser_guidance_text(raw_response: str, error: str | None) -> str:
    try:
        payload = json.loads(raw_response)
    except (TypeError, ValueError):
        return (
            "Your previous response was invalid. Respond with exactly one JSON object and no prose. "
            f"Parser error: {error}"
        )

    if isinstance(payload, dict) and payload.get("type") in TOOL_REGISTRY:
        return (
            "Your previous response used a tool name as the JSON type. For a tool call, "
            'use {"type":"tool_call","tool":"<tool_name>","args":{...}} exactly. '
            f"Parser error: {error}"
        )

    return (
        "Your previous response was invalid. Respond with exactly one JSON object and no prose. "
        f"Parser error: {error}"
    )


def run_peer_task(
    message: PeerMessage,
    *,
    store: SessionStore,
    budget: Budget,
    system_prompt: str,
    console: Optional[ConsoleControl] = None,
    chat_fn=None,
    budget_save_event: Optional[threading.Event] = None,
    claims: Optional[ClaimRegistry] = None,
    agent_id: Optional[str] = None,
    recent_context: Optional[list[dict[str, str]]] = None,
    absorb_claims: bool = True,
    collision: Optional[CollisionInfo] = None,
    runtime_guidance: Optional[list[str]] = None,
    console_log: Optional[Callable[[str, str], None]] = None,
) -> str:
    # Late binding so monkey-patching `peer_task.complete_chat_with_metadata`
    # in tests works.
    if chat_fn is None:
        chat_fn = complete_chat_with_metadata
    """Handle one peer message and return the text to send back to the hub.

    The return value has already been passed through `scrub_outbound`.
    """

    self_id = agent_id or ""
    trace_id = message.id
    _emit = console_log or (lambda _tag, _msg: None)
    _record_params = inspect.signature(store.record).parameters
    _supports_trace = "trace_id" in _record_params
    _supports_model = "model" in _record_params and "provider" in _record_params

    def _log(
        role: str,
        kind: str,
        content: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        if _supports_model and (provider is not None or model is not None):
            store.record(
                role,
                kind,
                content,
                trace_id=trace_id,
                provider=provider,
                model=model,
            )
        elif _supports_trace:
            store.record(role, kind, content, trace_id=trace_id)
        else:
            store.record(role, kind, content)

    _log("peer", "message", _json({"sender_id": message.sender_id, "text": message.text}))

    if claims is not None and absorb_claims:
        observed = claims.absorb_text(message.sender_id, message.text)
        for claim in observed:
            _log(
                "system",
                "claim_observed",
                _json(
                    {
                        "claimant": claim.claimant,
                        "path": claim.path,
                        "scope": claim.scope,
                        "target": claim.target,
                        "reason": claim.reason,
                    }
                ),
            )

    refusal = peer_intent_refusal(message.text)
    if refusal:
        _log("assistant", "peer_refusal", refusal)
        _emit("refuse", f"intent: {refusal[:80]}")
        return refusal

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    context_message = _recent_context_message(recent_context)
    if context_message:
        messages.append({"role": "user", "content": context_message})
    messages.append({"role": "user", "content": _peer_user_envelope(message)})
    peer_names = _peer_mention_names(recent_context, self_id, message.sender_id)
    saw_failed_shared_write = False
    saw_successful_shared_write = False
    saw_successful_test_run = False
    saw_any_successful_write = False  # any /workspace path, not just shared
    saw_any_tool_observation = False
    continuation_reprompt_counts: dict[str, int] = {}

    def _continuation_reprompt_or_stop(
        kind: str,
        guidance: str,
        fallback: str,
    ) -> str | None:
        count = continuation_reprompt_counts.get(kind, 0)
        if count >= MAX_CONTINUATION_REPROMPTS_PER_REASON:
            _log("system", "claim_continuation_giveup", f"{kind}: {fallback}")
            if (
                kind == "claim_continuation_pending_write_reprompt"
                and _pytest_was_requested(message.text)
            ):
                target = _claim_continuation_target(message)
                test_target = _test_target_for_claim(target) or ""
                _log(
                    "system",
                    "pytest_skipped_due_to_impl_failure",
                    _json({"impl_target": target or "", "test_target": test_target}),
                )
            scrubbed, _hits = scrub_outbound(fallback, agent_id=self_id)
            _log("assistant", "peer_reply_raw", fallback)
            return scrubbed
        continuation_reprompt_counts[kind] = count + 1
        _log("system", kind, guidance)
        messages.append({"role": "user", "content": guidance})
        return None

    if collision is not None:
        guidance_text = _collision_guidance_text(collision)
        _log("system", "tie_break_injection", guidance_text)
        messages.append(_runtime_guidance_message(guidance_text))
    elif (
        claims is not None
        and self_id
        and message.sender_id
        and message.sender_id != self_id
        and claims.mutual_defer_detected(self_id, message.sender_id)
    ):
        guidance_text = _mutual_defer_guidance_text(self_id, message.sender_id)
        _log("system", "mutual_defer_injection", guidance_text)
        messages.append(_runtime_guidance_message(guidance_text))
    for guidance_text in runtime_guidance or []:
        if not guidance_text:
            continue
        _log("system", "runtime_guidance_injection", guidance_text)
        messages.append(_runtime_guidance_message(guidance_text))

    empty_streak = 0
    max_steps = MAX_CLAIM_CONTINUATION_STEPS if _is_claim_continuation(message) else MAX_STEPS
    for step in range(1, max_steps + 1):
        estimate = estimate_tokens(_json({"messages": messages}))
        try:
            budget.permit(estimate)
        except BudgetExceeded as exc:
            _log("system", "budget_exceeded", exc.reason)
            _emit("budget!", exc.reason[:120])
            if console is None:
                return f"I have to stop here: my session budget is exhausted ({exc.reason})."
            _log(
                "system",
                "budget_override_requested",
                _json({"reason": exc.reason, "estimated_tokens": estimate}),
            )
            approved = console.request_budget_approval(exc.reason, estimate)
            if not approved:
                _log(
                    "system",
                    "budget_override_denied",
                    _json({"reason": exc.reason, "estimated_tokens": estimate}),
                )
                return f"I have to stop here: my session budget is exhausted ({exc.reason})."
            _log(
                "system",
                "budget_override_approved",
                _json({"reason": exc.reason, "estimated_tokens": estimate}),
            )
            try:
                budget.permit(estimate, override=True)
            except BudgetExceeded as override_exc:
                _log("system", "budget_override_failed", override_exc.reason)
                return (
                    "I have to stop here: my session budget is exhausted "
                    f"({override_exc.reason})."
                )

        result = chat_fn(messages)
        usage = None
        if isinstance(result, tuple):
            raw_response = result[0]
            provider = result[1] if len(result) > 1 else None
            model = result[2] if len(result) > 2 else None
            usage = result[3] if len(result) > 3 else None
        else:
            raw_response, provider, model = result, None, None
        budget.record_usage(
            prompt_tokens=_usage_value(usage, "prompt_tokens"),
            completion_tokens=_usage_value(usage, "completion_tokens"),
            total_tokens=_usage_value(usage, "total_tokens"),
            estimated_tokens=estimate_tokens(raw_response or ""),
        )
        _log("assistant", "raw_json", raw_response, provider=provider, model=model)
        if budget_save_event is not None:
            budget_save_event.set()
        _prompt_t = _usage_value(usage, "prompt_tokens")
        _completion_t = _usage_value(usage, "completion_tokens")
        _llm_id = f"{provider}/{model}" if (provider or model) else "model"
        if _prompt_t is not None or _completion_t is not None:
            _emit(
                "llm",
                f"{_llm_id} step={step} prompt={_prompt_t or 0}t out={_completion_t or 0}t",
            )
        else:
            _emit("llm", f"{_llm_id} step={step}")

        if not (raw_response or "").strip():
            empty_streak += 1
            # Empty responses waste steps and don't help the model recover; bail
            # after two in a row with a clearer reason than "step budget".
            if empty_streak >= 2:
                reason = (
                    f"model returned empty response {empty_streak} times in a row "
                    "(likely truncated output or token cap)"
                )
                _log("system", "empty_response_giveup", reason)
                fallback = (
                    "I had to stop: the model returned empty replies repeatedly. "
                    "Try again, shorten the request, or raise LLM_MAX_TOKENS."
                )
                scrubbed, _ = scrub_outbound(fallback, agent_id=self_id)
                _log("assistant", "peer_reply_raw", fallback)
                return scrubbed
            # Don't pollute history with the empty turn; just re-prompt.
            guidance = (
                "Your previous response was empty. Respond with exactly one JSON "
                "object and no prose."
            )
            _log("system", "parser_guidance", guidance)
            messages.append({"role": "user", "content": guidance})
            continue
        empty_streak = 0

        messages.append({"role": "assistant", "content": raw_response})
        parsed = parse_response(raw_response, allowed_tools=TOOL_REGISTRY.keys())

        if parsed.kind == "final":
            answer = parsed.answer or ""
            if _is_claim_continuation(message) and CLAIM_PATTERN.search(answer):
                current_target = _claim_continuation_target(message)
                claimed_targets = _claim_targets_from_text(answer)
                if current_target is not None and claimed_targets - {current_target}:
                    scrubbed, hits = scrub_outbound(answer, agent_id=self_id)
                    _log("assistant", "peer_reply_raw", answer)
                    if hits:
                        _log("assistant", "peer_reply_scrubbed", _json({"hits": hits, "text": scrubbed}))
                    scrubbed = _ensure_peer_mentions(scrubbed, peer_names)
                    if claims is not None and self_id:
                        for claim in claims.absorb_text(self_id, scrubbed):
                            _log(
                                "system",
                                "claim_self",
                                _json(
                                    {
                                        "path": claim.path,
                                        "scope": claim.scope,
                                        "target": claim.target,
                                        "reason": claim.reason,
                                    }
                                ),
                            )
                    return scrubbed
                guidance = (
                    "You already posted the CLAIM. This is the runtime continuation for that "
                    "active claim, so do not reply with another CLAIM. Use a tool call now "
                    "(read_file, create_file, append_text, edit_section, rename_file, or replace_text), then "
                    "report only after the tool observation succeeds."
                )
                stopped = _continuation_reprompt_or_stop(
                    "claim_continuation_reprompt",
                    guidance,
                    "I had to stop because I repeated a CLAIM instead of using a write tool.",
                )
                if stopped is not None:
                    return stopped
                continue
            if (
                _is_claim_continuation(message)
                and saw_successful_shared_write
                and _looks_like_pending_test_work(answer)
            ):
                test_target = _test_target_for_claim(_claim_continuation_target(message))
                target_guidance = (
                    f" Post exactly this CLAIM target if you will write tests: {test_target}."
                    if test_target
                    else ""
                )
                guidance = (
                    "A successful shared implementation write already happened in this "
                    "runtime continuation. If the original request also asked for pytest "
                    "coverage, do not describe test work as a future step. Either emit a "
                    "new CLAIM for the shared test file so the runtime can continue into "
                    "the test write, or send a final summary that explicitly says tests "
                    f"were not written/run.{target_guidance}"
                )
                stopped = _continuation_reprompt_or_stop(
                    "claim_continuation_pending_tests_reprompt",
                    guidance,
                    "I had to stop because I kept describing test work instead of claiming or writing tests.",
                )
                if stopped is not None:
                    return stopped
                continue
            if (
                _is_claim_continuation(message)
                and _pytest_was_requested(message.text)
                and saw_successful_shared_write
                and not saw_successful_test_run
                and _looks_like_done_without_tests(answer)
            ):
                test_path = (
                    _run_tests_path_for_target(_claim_continuation_target(message))
                    or "the shared test file path"
                )
                guidance = (
                    "A successful shared-file write happened in this continuation and the "
                    "original request asked for pytest coverage, but no run_tests observation "
                    "exists yet in this round. Do not report Done with 'Tests: not run'. "
                    'Call run_tests now with {"path": "' + test_path + '"} and only emit the '
                    "final answer after the observation. Report the pytest result honestly: "
                    "'Tests: ran and passed' on green, or 'Tests: ran and failed' followed "
                    "by the first failure line on red. If the test file does not yet exist "
                    "(peer hasn't written it), say so in Blockers instead of claiming Done."
                )
                stopped = _continuation_reprompt_or_stop(
                    "claim_continuation_pytest_required_reprompt",
                    guidance,
                    "I had to stop because I kept reporting Done without running the requested pytest verification.",
                )
                if stopped is not None:
                    return stopped
                continue
            if (
                not _is_claim_continuation(message)
                and _pytest_was_requested(message.text)
                and saw_any_successful_write
                and not saw_successful_test_run
                and _looks_like_completion_claim_any_path(answer)
                and not _states_test_blocker(answer)
            ):
                guidance = (
                    "The request asked for tests or pytest, and you already have a successful "
                    "write observation in this round, but no run_tests observation yet. Do not "
                    "report `Klar med:` or `Done with:` until you call run_tests, unless you "
                    "state a concrete blocker that prevents running tests. Call run_tests now "
                    "for the file or test directory you created/updated, then report the result "
                    "honestly."
                )
                stopped = _continuation_reprompt_or_stop(
                    "user_action_pytest_required_reprompt",
                    guidance,
                    "I had to stop because I reported done without running the requested pytest verification.",
                )
                if stopped is not None:
                    return stopped
                continue
            if (
                _is_claim_continuation(message)
                and not saw_successful_shared_write
                and _looks_like_pending_shared_write(answer)
            ):
                claim_target = _claim_continuation_target(message)
                example_path = (
                    split_claim_target(claim_target)[0]
                    if claim_target
                    else "/workspace/shared/<file>"
                )
                example_call = _json(
                    {
                        "type": "tool_call",
                        "tool": "create_file",
                        "args": {
                            "path": example_path,
                            "content": "<file contents here>",
                        },
                    }
                )
                guidance = (
                    "This is still the runtime continuation for your active shared-file claim. "
                    'Only {"type":"tool_call",...} JSON is valid on this turn. '
                    '{"type":"final",...} replies — including RELEASE prose, "I will read first", '
                    '"I need to implement", or any description of what you plan to do — are '
                    "invalid and will be rejected until a successful write tool observation is "
                    "recorded. For a new file, the response MUST look exactly like this "
                    f"(replace the content placeholder): {example_call}. If the file already "
                    "exists (e.g. a peer wrote first), call read_file then append_text for "
                    "additive work, or edit_section/replace_text for exact replacements — do not "
                    "retry create_file on an existing shared file. Only send a final answer "
                    "after a successful shared-file write tool observation."
                )
                stopped = _continuation_reprompt_or_stop(
                    "claim_continuation_pending_write_reprompt",
                    guidance,
                    "I had to stop because I kept describing the write instead of using a write tool.",
                )
                if stopped is not None:
                    return stopped
                continue
            if (
                not _is_claim_continuation(message)
                and _action_was_requested(message.text)
                and _looks_like_pending_shared_write(answer)
            ):
                guidance = (
                    "The user asked you to take action (e.g. run pytest or read a file) and your "
                    'reply was prose only. {"type":"final",...} replies describing what you "will" '
                    'or "need to" do are invalid here — make the tool call now. For verification, '
                    'call {"type":"tool_call","tool":"run_tests","args":{"path":"/workspace/shared/test_<file>.py"}}. '
                    "For inspection, use read_file first. Only send a final answer after a tool observation."
                )
                stopped = _continuation_reprompt_or_stop(
                    "user_action_prose_stall_reprompt",
                    guidance,
                    "I had to stop because I kept describing what I would do instead of calling a tool.",
                )
                if stopped is not None:
                    return stopped
                continue
            if (
                not _is_claim_continuation(message)
                and _action_was_requested(message.text)
                and not saw_any_successful_write
                and not saw_successful_test_run
                and (
                    _looks_like_completion_claim_any_path(answer)
                    or _looks_like_pending_write_any_path(answer)
                )
            ):
                # Remote-hub-mode (and any path-agnostic) variant of the
                # stall guard above. Fires when the user asked for action
                # but the round ended with prose that either fabricates
                # completion ("Done: Implemented...") or postpones it
                # ("I will create..."), without any successful write tool
                # observation backing it up.
                guidance = (
                    "The user asked you to take action and your reply has no successful write "
                    "tool observation in this round. Never claim Done/Implemented/Created unless "
                    "a create_file/append_text/edit_section/replace_text/rename_file observation "
                    "for the target path was returned in this round. Make the actual tool call "
                    'now: {"type":"tool_call","tool":"create_file","args":{"path":"/workspace/'
                    "<your_agent_id>/<projectN>/<filename>\",\"content\":\"...\"}} for a new "
                    "file (use your active project dir; do NOT write to /workspace/shared on the "
                    "remote hub), or append_text / edit_section / replace_text for an existing "
                    "file. After a successful write, your final answer MUST name the exact path "
                    "you wrote AND paste the file contents in a fenced code block (with `# file: "
                    "<filename>` as the first line inside the fence) so peers can see what was "
                    "actually done."
                )
                stopped = _continuation_reprompt_or_stop(
                    "user_action_no_write_reprompt",
                    guidance,
                    "I had to stop because I described work as Done or upcoming without actually "
                    "calling a write tool. No file was created.",
                )
                if stopped is not None:
                    return stopped
                continue
            if (
                _is_claim_continuation(message)
                and RELEASE_PATTERN.search(answer)
                and not saw_successful_shared_write
            ):
                guidance = (
                    "You posted RELEASE but the runtime has no successful "
                    "create_file/append_text/edit_section/replace_text observation for "
                    "/workspace/shared in this round. RELEASE without a write abandons the "
                    "claim and leaves the work undone. Either call the write tool now to "
                    "complete the work, or send a final answer that explicitly explains "
                    "why you cannot proceed (do not just repeat RELEASE)."
                )
                stopped = _continuation_reprompt_or_stop(
                    "claim_release_without_write_reprompt",
                    guidance,
                    "I had to stop because I tried to release the claim before completing the write.",
                )
                if stopped is not None:
                    return stopped
                continue
            scrubbed, hits = scrub_outbound(answer, agent_id=self_id)
            _log("assistant", "peer_reply_raw", answer)
            if hits:
                _log("assistant", "peer_reply_scrubbed", _json({"hits": hits, "text": scrubbed}))
            if saw_failed_shared_write and _looks_like_write_success_claim(scrubbed):
                scrubbed = (
                    "I could not complete the shared-file write. The latest tool observation "
                    "reported a block/refusal, so no successful update to /workspace/shared "
                    "should be assumed."
                )
                _log("assistant", "peer_reply_corrected", scrubbed)
            elif (
                not saw_any_successful_write
                and not saw_successful_test_run
                and _looks_like_completion_claim_any_path(scrubbed)
            ):
                # Final safety net: the reprompt loop above gave up (or
                # this is a non-action-requested context that still tried
                # to fabricate completion). Replace the lie with the truth
                # rather than ship it to the hub.
                scrubbed = (
                    "I have not actually created or edited any file in this round — no "
                    "create_file/append_text/edit_section/replace_text observation was "
                    "returned. Please rephrase the request or let me know if you want me "
                    "to retry, and I will call the write tool this time."
                )
                _log("assistant", "peer_reply_corrected", scrubbed)
            scrubbed = _ensure_peer_mentions(scrubbed, peer_names)
            if claims is not None and self_id:
                for claim in claims.absorb_text(self_id, scrubbed):
                    _log(
                        "system",
                        "claim_self",
                        _json(
                            {
                                "path": claim.path,
                                "scope": claim.scope,
                                "target": claim.target,
                                "reason": claim.reason,
                            }
                        ),
                    )
            return scrubbed

        if parsed.kind == "tool_call":
            args_refusal = _maybe_scrub_args_refusal(parsed.args)
            if args_refusal:
                _log("system", "peer_refusal_tool_args", args_refusal)
                _emit("refuse", f"tool_args: {args_refusal[:80]}")
                observation = _refusal_observation(args_refusal)
                messages.append({"role": "user", "content": observation})
                continue

            block_reason = _maybe_shared_write_refusal(parsed.tool, parsed.args, claims, self_id)
            if block_reason:
                _log("system", "claim_block", block_reason)
                _emit("block", block_reason[:120])
                saw_failed_shared_write = True
                observation = _refusal_observation(block_reason)
                messages.append({"role": "user", "content": observation})
                continue

            _emit("tool", f"{parsed.tool} {_tool_arg_summary(parsed.tool, parsed.args)}")
            observation = _run_tool_with_approval(parsed.tool, parsed.args, console)
            observation = _truncate(observation)
            _emit("tool=", _observation_summary(observation))
            saw_any_tool_observation = True
            if (
                parsed.tool in CLAIM_GATED_TOOLS
                and isinstance(parsed.args.get("path"), str)
                and parsed.args["path"].startswith(SHARED_PATH_PREFIX)
            ):
                if _looks_like_failed_write(observation):
                    saw_failed_shared_write = True
                else:
                    # A subsequent successful shared write supersedes an
                    # earlier failure in this turn. Without this, a recovery
                    # sequence (create_file blocked → read_file → edit_section
                    # succeeded) still trips _looks_like_write_success_claim
                    # and the model's truthful answer gets overwritten below.
                    saw_failed_shared_write = False
                    saw_successful_shared_write = True
                    saw_any_successful_write = True
                    if claims is not None and self_id:
                        claims.mark_satisfied(self_id, parsed.args["path"])
            elif (
                parsed.tool in CLAIM_GATED_TOOLS
                and isinstance(parsed.args.get("path"), str)
                and not _looks_like_failed_write(observation)
            ):
                # Private/project writes (remote-hub mode). Tracked separately
                # from saw_successful_shared_write so claim-gate logic stays
                # shared-only, but the user-facing "did you actually do it?"
                # gate sees them.
                saw_any_successful_write = True
            if parsed.tool == "rename_file":
                source_path = parsed.args.get("source_path")
                target_path = parsed.args.get("target_path")
                shared_path = None
                if isinstance(source_path, str) and source_path.startswith(SHARED_PATH_PREFIX):
                    shared_path = source_path
                elif isinstance(target_path, str) and target_path.startswith(SHARED_PATH_PREFIX):
                    shared_path = target_path
                if shared_path is not None:
                    if _looks_like_failed_write(observation):
                        saw_failed_shared_write = True
                    else:
                        saw_failed_shared_write = False
                        saw_successful_shared_write = True
                        saw_any_successful_write = True
                        if claims is not None and self_id:
                            claims.mark_satisfied(self_id, shared_path)
                elif not _looks_like_failed_write(observation):
                    saw_any_successful_write = True
            if parsed.tool == "run_tests":
                # Any reachable run_tests attempt counts as verification: a red
                # pytest is still proof the agent tried, and the next final
                # answer can honestly report `Tests: ran and failed` instead of
                # being reprompted into a loop.
                saw_successful_test_run = True
            _log(
                "tool",
                parsed.tool,
                _json({"args": parsed.args, "observation": observation}),
            )
            messages.append(
                {"role": "user", "content": _tool_observation_message(parsed.tool, observation)}
            )
            if _is_claim_continuation(message):
                guidance = _edit_recovery_guidance(parsed.tool, observation)
                if guidance:
                    _log("system", "edit_recovery_guidance", guidance)
                    messages.append(_runtime_guidance_message(guidance))
            continue

        if SHARED_PATH_PREFIX in raw_response and any(
            tool in raw_response for tool in CLAIM_GATED_TOOLS
        ):
            saw_failed_shared_write = True
        guidance = _parser_guidance_text(raw_response, parsed.error)
        if _is_claim_continuation(message):
            stopped = _continuation_reprompt_or_stop(
                "parser_guidance",
                guidance,
                "I had to stop because I kept returning invalid JSON instead of a valid tool call.",
            )
            if stopped is not None:
                return stopped
        else:
            _log("system", "parser_guidance", guidance)
            messages.append({"role": "user", "content": guidance})

    fallback = "I could not complete this within my step budget. Please rephrase or split the task."
    scrubbed, _ = scrub_outbound(fallback, agent_id=self_id)
    _log("assistant", "peer_reply_raw", fallback)
    return scrubbed
