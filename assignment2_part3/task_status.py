"""Parse lightweight visible task-status phrases for hub chat."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class TaskStatus:
    kind: str  # "taking", "accepted", or "done"
    task: str
    language: str  # "sv" or "en"


_STATUS_PATTERNS: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "taking",
        "sv",
        "Jag tar mig an",
        re.compile(r"^\s*Jag\s+tar\s+mig\s+an:\s*(?P<task>\S.*)\s*$", re.IGNORECASE),
    ),
    (
        "accepted",
        "sv",
        "Bekraftat jag tar",
        re.compile(
            r"^\s*Bekräftat,\s*jag\s+tar(?::|\s)\s*(?P<task>\S.*)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "done",
        "sv",
        "Klar med",
        re.compile(r"^\s*Klar\s+med:\s*(?P<task>\S.*)\s*$", re.IGNORECASE),
    ),
    (
        "taking",
        "en",
        "I am taking on",
        re.compile(
            r"^\s*(?:I'm|I\s+am)\s+taking\s+on:\s*(?P<task>\S.*)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "accepted",
        "en",
        "Confirmed I will take",
        re.compile(
            r"^\s*Confirmed,\s*(?:I'll|I\s+will)\s+take:\s*(?P<task>\S.*)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "done",
        "en",
        "Done with",
        re.compile(r"^\s*Done\s+with:\s*(?P<task>\S.*)\s*$", re.IGNORECASE),
    ),
)


def parse_task_status(text: str) -> TaskStatus | None:
    """Return a parsed status phrase from the first non-empty line."""

    if not text:
        return None
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return None
    for kind, language, _label, pattern in _STATUS_PATTERNS:
        match = pattern.match(first_line)
        if match:
            task = match.group("task").strip()
            if task:
                return TaskStatus(kind=kind, task=task, language=language)
    return None


# Markers that indicate the reply is a passive acknowledgment without unique
# technical value. Phrases like "jag avvaktar" / "awaiting" are also handled
# here (instead of the stall-guard in peer_task) so the runtime can suppress
# such replies entirely rather than reprompting and pushing the model to
# claim an action it does not have.
_EMPTY_ACK_MARKERS: tuple[str, ...] = (
    "okej",
    "ok,",
    "ok.",
    "ok!",
    "okay",
    "förstår",
    "jag avvaktar",
    "jag väntar",
    "jag inväntar",
    "awaiting",
    "i await",
    "i'll await",
    "i will await",
    "i'm awaiting",
    "i am awaiting",
    "ready for next task",
    "redo för nästa",
    "tack",
    "thanks",
    "noted",
)


# Markers that indicate the reply DOES carry substantive content even if it
# also contains acknowledgment language — never suppress these.
_EMPTY_ACK_NEGATIVE_MARKERS: tuple[str, ...] = (
    "/workspace/",
    "```",
    "claim ",
    "release ",
    "defer ",
    "klar med:",
    "done with:",
    "jag tar mig an:",
    "i'm taking on:",
    "i am taking on:",
    "bekräftat, jag tar",
    "confirmed, i'll take",
    "confirmed, i will take",
    "blockers:",
    "tests:",
)


def looks_like_empty_acknowledgment(text: str, *, max_chars: int = 240) -> bool:
    """Return True if the reply is a content-free acknowledgment.

    Suppression target: short prose like "Okej, jag förstår. Jag avvaktar nya
    instruktioner." that wastes a broadcast-window slot and adds no
    information. Negative markers (paths, code fences, CLAIM/RELEASE/DEFER,
    task-status phrases, Blockers/Tests lines, a trailing `?`) opt the reply
    out of suppression.
    """

    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > max_chars:
        return False
    if stripped.endswith("?"):
        return False
    lowered = stripped.lower()
    if any(marker in lowered for marker in _EMPTY_ACK_NEGATIVE_MARKERS):
        return False
    return any(marker in lowered for marker in _EMPTY_ACK_MARKERS)
