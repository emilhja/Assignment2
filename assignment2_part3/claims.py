"""Per-agent claim registry for cooperative file work.

Each agent runs its own registry. When another agent posts
`CLAIM /workspace/shared/<path>` in chat, this agent records that
claim with a TTL. Before invoking a write/edit tool targeting that
path, the agent consults the registry; if the path is claimed by a
different agent and the claim has not expired, the write is denied
and the agent is nudged to post `DEFER to @<agent>` instead.

The registry intentionally lives in process memory only. Claims are
short-lived coordination hints, not durable locks — the hub-side
ordering remains the source of truth.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional


DEFAULT_TTL_SECONDS = 300.0
DEFAULT_DEFER_WINDOW_SECONDS = 120.0

CLAIM_PATTERN = re.compile(
    r"(?im)^\s*CLAIM\s+(?P<path>/workspace/shared/[^\s:]+)\s*(?::\s*(?P<reason>.+))?$"
)
RELEASE_PATTERN = re.compile(
    r"(?im)^\s*RELEASE\s+(?P<path>/workspace/shared/[^\s:]+)\s*$"
)
DEFER_PATTERN = re.compile(
    r"(?im)^\s*DEFER\s+to\s+@?(?P<target>[A-Za-z0-9_.\-]+)"
)


def tie_break_winner(id_a: str, id_b: str) -> str:
    """Return the lexicographically smaller AGENT_ID. Single source of truth
    for the P3.9 racing-CLAIM rule referenced in `config/system_prompt.txt`.
    """

    a = (id_a or "").lower()
    b = (id_b or "").lower()
    return id_a if a <= b else id_b


@dataclass(frozen=True)
class Claim:
    path: str
    claimant: str
    claimed_at: float
    reason: str = ""
    scope: str | None = None

    @property
    def target(self) -> str:
        if self.scope:
            return f"{self.path}#{self.scope}"
        return self.path


def split_claim_target(target: str) -> tuple[str, str | None]:
    """Return the base shared path and optional coordination scope."""

    normalized = target.strip().rstrip("/")
    path, marker, scope = normalized.partition("#")
    scope_norm = scope.strip() if marker else None
    return path.rstrip("/"), scope_norm or None


def _claim_key(path: str, scope: str | None = None) -> str:
    return f"{path}#{scope}" if scope else path


def claims_conflict(first: Claim, second: Claim) -> bool:
    """Whole-file claims conflict with every scope; scopes conflict by name."""

    if first.path != second.path:
        return False
    if first.scope is None or second.scope is None:
        return True
    return first.scope == second.scope


class ClaimRegistry:
    """Thread-safe in-memory claim registry.

    `record_observed` adds a claim seen in chat from any sender.
    `is_claimed_by_other` answers the gate question. Claims expire
    after `ttl_seconds` so a crashed agent does not freeze the path
    forever.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock=time.monotonic,
        defer_window_seconds: float = DEFAULT_DEFER_WINDOW_SECONDS,
    ):
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._claims: dict[str, Claim] = {}
        self._defer_window = float(defer_window_seconds)
        # (deferrer_id_lower, target_id_lower) -> last observed timestamp
        self._defers: dict[tuple[str, str], float] = {}
        self._lock = Lock()

    def record_observed(self, claimant: str, path: str, reason: str = "") -> Claim:
        path_norm, scope = split_claim_target(path)
        claim = Claim(
            path=path_norm,
            scope=scope,
            claimant=claimant,
            claimed_at=self._clock(),
            reason=reason,
        )
        with self._lock:
            self._claims[_claim_key(path_norm, scope)] = claim
        return claim

    def release(self, claimant: str, path: str) -> bool:
        path_norm, scope = split_claim_target(path)
        released = False
        with self._lock:
            if scope is None:
                keys = [
                    key
                    for key, existing in self._claims.items()
                    if existing.path == path_norm and existing.claimant == claimant
                ]
            else:
                keys = [_claim_key(path_norm, scope)]
            for key in keys:
                existing = self._claims.get(key)
                if existing is not None and existing.claimant == claimant:
                    del self._claims[key]
                    released = True
        return released

    def lookup(self, path: str) -> Optional[Claim]:
        path_norm, scope = split_claim_target(path)
        key = _claim_key(path_norm, scope)
        with self._lock:
            claim = self._claims.get(key)
            if claim is None:
                return None
            if self._clock() - claim.claimed_at > self._ttl:
                del self._claims[key]
                return None
            return claim

    def _active_claims_locked(self) -> list[Claim]:
        now = self._clock()
        expired = [
            key for key, claim in self._claims.items()
            if now - claim.claimed_at > self._ttl
        ]
        for key in expired:
            del self._claims[key]
        return list(self._claims.values())

    def is_claimed_by_other(self, path: str, self_id: str) -> Optional[Claim]:
        path_norm, scope = split_claim_target(path)
        candidate = Claim(
            path=path_norm,
            scope=scope,
            claimant=self_id,
            claimed_at=0.0,
        )
        with self._lock:
            for claim in self._active_claims_locked():
                if claim.claimant != self_id and claims_conflict(candidate, claim):
                    return claim
        return None

    def own_claim_for_write(self, path: str, self_id: str) -> Optional[Claim]:
        path_norm, _scope = split_claim_target(path)
        with self._lock:
            for claim in self._active_claims_locked():
                if claim.path == path_norm and claim.claimant == self_id:
                    return claim
        return None

    def active_claims_for(self, claimant: str) -> list[Claim]:
        with self._lock:
            return [
                claim for claim in self._active_claims_locked()
                if claim.claimant == claimant
            ]

    def absorb_text(self, sender_id: str, text: str) -> list[Claim]:
        """Scan one message for CLAIM/RELEASE/DEFER markers; update state.

        Returns the list of new claims recorded (useful for tests and logs).
        DEFER lines are tracked separately so the runtime can detect
        mutual-defer deadlocks; RELEASE clears the sender's outstanding
        defers so an agent that releases and moves on is not still
        considered "currently deferring."
        """

        if not isinstance(text, str) or not text:
            return []

        recorded: list[Claim] = []
        for match in CLAIM_PATTERN.finditer(text):
            reason = (match.group("reason") or "").strip()
            claim = self.record_observed(sender_id, match.group("path"), reason)
            recorded.append(claim)
        for match in DEFER_PATTERN.finditer(text):
            self.record_defer(sender_id, match.group("target"))
        if RELEASE_PATTERN.search(text):
            for match in RELEASE_PATTERN.finditer(text):
                self.release(sender_id, match.group("path"))
            self._clear_defers_for(sender_id)
        return recorded

    def record_defer(self, deferrer: str, target: str) -> None:
        """Record that `deferrer` posted a DEFER to `target`."""

        deferrer_norm = (deferrer or "").lower()
        target_norm = (target or "").lstrip("@").lower()
        if not deferrer_norm or not target_norm:
            return
        with self._lock:
            self._defers[(deferrer_norm, target_norm)] = self._clock()

    def _defer_recent_locked(self, deferrer: str, target: str) -> bool:
        ts = self._defers.get(((deferrer or "").lower(), (target or "").lower()))
        if ts is None:
            return False
        return (self._clock() - ts) <= self._defer_window

    def mutual_defer_detected(self, self_id: str, peer_id: str) -> bool:
        """True iff each side has DEFERred to the other within the window."""

        if not self_id or not peer_id:
            return False
        with self._lock:
            return (
                self._defer_recent_locked(self_id, peer_id)
                and self._defer_recent_locked(peer_id, self_id)
            )

    def _clear_defers_for(self, agent_id: str) -> None:
        norm = (agent_id or "").lower()
        if not norm:
            return
        with self._lock:
            stale = [key for key in self._defers if key[0] == norm or key[1] == norm]
            for key in stale:
                del self._defers[key]

    def clear_defers_between(self, agent_a: str, agent_b: str) -> None:
        a = (agent_a or "").lower()
        b = (agent_b or "").lower()
        if not a or not b:
            return
        with self._lock:
            self._defers.pop((a, b), None)
            self._defers.pop((b, a), None)
