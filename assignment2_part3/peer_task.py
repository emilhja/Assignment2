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
import threading
from typing import Optional

import part2_bridge  # noqa: F401 — sys.path side effect; needed before Part 2 imports

from llm_client import complete_chat_with_metadata
from parser import parse_response
from session_store import SessionStore
from tools import MAX_OUTPUT_CHARS, TOOL_REGISTRY, _resolve_workspace_path, run_tool

from budget import Budget, BudgetExceeded, estimate_tokens
from claims import ClaimRegistry
from console_control import ConsoleControl
from peer import PeerMessage, peer_intent_refusal, scrub_outbound
from reply_policy import CollisionInfo


MAX_STEPS = 8
MAX_CONTEXT_MESSAGES = 24
MAX_CONTEXT_CHARS = 2000

CLAIM_GATED_TOOLS = {"create_file", "edit_section", "replace_text"}
SHARED_PATH_PREFIX = "/workspace/shared/"


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


def _maybe_scrub_args_refusal(args: dict) -> Optional[str]:
    """Check tool args for peer-refusal-class leak attempts."""

    try:
        text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return peer_intent_refusal(text)


def _run_tool_with_approval(
    tool: str,
    args: dict,
    console: Optional[ConsoleControl],
) -> str:
    if tool == "bash":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return "Tool error: bash requires a non-empty string command."
        if console is not None:
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
        return refusal

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    context_message = _recent_context_message(recent_context)
    if context_message:
        messages.append({"role": "user", "content": context_message})
    messages.append({"role": "user", "content": _peer_user_envelope(message)})
    peer_names = _peer_mention_names(recent_context, self_id, message.sender_id)
    saw_failed_shared_write = False

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

    for step in range(1, MAX_STEPS + 1):
        estimate = estimate_tokens(_json({"messages": messages}))
        try:
            budget.permit(estimate)
        except BudgetExceeded as exc:
            _log("system", "budget_exceeded", exc.reason)
            return f"I have to stop here: my session budget is exhausted ({exc.reason})."

        result = chat_fn(messages)
        if isinstance(result, tuple):
            raw_response, provider, model = result
        else:
            raw_response, provider, model = result, None, None
        budget.record(estimate_tokens(raw_response or ""))
        _log("assistant", "raw_json", raw_response, provider=provider, model=model)
        if budget_save_event is not None:
            budget_save_event.set()

        messages.append({"role": "assistant", "content": raw_response})
        parsed = parse_response(raw_response, allowed_tools=TOOL_REGISTRY.keys())

        if parsed.kind == "final":
            answer = parsed.answer or ""
            scrubbed, hits = scrub_outbound(answer)
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
                observation = _refusal_observation(args_refusal)
                messages.append({"role": "user", "content": observation})
                continue

            block_reason = _maybe_shared_write_refusal(parsed.tool, parsed.args, claims, self_id)
            if block_reason:
                _log("system", "claim_block", block_reason)
                saw_failed_shared_write = True
                observation = _refusal_observation(block_reason)
                messages.append({"role": "user", "content": observation})
                continue

            observation = _run_tool_with_approval(parsed.tool, parsed.args, console)
            observation = _truncate(observation)
            if (
                parsed.tool in CLAIM_GATED_TOOLS
                and isinstance(parsed.args.get("path"), str)
                and parsed.args["path"].startswith(SHARED_PATH_PREFIX)
                and _looks_like_failed_write(observation)
            ):
                saw_failed_shared_write = True
            _log(
                "tool",
                parsed.tool,
                _json({"args": parsed.args, "observation": observation}),
            )
            messages.append(
                {"role": "user", "content": _tool_observation_message(parsed.tool, observation)}
            )
            continue

        guidance = (
            "Your previous response was invalid. Respond with exactly one JSON object and no prose. "
            f"Parser error: {parsed.error}"
        )
        _log("system", "parser_guidance", guidance)
        messages.append({"role": "user", "content": guidance})

    fallback = "I could not complete this within my step budget. Please rephrase or split the task."
    scrubbed, _ = scrub_outbound(fallback)
    _log("assistant", "peer_reply_raw", fallback)
    return scrubbed
