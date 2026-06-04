"""Unit tests for the roster roll-call module (P3)."""

from roster import (
    RosterRegistry,
    build_roster_line,
    parse_roster_line,
    parse_roster_lines,
    roster_guidance,
    roster_request_text,
)


def test_parse_roster_line_full():
    name, role, actions, backend = parse_roster_line(
        "alice-swe | SWE agent | actions: bash, create_file, yield | backend: openrouter"
    )
    assert name == "alice-swe"
    assert role == "SWE agent"
    assert actions == ("bash", "create_file", "yield")
    assert backend == "openrouter"


def test_parse_roster_line_strips_at_and_handles_minimal():
    name, role, actions, backend = parse_roster_line("@bob-swe")
    assert name == "bob-swe"
    assert role == ""
    assert actions == ()
    assert backend == ""


def test_parse_roster_line_rejects_placeholder():
    assert parse_roster_line("your-agent-name | SWE agent | backend: your-backend") is None


def test_parse_roster_lines_extracts_from_chat():
    text = "Sure thing!\n[ROSTER] carol | tester | actions: pytest | backend: groq\nthanks"
    entries = parse_roster_lines(text, sender_id="carol-id", now=10.0)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.agent_id == "carol-id"
    assert entry.name == "carol"
    assert entry.actions == ("pytest",)
    assert entry.observed_at == 10.0


def test_parse_roster_lines_no_tag_returns_empty():
    assert parse_roster_lines("just a normal message", "x", 1.0) == []


def test_registry_collects_within_window():
    reg = RosterRegistry(window_seconds=60)
    reg.open(now=100.0)
    assert reg.is_open(now=100.0)
    added = reg.observe("alice", "[ROSTER] alice | swe | actions: bash", now=110.0)
    assert [e.name for e in added] == ["alice"]
    reg.observe("bob", "[ROSTER] bob | swe | actions: bash", now=120.0)
    present = reg.close(now=160.0)
    assert sorted(e.name for e in present) == ["alice", "bob"]


def test_registry_window_is_firm():
    reg = RosterRegistry(window_seconds=60)
    reg.open(now=100.0)
    # 61s later the window has closed; a late line must not register.
    assert reg.is_open(now=161.0) is False
    added = reg.observe("late", "[ROSTER] late | swe", now=161.0)
    assert added == []
    assert reg.present() == ()


def test_registry_dedupes_repeat_sender():
    reg = RosterRegistry(window_seconds=60)
    reg.open(now=0.0)
    reg.observe("alice", "[ROSTER] alice | swe | actions: bash", now=1.0)
    again = reg.observe("alice", "[ROSTER] alice | swe | actions: bash, edit", now=2.0)
    assert again == []
    assert len(reg.present()) == 1


def test_registry_closed_before_open_reports_not_open():
    reg = RosterRegistry(window_seconds=60)
    assert reg.is_open(now=5.0) is False
    assert reg.deadline() is None
    assert reg.observe("x", "[ROSTER] x | swe", now=5.0) == []


def test_request_text_includes_example_and_own_line():
    own = build_roster_line("alice-swe", actions=("bash", "yield"), backend="local")
    text = roster_request_text("the React calculator app", own_line=own)
    assert "[ROSTER] your-agent-name" in text  # example for peers
    assert own in text
    assert "React calculator app" in text


def test_request_example_is_not_parsed_as_attendee():
    # A peer receiving the broadcast must not register the example placeholder.
    text = roster_request_text("x", own_line=build_roster_line("alice", backend="local"))
    reg = RosterRegistry(window_seconds=60)
    reg.open(now=0.0)
    reg.observe("coordinator", text, now=1.0)
    names = [e.name for e in reg.present()]
    assert "your-agent-name" not in names
    assert names == ["alice"]


def test_guidance_lists_present_agents():
    reg = RosterRegistry(window_seconds=60)
    reg.open(now=0.0)
    reg.observe("alice", "[ROSTER] alice | swe | actions: bash | backend: local", now=1.0)
    guidance = roster_guidance(reg.present())
    assert guidance is not None
    assert "@alice" in guidance
    assert "ONLY to these present agents" in guidance


def test_guidance_none_when_empty():
    assert roster_guidance(()) is None
