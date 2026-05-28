"""Reassemble multi-part inbound peer messages.

The transport-level auto-split (`transport._split_for_hub`) emits oversized
payloads as separate hub posts with a `(part i/N)\\n` header. LLMs sometimes
do the same thing voluntarily. Without reassembly, the receiver sees each
chunk as an independent `PeerMessage`, runs `should_reply` on every one of
them, and may react to half a payload.

`MultipartAssembler.feed` is the chokepoint. It returns the messages that
are ready for downstream processing — most of the time exactly one (the
input itself, untouched), occasionally zero (the part is buffered waiting
for siblings), occasionally several (a complete group plus the next
non-part message, or several timed-out groups).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

from peer import PeerMessage


PART_HEADER = re.compile(
    r"^\s*\(\s*part\s+(\d+)\s*/\s*(\d+)\s*\)\s*\n?",
    re.IGNORECASE,
)

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_PARTS = 20
DEFAULT_MAX_GROUPS_PER_SENDER = 4


@dataclass
class _PendingGroup:
    sender_id: str
    total: int
    parts: dict[int, tuple[PeerMessage, str]] = field(default_factory=dict)
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0


class MultipartAssembler:
    """Buffer `(part i/N)` sequences until the group is complete.

    Pure logic — no I/O, no threading. Wired into `group_chat` between
    `transport.recv` and the reply gate so multi-part traffic looks like
    a single inbound turn to everything downstream (claim parsing,
    reply policy, trace ids).
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_parts: int = DEFAULT_MAX_PARTS,
        max_groups_per_sender: int = DEFAULT_MAX_GROUPS_PER_SENDER,
    ):
        self._groups: dict[tuple[str, int], _PendingGroup] = {}
        self._timeout = timeout_seconds
        self._max_parts = max_parts
        self._max_groups_per_sender = max_groups_per_sender

    def feed(self, message: PeerMessage, now: float) -> list[PeerMessage]:
        """Process one inbound message and return what's ready downstream."""
        text = message.text or ""
        match = PART_HEADER.match(text)
        if match is None:
            # Sender broke off any in-flight split with an unrelated message.
            # Surface the partial groups first so the caller sees them in
            # arrival order, then the new message.
            flushed = self._flush_pending_for_sender(message.sender_id)
            flushed.append(message)
            return flushed

        idx = int(match.group(1))
        total = int(match.group(2))
        body = text[match.end():]

        if total < 1 or idx < 1 or idx > total or total > self._max_parts:
            # Header is malformed or hostile. Strip nothing, treat as plain.
            return [message]

        if total == 1:
            # Degenerate single-part message. Strip the header and deliver.
            return [replace(message, text=body)]

        key = (message.sender_id, total)
        group = self._groups.get(key)

        carried: list[PeerMessage] = []
        if group is None:
            # Enforce per-sender group cap by flushing the oldest.
            existing_keys = [k for k in self._groups if k[0] == message.sender_id]
            while len(existing_keys) >= self._max_groups_per_sender:
                oldest = min(existing_keys, key=lambda k: self._groups[k].first_seen_at)
                carried.append(_reassemble_incomplete(self._groups.pop(oldest)))
                existing_keys.remove(oldest)
            group = _PendingGroup(
                sender_id=message.sender_id,
                total=total,
                first_seen_at=now,
                last_seen_at=now,
            )
            self._groups[key] = group

        group.parts[idx] = (message, body)
        group.last_seen_at = now

        if len(group.parts) == total:
            del self._groups[key]
            carried.append(_reassemble(group))
            return carried
        return carried

    def flush_expired(self, now: float) -> list[PeerMessage]:
        """Emit incomplete groups older than the timeout."""
        out: list[PeerMessage] = []
        # Stable order: oldest first_seen_at first.
        stale_keys = [
            k for k, g in self._groups.items()
            if now - g.first_seen_at >= self._timeout
        ]
        stale_keys.sort(key=lambda k: self._groups[k].first_seen_at)
        for key in stale_keys:
            out.append(_reassemble_incomplete(self._groups.pop(key)))
        return out

    def pending_count(self) -> int:
        """Number of in-flight groups. Diagnostic only."""
        return len(self._groups)

    def _flush_pending_for_sender(self, sender_id: str) -> list[PeerMessage]:
        keys = [k for k in self._groups if k[0] == sender_id]
        keys.sort(key=lambda k: self._groups[k].first_seen_at)
        return [_reassemble_incomplete(self._groups.pop(k)) for k in keys]


def _reassemble(group: _PendingGroup) -> PeerMessage:
    ordered = [group.parts[i][1] for i in sorted(group.parts)]
    text = "\n".join(ordered)
    first_msg = group.parts[min(group.parts)][0]
    last_received_at = max(group.parts[i][0].received_at for i in group.parts)
    return replace(first_msg, text=text, received_at=last_received_at)


def _reassemble_incomplete(group: _PendingGroup) -> PeerMessage:
    received = sorted(group.parts)
    received_str = ", ".join(str(i) for i in received) if received else "none"
    marker = (
        f"[incomplete multi-part message from {group.sender_id}: "
        f"received parts {received_str} of {group.total}]"
    )
    ordered_bodies = [group.parts[i][1] for i in received]
    body_text = "\n".join(ordered_bodies)
    text = f"{marker}\n{body_text}" if body_text else marker
    # We always have at least one part by the time we reach this helper —
    # _PendingGroup is only created at the moment a part is registered.
    first_msg = group.parts[min(group.parts)][0]
    last_received_at = max(group.parts[i][0].received_at for i in group.parts)
    return replace(first_msg, text=text, received_at=last_received_at)
