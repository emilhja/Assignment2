"""Runtime tracking for optimistic coordinator/manager role claims."""

from __future__ import annotations

import re
from dataclasses import dataclass


ROLE_WORD_PATTERN = re.compile(
    r"(?i)\b("
    r"manager|coordinator|lead|coordination|"
    r"samordnare|projektledare|chef(?:sposition(?:en)?)?|chefsrollen|managerrollen"
    r")\b"
)

_ENGLISH_CLAIM_PATTERN = re.compile(
    r"(?is)\b(?:"
    r"i\s+(?:am|'m|will\s+be)|i'll\s+be|"
    r"i\s+(?:take|claim|accept|assume)|"
    r"confirmed,\s*(?:i'll|i\s+will)\s+take"
    r")\b.{0,80}"
    r"(?:manager|coordinator|lead|coordination)\b"
)
_SWEDISH_CLAIM_PATTERN = re.compile(
    r"(?is)\b(?:"
    r"jag\s+(?:är|ar)|"
    r"jag\s+(?:tar|antar|accepterar)|"
    r"bekräftat,\s*jag\s+tar"
    r")\b.{0,80}"
    r"(?:manager|coordinator|lead|coordination|samordnare|projektledare|"
    r"chef(?:sposition(?:en)?)?|chefsrollen|managerrollen)\b"
)
_NEGATED_CLAIM_PATTERN = re.compile(
    r"(?is)\b(?:i\s+am\s+not|i'm\s+not|jag\s+är\s+inte|jag\s+ar\s+inte)\b"
    r".{0,40}"
    r"(?:manager|coordinator|lead|samordnare|projektledare|chef)\b"
)


@dataclass(frozen=True)
class RoleClaim:
    sender_id: str
    message_id: str
    role: str
    order: int


def role_claim_from_text(
    sender_id: str,
    text: str,
    message_id: str,
    order: int,
) -> RoleClaim | None:
    """Return a first-person coordination-role claim, if present."""

    if not isinstance(text, str) or not text.strip():
        return None
    if _NEGATED_CLAIM_PATTERN.search(text):
        return None
    if not (
        _ENGLISH_CLAIM_PATTERN.search(text)
        or _SWEDISH_CLAIM_PATTERN.search(text)
    ):
        return None
    role_match = ROLE_WORD_PATTERN.search(text)
    if role_match is None:
        return None
    role = role_match.group(1).lower()
    if role.startswith("chef") or role in {"samordnare", "projektledare"}:
        role = "manager"
    elif role == "coordination":
        role = "coordinator"
    return RoleClaim(
        sender_id=sender_id,
        message_id=message_id,
        role=role,
        order=order,
    )


class RoleElectionState:
    """Observed role claims plus quiet-worker state after a handover.

    The live hub does not expose the sequence id of this agent's outbound
    message, because the transport intentionally skips self-echoes. The safe
    runtime rule is therefore conservative: after a self role claim, any peer
    claim observed during the reconciliation window wins the role.
    """

    def __init__(self) -> None:
        self._order = 0
        self._claims: list[RoleClaim] = []
        self.pending_self_claim: RoleClaim | None = None
        self.quiet_after_loss = False
        self.leader_id: str | None = None

    def observe(self, sender_id: str, text: str, message_id: str) -> RoleClaim | None:
        self._order += 1
        claim = role_claim_from_text(sender_id, text, message_id, self._order)
        if claim is not None:
            self._claims.append(claim)
        return claim

    def observe_self(
        self,
        sender_id: str,
        text: str,
        message_id: str,
    ) -> RoleClaim | None:
        claim = self.observe(sender_id, text, message_id)
        if claim is not None:
            self.pending_self_claim = claim
            self.quiet_after_loss = False
            self.leader_id = sender_id
        return claim

    def peer_winner_for_pending(self, self_id: str) -> RoleClaim | None:
        if self.pending_self_claim is None:
            return None
        self_norm = (self_id or "").lower()
        peers = [
            claim for claim in self._claims
            if claim.sender_id.lower() != self_norm
        ]
        if not peers:
            return None
        return min(peers, key=lambda claim: claim.order)

    def finish_pending(self, winner: RoleClaim | None, self_id: str) -> None:
        if self.pending_self_claim is None:
            return
        if winner is not None and winner.sender_id.lower() != (self_id or "").lower():
            self.quiet_after_loss = True
            self.leader_id = winner.sender_id
        else:
            self.leader_id = self_id
        self.pending_self_claim = None
