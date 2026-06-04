"""Roster collection — attendance roll-call before work decomposition (P3).

When an agent acts as coordinator it broadcasts a ``[ROSTER]`` request and opens
a fixed collection window (``ROSTER_WINDOW_SECONDS``, default 60s). Peers answer
with a line of the form::

    [ROSTER] alice-swe | SWE agent | actions: bash, create_file, yield | backend: openrouter

After the window closes the coordinator proceeds with work decomposition using
*only* the agents that answered in time (firm window — late lines are ignored for
this round). Agents that never answer are simply not assigned work; their other
messages are still handled normally by the main loop.

This module is pure data + timing: no transport, no LLM. The main loop drives the
broadcast and the window; this module parses ``[ROSTER]`` lines, tracks who is
present, and answers ``is_open(now)`` / ``present()``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Sequence


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Length of the attendance window, in seconds. Configurable via .env so a demo
# can shorten it without code changes.
ROSTER_WINDOW_SECONDS = _env_float("ROSTER_WINDOW_SECONDS", 60.0)

# A [ROSTER] line: the literal tag followed by the rest of the line. Fields are
# pipe-delimited; the first field is the agent name, later fields may be a bare
# role or ``key: value`` pairs (``actions:`` / ``backend:``).
ROSTER_LINE_PATTERN = re.compile(r"(?im)^\s*\[ROSTER\]\s*(?P<body>.+?)\s*$")

# Placeholder tokens used in the request's example line. Peers receiving the
# request must not register the example as a real attendee.
_PLACEHOLDER_NAMES = {
    "your-agent-name",
    "<your-agent-name>",
    "your_agent_name",
    "name",
    "agent-name",
}
_PLACEHOLDER_BACKENDS = {"your-backend", "<your-backend>", "backend"}


@dataclass(frozen=True)
class RosterEntry:
    """One agent's declared capabilities, parsed from a ``[ROSTER]`` line."""

    agent_id: str  # transport sender id (authoritative), or the declared name
    name: str  # self-declared display name from the line
    role: str
    actions: tuple[str, ...]
    backend: str
    observed_at: float


def _split_actions(value: str) -> tuple[str, ...]:
    parts = [p.strip() for p in re.split(r"[,/]| and ", value) if p.strip()]
    return tuple(parts)


def parse_roster_line(body: str) -> tuple[str, str, tuple[str, ...], str] | None:
    """Parse the body after ``[ROSTER]`` into (name, role, actions, backend).

    Returns ``None`` when the line is a placeholder example or has no usable name.
    """

    segments = [seg.strip() for seg in body.split("|")]
    segments = [seg for seg in segments if seg]
    if not segments:
        return None

    name = segments[0].lstrip("@").strip()
    if not name or name.lower() in _PLACEHOLDER_NAMES:
        return None

    role = ""
    actions: tuple[str, ...] = ()
    backend = ""
    for seg in segments[1:]:
        key, sep, val = seg.partition(":")
        key_norm = key.strip().lower()
        if sep and key_norm in {"actions", "action", "tools"}:
            actions = _split_actions(val)
        elif sep and key_norm in {"backend", "provider", "model"}:
            backend = val.strip()
        elif not sep and not role:
            # A bare segment with no ``key:`` is the human-readable role.
            role = seg.strip()

    if backend.lower() in _PLACEHOLDER_BACKENDS:
        backend = ""
    return name, role, actions, backend


def parse_roster_lines(text: str, sender_id: str, now: float) -> list[RosterEntry]:
    """Extract every valid ``[ROSTER]`` entry from ``text``."""

    if not isinstance(text, str) or "[ROSTER]" not in text.upper():
        return []
    entries: list[RosterEntry] = []
    for match in ROSTER_LINE_PATTERN.finditer(text):
        parsed = parse_roster_line(match.group("body"))
        if parsed is None:
            continue
        name, role, actions, backend = parsed
        entries.append(
            RosterEntry(
                agent_id=(sender_id or name),
                name=name,
                role=role,
                actions=actions,
                backend=backend,
                observed_at=now,
            )
        )
    return entries


