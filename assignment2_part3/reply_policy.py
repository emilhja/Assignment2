"""N×M reply gate (P3.6).

Pure-function decision called before the LLM round-trip. Keeps the agent
quiet on irrelevant traffic so a group of N agents does not generate N×M
replies for M messages.

Rules, evaluated in order:

1. Direct address (`@<id>`, `@<display_name>`, or literal name) → reply.
2. Coordinator handoff prefix (`assigned: <id>`, `handoff -> <id>`) → reply.
3. Claim collision — incoming message contains a peer CLAIM for a path
   this agent already self-claimed → reply (bypasses cooldown so the
   tie-break/DEFER line can actually leave).
4. Per-thread cooldown — if this agent replied within COOLDOWN_SECONDS → skip.
5. Broadcast question to everyone/anyone/all → reply only if this agent has
   replied fewer than MAX_BROADCAST_REPLIES times in BROADCAST_WINDOW_SECONDS.
6. Otherwise → skip.
"""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from typing import Optional

from claims import (
    CLAIM_PATTERN,
    Claim,
    ClaimRegistry,
    DEFER_PATTERN,
    RELEASE_PATTERN,
    claims_conflict,
    split_claim_target,
    tie_break_winner,
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


COOLDOWN_SECONDS = _env_int("REPLY_COOLDOWN_SECONDS", 8)
MAX_BROADCAST_REPLIES = _env_int("REPLY_MAX_BROADCAST", 1)
BROADCAST_WINDOW_SECONDS = _env_int("REPLY_BROADCAST_WINDOW_SECONDS", 300)

BROADCAST_PATTERN = re.compile(
    r"(?i)\b("
    # Explicit typo set for "agents" so a fat-fingered roll-call still
    # triggers the broadcast branch. Kept narrow on purpose — wider
    # patterns like `all\s+\w+ents?` falsely match "all events".
    r"everyone|anyone"
    r"|all\s+(?:agents?|egents?|agnets?|agnts?|aents?|agets?|agetns?)"
    r"|any\s+volunteers?|whoever"
    r"|alla|någon|vem\s+som\s+helst|alla\s+agenter|volontär(?:er)?"
    r")\b"
)
HANDOFF_PATTERN = re.compile(
    r"(?im)^\s*(?:"
    r"(?:assigned|handoff\s*->|task\s+for)\s*:?\s*@?(?P<legacy_target>[\w.-]+)"
    r"|task\s+@?(?P<task_target>[\w.-]+)\s*:"
    r")"
)


@dataclass(frozen=True)
class CollisionInfo:
    """Description of a racing-CLAIM collision the runtime detected.

    `outcome` is "self-wins" when the receiving agent owns the lexicographic
    tie-break and should proceed, "self-loses" when the agent must defer and
    release. This is computed once in the policy gate so the LLM does not
    have to apply the rule itself.
    """

    path: str
    peer_id: str
    outcome: str  # "self-wins" | "self-loses"


@dataclass(frozen=True)
class ReplyDecision:
    respond: bool
    reason: str
    delay_seconds: float = 0.0
    collision: Optional[CollisionInfo] = None


def _strip_protocol_lines(text: str) -> str:
    """Remove DEFER/RELEASE lines so their @mentions don't trigger a reply.

    A peer's "DEFER to @you" is a one-way acknowledgment; treating the @
    inside it as a real address creates a tight ping-pong loop between
    two agents that exhausts their token budgets.
    """

    if not isinstance(text, str) or not text:
        return text
    stripped = DEFER_PATTERN.sub("", text)
    stripped = RELEASE_PATTERN.sub("", stripped)
    return stripped


def _mentions(text: str, names: tuple[str, ...]) -> bool:
    if not names:
        return False
    lowered = text.lower()
    agent_id = names[0]
    # display_name + any aliases — all are human-friendly handles that
    # show up bare or @-prefixed in chat.
    human_names = names[1:]
    for name in names:
        if name and re.search(rf"(?i)@{re.escape(name)}(?![\w-])", text):
            return True
    for name in human_names:
        if name and re.search(rf"(?i)(?<![@\w-]){re.escape(name)}(?![\w-])", text):
            return True
    if agent_id and re.search(rf"(?i)^\s*{re.escape(agent_id)}\b\s*[:,\-]", text):
        return True
    return False


def _coordinator_handoff(text: str, names: tuple[str, ...]) -> bool:
    match = HANDOFF_PATTERN.search(text)
    if not match:
        return False
    target = (match.group("legacy_target") or match.group("task_target") or "").lower()
    return any(target == name.lower() for name in names if name)


def _replies_in_window(recent_replies, since: float) -> int:
    return sum(1 for ts, _ in recent_replies if ts >= since)


def _last_reply_age(recent_replies, now: float) -> float | None:
    if not recent_replies:
        return None
    return now - recent_replies[-1][0]


def _claim_collision(
    text: str,
    agent_id: str,
    peer_id: str,
    claims: Optional[ClaimRegistry],
) -> Optional[CollisionInfo]:
    """Return collision info if the incoming text races a CLAIM we own.

    The outcome is decided here so downstream code (peer_task) can inject
    deterministic tie-break guidance instead of leaving the lexicographic
    rule to the LLM.
    """

    if claims is None or not isinstance(text, str):
        return None
    own_claims = claims.active_claims_for(agent_id)
    if not own_claims:
        return None
    for match in CLAIM_PATTERN.finditer(text):
        incoming_path, incoming_scope = split_claim_target(match.group("path"))
        # Mirror is_claimed_by_other(): whole-file × scoped peer-claim must
        # collide, which the prior scope-strict lookup(path#scope) missed.
        incoming = Claim(
            path=incoming_path,
            scope=incoming_scope,
            claimant=peer_id,
            claimed_at=0.0,
        )
        for own in own_claims:
            if claims_conflict(own, incoming):
                winner = tie_break_winner(agent_id, peer_id)
                outcome = "self-wins" if winner == agent_id else "self-loses"
                return CollisionInfo(path=own.target, peer_id=peer_id, outcome=outcome)
    return None


def should_reply(
    message,
    agent_id: str,
    display_name: str,
    recent_replies,
    *,
    now: float | None = None,
    rng: random.Random | None = None,
    claims: Optional[ClaimRegistry] = None,
    aliases: tuple[str, ...] = (),
) -> ReplyDecision:
    """Decide whether to reply to a peer message.

    `message` must expose `.sender_id` and `.text`. `recent_replies` is a
    list of `(timestamp, message_id)` for this agent's outbound replies.
    `aliases` is an optional tuple of extra handles this agent should also
    respond to (e.g. a human's real name) — matched the same way as
    `display_name`.
    """

    if now is None:
        now = time.time()
    if rng is None:
        rng = random

    if message.sender_id == agent_id:
        return ReplyDecision(False, "skipped: message is from this agent")

    names = (agent_id, display_name, *aliases)

    # Coordinator handoff is a stronger signal than a passing mention,
    # so check it first — an "assigned: alice" line should be tagged
    # as a handoff even though it also contains the literal name.
    if _coordinator_handoff(message.text, names):
        return ReplyDecision(True, "coordinator handoff", delay_seconds=0.0)

    if _mentions(_strip_protocol_lines(message.text), names):
        delay = rng.uniform(0.5, 1.5)
        return ReplyDecision(True, "directly addressed", delay_seconds=delay)

    # Claim collision wins over the cooldown gate — otherwise two agents
    # who emit competing CLAIMs in the same round stay stuck.
    collision = _claim_collision(message.text, agent_id, message.sender_id, claims)
    if collision is not None:
        return ReplyDecision(
            True,
            f"claim collision on {collision.path} ({collision.outcome})",
            delay_seconds=0.0,
            collision=collision,
        )

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
