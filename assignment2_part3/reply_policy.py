"""N×M reply gate (P3.6).

Pure-function decision called before the LLM round-trip. Keeps the agent
quiet on irrelevant traffic so a group of N agents does not generate N×M
replies for M messages.

Rules, evaluated in order:

1. Direct address (`@<id>`, `@<display_name>`, or literal name) → reply.
2. Coordinator handoff prefix (`assigned: <id>`, `handoff -> <id>`) → reply.
3. Per-thread cooldown — if this agent replied within COOLDOWN_SECONDS → skip.
4. Broadcast question to everyone/anyone/all → reply only if this agent has
   replied fewer than MAX_BROADCAST_REPLIES times in BROADCAST_WINDOW_SECONDS.
5. Otherwise → skip.
"""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


COOLDOWN_SECONDS = _env_int("REPLY_COOLDOWN_SECONDS", 30)
MAX_BROADCAST_REPLIES = _env_int("REPLY_MAX_BROADCAST", 1)
BROADCAST_WINDOW_SECONDS = _env_int("REPLY_BROADCAST_WINDOW_SECONDS", 300)

BROADCAST_PATTERN = re.compile(r"(?i)\b(everyone|anyone|all\s+agents?|any\s+volunteers?|whoever)\b")
HANDOFF_PATTERN = re.compile(r"(?im)^\s*(assigned|handoff\s*->|task\s+for)\s*:?\s*(?P<target>[\w.-]+)")


@dataclass(frozen=True)
class ReplyDecision:
    respond: bool
    reason: str
    delay_seconds: float = 0.0


def _mentions(text: str, names: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for name in names:
        if not name:
            continue
        needle = name.lower()
        if f"@{needle}" in lowered:
            return True
        if re.search(rf"(?i)\b{re.escape(name)}\b", text):
            return True
    return False


def _coordinator_handoff(text: str, names: tuple[str, ...]) -> bool:
    match = HANDOFF_PATTERN.search(text)
    if not match:
        return False
    target = match.group("target").lower()
    return any(target == name.lower() for name in names if name)


def _replies_in_window(recent_replies, since: float) -> int:
    return sum(1 for ts, _ in recent_replies if ts >= since)


def _last_reply_age(recent_replies, now: float) -> float | None:
    if not recent_replies:
        return None
    return now - recent_replies[-1][0]


def should_reply(
    message,
    agent_id: str,
    display_name: str,
    recent_replies,
    *,
    now: float | None = None,
    rng: random.Random | None = None,
) -> ReplyDecision:
    """Decide whether to reply to a peer message.

    `message` must expose `.sender_id` and `.text`. `recent_replies` is a
    list of `(timestamp, message_id)` for this agent's outbound replies.
    """

    if now is None:
        now = time.time()
    if rng is None:
        rng = random

    if message.sender_id == agent_id:
        return ReplyDecision(False, "skipped: message is from this agent")

    names = (agent_id, display_name)

    # Coordinator handoff is a stronger signal than a passing mention,
    # so check it first — an "assigned: alice" line should be tagged
    # as a handoff even though it also contains the literal name.
    if _coordinator_handoff(message.text, names):
        return ReplyDecision(True, "coordinator handoff", delay_seconds=0.0)

    if _mentions(message.text, names):
        delay = rng.uniform(0.5, 1.5)
        return ReplyDecision(True, "directly addressed", delay_seconds=delay)

    last_age = _last_reply_age(recent_replies, now)
    if last_age is not None and last_age < COOLDOWN_SECONDS:
        return ReplyDecision(False, f"cooldown: last reply {last_age:.1f}s ago")

    if BROADCAST_PATTERN.search(message.text):
        broadcasts_recently = _replies_in_window(recent_replies, now - BROADCAST_WINDOW_SECONDS)
        if broadcasts_recently >= MAX_BROADCAST_REPLIES:
            return ReplyDecision(
                False,
                f"broadcast back-off: replied {broadcasts_recently} times in last "
                f"{BROADCAST_WINDOW_SECONDS}s",
            )
        delay = rng.uniform(0.5, 2.0)
        return ReplyDecision(True, "broadcast question", delay_seconds=delay)

    return ReplyDecision(False, "not addressed; not a broadcast")