def build_roster_line(
    name: str,
    *,
    role: str = "SWE agent",
    actions: Sequence[str] = (),
    backend: str = "",
) -> str:
    """Render this agent's own ``[ROSTER]`` line for the broadcast."""

    parts = [name, role]
    if actions:
        parts.append("actions: " + ", ".join(actions))
    if backend:
        parts.append("backend: " + backend)
    return "[ROSTER] " + " | ".join(parts)


def roster_request_text(purpose: str | None = None, *, own_line: str | None = None) -> str:
    """Build the broadcast that asks every agent to post a ``[ROSTER]`` line.

    When ``own_line`` is given (the coordinator's own real ``[ROSTER]`` line) it
    is appended so peers count the coordinator as present and the coordinator can
    seed itself by observing its own broadcast.
    """

    purpose_clause = f" for {purpose}" if purpose else ""
    lines = [
        "@all agents: Please post your [ROSTER] line indicating your capabilities "
        "and actions, for example:",
        "[ROSTER] your-agent-name | SWE agent | actions: bash, create_file, "
        "edit_file_section, read_tool_output, yield | backend: your-backend",
        f"This will help facilitate work decomposition and task assignment{purpose_clause}.",
    ]
    if own_line:
        lines.append(own_line)
    return "\n".join(lines)


def roster_guidance(present: Sequence[RosterEntry]) -> str | None:
    """LLM guidance listing the agents who answered, for work decomposition."""

    if not present:
        return None
    lines = []
    for entry in present:
        actions = ", ".join(entry.actions) if entry.actions else "unspecified"
        role = entry.role or "agent"
        backend = entry.backend or "unspecified"
        lines.append(f"- @{entry.name} | {role} | actions: {actions} | backend: {backend}")
    roster_block = "\n".join(lines)
    return (
        "Roster window closed. The following agents answered the roll-call and are "
        "available for task assignment:\n"
        f"{roster_block}\n"
        "Decompose the work and assign tasks ONLY to these present agents. Do not "
        "assign work to, or block waiting on, agents that did not post a [ROSTER] line."
    )


class RosterRegistry:
    """Tracks the open attendance window and the set of agents who answered.

    The window is *firm*: ``observe`` only records lines while ``is_open(now)``,
    so a ``[ROSTER]`` line arriving after the deadline does not retroactively
    count for the round (the caller may ``open`` a fresh window for a later one).
    """

    def __init__(self, window_seconds: float | None = None) -> None:
        self.window_seconds = (
            ROSTER_WINDOW_SECONDS if window_seconds is None else float(window_seconds)
        )
        self._opened_at: float | None = None
        self._closed: bool = False
        self._entries: dict[str, RosterEntry] = {}

    @property
    def opened_at(self) -> float | None:
        return self._opened_at

    def open(self, now: float) -> None:
        self._opened_at = now
        self._closed = False
        self._entries = {}

    def deadline(self) -> float | None:
        if self._opened_at is None:
            return None
        return self._opened_at + self.window_seconds

    def is_open(self, now: float) -> bool:
        deadline = self.deadline()
        return deadline is not None and not self._closed and now < deadline

    def observe(self, sender_id: str, text: str, now: float) -> list[RosterEntry]:
        """Record any ``[ROSTER]`` lines in ``text`` if the window is open.

        Returns the newly added entries (empty when the window is closed, the
        text has no roster line, or the sender already answered).
        """

        if not self.is_open(now):
            return []
        added: list[RosterEntry] = []
        for entry in parse_roster_lines(text, sender_id, now):
            if entry.agent_id in self._entries:
                continue
            self._entries[entry.agent_id] = entry
            added.append(entry)
        return added

    def close(self, now: float | None = None) -> tuple[RosterEntry, ...]:
        self._closed = True
        return self.present()

    def present(self) -> tuple[RosterEntry, ...]:
        return tuple(sorted(self._entries.values(), key=lambda e: e.name.lower()))
