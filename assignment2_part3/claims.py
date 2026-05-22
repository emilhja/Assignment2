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

CLAIM_PATTERN = re.compile(
    r"(?im)^\s*CLAIM\s+(?P<path>/workspace/shared/[^\s:]+)\s*(?::\s*(?P<reason>.+))?$"
)
RELEASE_PATTERN = re.compile(
    r"(?im)^\s*RELEASE\s+(?P<path>/workspace/shared/[^\s:]+)\s*$"
)


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

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, clock=time.monotonic):
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._claims: dict[str, Claim] = {}
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
        """Scan one message for CLAIM/RELEASE markers; update state.

        Returns the list of new claims recorded (useful for tests and logs).
        """

        if not isinstance(text, str) or not text:
            return []

        recorded: list[Claim] = []
        for match in CLAIM_PATTERN.finditer(text):
            reason = (match.group("reason") or "").strip()
            claim = self.record_observed(sender_id, match.group("path"), reason)
            recorded.append(claim)
        for match in RELEASE_PATTERN.finditer(text):
            self.release(sender_id, match.group("path"))
        return recorded
