from __future__ import annotations

import pytest

from task_status import looks_like_empty_acknowledgment, parse_task_status


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


@pytest.mark.parametrize(
    "text",
    [
        "Okej, jag förstår. Jag avvaktar nya instruktioner.",
        "Okej, jag förstår att jag ska använda filen. Jag avvaktar nästa instruktion.",
        "I will await the coordinator's task distribution.",
        "Ready for next task.",
        "Tack!",
        "Noted.",
    ],
)
def test_empty_acknowledgment_is_detected(text):
    assert looks_like_empty_acknowledgment(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Klar med: terminal-kalkylator. Filer: /workspace/alice/project1/calc.py. Tester: passed.",
        "CLAIM /workspace/shared/calc.py#add: implement add",
        "RELEASE /workspace/shared/calc.py#add",
        "Done: implemented add at /workspace/shared/calc.py. Tests: ran and passed.",
        "Should I create the README file?",
        "Bekräftat, jag tar: terminal-kalkylator",
        "Here is the code:\n```python\ndef add(a, b): return a+b\n```",
    ],
)
def test_empty_acknowledgment_skips_substantive_replies(text):
    assert looks_like_empty_acknowledgment(text) is False


def test_empty_acknowledgment_skips_long_text():
    # An ack-like phrase buried in a very long reply is no longer "empty".
    text = "Okej, jag förstår. " + ("Lorem ipsum dolor sit amet. " * 20)
    assert looks_like_empty_acknowledgment(text) is False
