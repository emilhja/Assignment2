from __future__ import annotations

import pytest

from task_status import parse_task_status


@pytest.mark.parametrize(
    ("text", "kind", "task", "language"),
    [
        ("Jag tar mig an: terminal-kalkylator", "taking", "terminal-kalkylator", "sv"),
        ("Bekräftat, jag tar: terminal-kalkylator", "accepted", "terminal-kalkylator", "sv"),
        ("Bekräftat, jag tar terminal-kalkylator", "accepted", "terminal-kalkylator", "sv"),
        ("Klar med: terminal-kalkylator", "done", "terminal-kalkylator", "sv"),
        ("I'm taking on: terminal calculator", "taking", "terminal calculator", "en"),
        ("I am taking on: terminal calculator", "taking", "terminal calculator", "en"),
        ("Confirmed, I'll take: terminal calculator", "accepted", "terminal calculator", "en"),
        ("Confirmed, I will take: terminal calculator", "accepted", "terminal calculator", "en"),
        ("Done with: terminal calculator", "done", "terminal calculator", "en"),
    ],
)
def test_parse_task_status_phrases(text, kind, task, language):
    parsed = parse_task_status(text)

    assert parsed is not None
    assert parsed.kind == kind
    assert parsed.task == task
    assert parsed.language == language


def test_parse_task_status_uses_first_non_empty_line():
    parsed = parse_task_status("\n\nBekräftat, jag tar: kalkylator\nMer text")

    assert parsed is not None
    assert parsed.kind == "accepted"
    assert parsed.task == "kalkylator"


def test_parse_task_status_rejects_plain_prose():
    assert parse_task_status("Jag ska skapa en kalkylator.") is None
