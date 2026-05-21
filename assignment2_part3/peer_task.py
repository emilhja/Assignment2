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

import json
import threading
from typing import Optional

import part2_bridge  # noqa: F401 — sys.path side effect; needed before Part 2 imports

from llm_client import complete_chat
from parser import parse_response
from session_store import SessionStore
from tools import MAX_OUTPUT_CHARS, TOOL_REGISTRY, run_tool

from budget import Budget, BudgetExceeded, estimate_tokens
from console_control import ConsoleControl
from peer import PeerMessage, peer_intent_refusal, scrub_outbound


MAX_STEPS = 8


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


def _refusal_observation(reason: str) -> str:
    return _json({"type": "tool_observation", "tool": "policy", "observation": f"refused: {reason}"})


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


def run_peer_task(
    message: PeerMessage,
    *,
    store: SessionStore,
    budget: Budget,
    system_prompt: str,
    console: Optional[ConsoleControl] = None,
    chat_fn=None,
    budget_save_event: Optional[threading.Event] = None,
) -> str:
    # Late binding so monkey-patching `peer_task.complete_chat` in tests works.
    if chat_fn is None:
        chat_fn = complete_chat
    """Handle one peer message and return the text to send back to the hub.

    The return value has already been passed through `scrub_outbound`.
    """

    store.record("peer", "message", _json({"sender_id": message.sender_id, "text": message.text}))

    refusal = peer_intent_refusal(message.text)
    if refusal:
        store.record("assistant", "peer_refusal", refusal)
        return refusal

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _peer_user_envelope(message)},
    ]

    for step in range(1, MAX_STEPS + 1):
        estimate = estimate_tokens(_json({"messages": messages}))
        try:
            budget.permit(estimate)
        except BudgetExceeded as exc:
            store.record("system", "budget_exceeded", exc.reason)
            return f"I have to stop here: my session budget is exhausted ({exc.reason})."

        raw_response = chat_fn(messages)
        budget.record(estimate_tokens(raw_response or ""))
        store.record("assistant", "raw_json", raw_response)
        if budget_save_event is not None:
            budget_save_event.set()

        messages.append({"role": "assistant", "content": raw_response})
        parsed = parse_response(raw_response, allowed_tools=TOOL_REGISTRY.keys())

        if parsed.kind == "final":
            answer = parsed.answer or ""
            scrubbed, hits = scrub_outbound(answer)
            store.record("assistant", "peer_reply_raw", answer)
            if hits:
                store.record("assistant", "peer_reply_scrubbed", _json({"hits": hits, "text": scrubbed}))
            return scrubbed

        if parsed.kind == "tool_call":
            args_refusal = _maybe_scrub_args_refusal(parsed.args)
            if args_refusal:
                store.record("system", "peer_refusal_tool_args", args_refusal)
                observation = _refusal_observation(args_refusal)
                messages.append({"role": "user", "content": observation})
                continue

            observation = _run_tool_with_approval(parsed.tool, parsed.args, console)
            observation = _truncate(observation)
            store.record(
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
        store.record("system", "parser_guidance", guidance)
        messages.append({"role": "user", "content": guidance})

    fallback = "I could not complete this within my step budget. Please rephrase or split the task."
    scrubbed, _ = scrub_outbound(fallback)
    store.record("assistant", "peer_reply_raw", fallback)
    return scrubbed
