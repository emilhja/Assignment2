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
