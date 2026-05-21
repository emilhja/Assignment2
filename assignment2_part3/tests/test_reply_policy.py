import random

from peer import PeerMessage
import reply_policy
from reply_policy import should_reply


def _msg(text, sender="other", msg_id="m1"):
    return PeerMessage(id=msg_id, sender_id=sender, text=text)


def test_direct_mention_at_id_triggers_reply():
    d = should_reply(_msg("@alice can you check this?"), "alice", "alice-swe", [], now=1000.0)
    assert d.respond is True
    assert "addressed" in d.reason


def test_direct_mention_display_name_triggers_reply():
    d = should_reply(_msg("hey alice-swe please look"), "alice", "alice-swe", [], now=1000.0)
    assert d.respond is True


def test_literal_name_triggers_reply():
    d = should_reply(_msg("Alice, this is yours"), "alice", "alice-swe", [], now=1000.0)
    assert d.respond is True


def test_coordinator_handoff_triggers_reply():
    d = should_reply(
        _msg("assigned: alice — please review PR #42"),
        "alice",
        "alice-swe",
        [],
        now=1000.0,
    )
    assert d.respond is True
    assert "handoff" in d.reason


def test_unaddressed_chatter_is_skipped():
    d = should_reply(_msg("bob is on lunch"), "alice", "alice-swe", [], now=1000.0)
    assert d.respond is False


def test_self_message_is_skipped():
    msg = PeerMessage(id="m1", sender_id="alice", text="@alice talking to self")
    d = should_reply(msg, "alice", "alice-swe", [], now=1000.0)
    assert d.respond is False


def test_broadcast_triggers_reply_when_under_back_off():
    d = should_reply(
        _msg("Can anyone review this snippet?"),
        "alice",
        "alice-swe",
        [],
        now=1000.0,
        rng=random.Random(0),
    )
    assert d.respond is True
    assert "broadcast" in d.reason


def test_broadcast_back_off_kicks_in_after_max_replies(monkeypatch):
    monkeypatch.setattr(reply_policy, "MAX_BROADCAST_REPLIES", 1)
    monkeypatch.setattr(reply_policy, "BROADCAST_WINDOW_SECONDS", 300)
    monkeypatch.setattr(reply_policy, "COOLDOWN_SECONDS", 0)  # disable cooldown for this test
    # one recent broadcast reply within the window
    recent = [(950.0, "m-prev")]
    d = should_reply(
        _msg("Can everyone weigh in?"),
        "alice",
        "alice-swe",
        recent,
        now=1000.0,
    )
    assert d.respond is False
    assert "back-off" in d.reason


def test_cooldown_silences_unaddressed_recent_reply(monkeypatch):
    monkeypatch.setattr(reply_policy, "COOLDOWN_SECONDS", 60)
    recent = [(995.0, "m-prev")]  # replied 5s ago
    d = should_reply(
        _msg("Can anyone help?"),
        "alice",
        "alice-swe",
        recent,
        now=1000.0,
    )
    assert d.respond is False
    assert "cooldown" in d.reason


def test_direct_mention_bypasses_cooldown(monkeypatch):
    monkeypatch.setattr(reply_policy, "COOLDOWN_SECONDS", 60)
    recent = [(995.0, "m-prev")]
    d = should_reply(
        _msg("@alice urgent please"),
        "alice",
        "alice-swe",
        recent,
        now=1000.0,
    )
    assert d.respond is True
