import random

from claims import ClaimRegistry
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


def test_other_agent_assignment_with_role_description_does_not_trigger_reply():
    d = should_reply(
        _msg(
            "@bob-swe collaborate on /workspace/shared/calculator.py: "
            "alice writes add+subtract, bob writes multiply+divide"
        ),
        "alice",
        "alice-swe",
        [],
        now=1000.0,
    )
    assert d.respond is False


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


def test_alias_at_mention_triggers_reply():
    d = should_reply(
        _msg("@Emil can you check?"),
        "emil_hjaertfors_bot",
        "emil_hjaertfors_bot",
        [],
        now=1000.0,
        aliases=("Emil Hjärtfors", "Emil"),
    )
    assert d.respond is True
    assert "addressed" in d.reason


def test_alias_word_boundary_triggers_reply():
    d = should_reply(
        _msg("Emil Hjärtfors, are you here?"),
        "emil_hjaertfors_bot",
        "emil_hjaertfors_bot",
        [],
        now=1000.0,
        aliases=("Emil Hjärtfors", "Emil"),
    )
    assert d.respond is True


def test_alias_substring_inside_word_does_not_trigger():
    d = should_reply(
        _msg("Emilio is on holiday"),
        "emil_hjaertfors_bot",
        "emil_hjaertfors_bot",
        [],
        now=1000.0,
        aliases=("Emil",),
    )
    assert d.respond is False


def test_aliases_default_to_empty_and_keep_old_behavior():
    d = should_reply(
        _msg("Emil Hjärtfors, are you here?"),
        "emil_hjaertfors_bot",
        "emil_hjaertfors_bot",
        [],
        now=1000.0,
    )
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


def _rng():
    return random.Random(0)


def test_broadcast_keyword_everyone():
    d = should_reply(
        _msg("everyone, status please"), "alice", "alice-swe", [], now=1000.0, rng=_rng()
    )
    assert d.respond is True
    assert "broadcast" in d.reason


def test_broadcast_keyword_all_agents():
    d = should_reply(
        _msg("all agents, please report"), "alice", "alice-swe", [], now=1000.0, rng=_rng()
    )
    assert d.respond is True
    assert "broadcast" in d.reason


def test_broadcast_keyword_any_volunteers():
    d = should_reply(
        _msg("any volunteers to help with the migration?"),
        "alice",
        "alice-swe",
        [],
        now=1000.0,
        rng=_rng(),
    )
    assert d.respond is True
    assert "broadcast" in d.reason


def test_broadcast_keyword_whoever():
    d = should_reply(
        _msg("whoever picks this up, go ahead"),
        "alice",
        "alice-swe",
        [],
        now=1000.0,
        rng=_rng(),
    )
    assert d.respond is True
    assert "broadcast" in d.reason


def test_broadcast_keyword_all_agents_typos():
    # A fat-fingered roll-call ("all egents, are you here?") should still
    # register as a broadcast — the explicit typo set in BROADCAST_PATTERN
    # covers the common misspellings of "agents".
    for text in (
        "all egents, are you here?",
        "all agnets, status?",
        "all agnts please reply",
        "all aents present?",
        "all agets ping",
    ):
        d = should_reply(
            _msg(text), "alice", "alice-swe", [], now=1000.0, rng=_rng()
        )
        assert d.respond is True, text
        assert "broadcast" in d.reason, text


def test_broadcast_typo_set_does_not_match_unrelated_nouns():
    # The typo set must stay tight — "all events" / "all gents" / "all
    # students" are not roll-calls and must not flip the broadcast branch.
    for text in ("all events from yesterday", "all gents welcome", "all students passed"):
        d = should_reply(
            _msg(text), "alice", "alice-swe", [], now=1000.0, rng=_rng()
        )
        assert d.respond is False, text


def test_bare_all_does_not_trigger_broadcast():
    # "All" alone (no "agents") must NOT be treated as a broadcast — the
    # regex requires \b(everyone|anyone|all\s+agents?|...). This pins the
    # tight match so a future regex tweak can't silently broaden it.
    d = should_reply(
        _msg("All systems go"), "alice", "alice-swe", [], now=1000.0, rng=_rng()
    )
    assert d.respond is False


def test_multi_mention_triggers_both_agents():
    text = "@alice-swe @bob-swe ping"
    d_alice = should_reply(_msg(text), "alice", "alice-swe", [], now=1000.0, rng=_rng())
    d_bob = should_reply(_msg(text), "bob", "bob-swe", [], now=1000.0, rng=_rng())
    assert d_alice.respond is True
    assert "addressed" in d_alice.reason
    assert d_bob.respond is True
    assert "addressed" in d_bob.reason


def test_cooldown_blocks_broadcast_silently(monkeypatch):
    # Recreates the user's "no feedback" scenario: a recent direct reply
    # puts the agent in cooldown, and a broadcast arrives. The reply
    # gate must return the cooldown reason (not the broadcast reason),
    # because the cooldown check runs first by design. Locking this in
    # so any reordering of should_reply fails loudly.
    monkeypatch.setattr(reply_policy, "COOLDOWN_SECONDS", 30)
    recent = [(995.0, "m-prev")]  # replied 5s ago
    d = should_reply(
        _msg("all agents, please share your status"),
        "alice",
        "alice-swe",
        recent,
        now=1000.0,
        rng=_rng(),
    )
    assert d.respond is False
    assert d.reason.startswith("cooldown:")


def test_swedish_broadcast_triggers_reply():
    d = should_reply(
        _msg("kan någon kolla det här?"),
        "alice",
        "alice-swe",
        [],
        now=1000.0,
        rng=_rng(),
    )
    assert d.respond is True
    assert "broadcast" in d.reason


def test_swedish_broadcast_backoff(monkeypatch):
    monkeypatch.setattr(reply_policy, "MAX_BROADCAST_REPLIES", 1)
    monkeypatch.setattr(reply_policy, "BROADCAST_WINDOW_SECONDS", 300)
    monkeypatch.setattr(reply_policy, "COOLDOWN_SECONDS", 0)
    recent = [(950.0, "m-prev")]
    d = should_reply(
        _msg("alla agenter, status?"),
        "alice",
        "alice-swe",
        recent,
        now=1000.0,
    )
    assert d.respond is False
    assert "back-off" in d.reason


def test_broadcast_window_resets_after_window_seconds(monkeypatch):
    monkeypatch.setattr(reply_policy, "MAX_BROADCAST_REPLIES", 1)
    monkeypatch.setattr(reply_policy, "BROADCAST_WINDOW_SECONDS", 300)
    monkeypatch.setattr(reply_policy, "COOLDOWN_SECONDS", 0)
    # Previous broadcast reply is older than the window — should NOT count.
    recent = [(500.0, "m-old")]  # 500s ago, window is 300s
    d = should_reply(
        _msg("anyone able to take this?"),
        "alice",
        "alice-swe",
        recent,
        now=1000.0,
        rng=_rng(),
    )
    assert d.respond is True
    assert "broadcast" in d.reason


def test_claim_collision_bypasses_cooldown():
    """Peer's CLAIM for a path we already self-claimed must override cooldown."""

    claims = ClaimRegistry()
    claims.record_observed("alice", "/workspace/shared/calc.py")

    incoming = _msg(
        "CLAIM /workspace/shared/calc.py: I'll add multiply and divide",
        sender="bob",
    )
    # Recent reply 1s ago — would normally trigger cooldown.
    decision = should_reply(
        incoming,
        "alice",
        "alice-swe",
        recent_replies=[(999.0, "prev")],
        now=1000.0,
        claims=claims,
    )
    assert decision.respond is True
    assert "claim collision" in decision.reason


def test_claim_collision_decides_tie_break_self_wins():
    """When self_id is lex-smaller than the peer, collision outcome is self-wins."""

    claims = ClaimRegistry()
    claims.record_observed("alice", "/workspace/shared/calc.py#multiply-divide")

    incoming = _msg(
        "CLAIM /workspace/shared/calc.py#multiply-divide: also working on this",
        sender="bob",
    )
    decision = should_reply(
        incoming, "alice", "alice-swe", [], now=1000.0, claims=claims
    )
    assert decision.respond is True
    assert decision.collision is not None
    assert decision.collision.outcome == "self-wins"
    assert decision.collision.peer_id == "bob"
    assert decision.collision.path == "/workspace/shared/calc.py#multiply-divide"


def test_claim_collision_decides_tie_break_self_loses():
    """When peer is lex-smaller, collision outcome is self-loses."""

    claims = ClaimRegistry()
    claims.record_observed("bob", "/workspace/shared/calc.py#multiply-divide")

    incoming = _msg(
        "CLAIM /workspace/shared/calc.py#multiply-divide: also working on this",
        sender="alice",
    )
    decision = should_reply(
        incoming, "bob", "bob-swe", [], now=1000.0, claims=claims
    )
    assert decision.respond is True
    assert decision.collision is not None
    assert decision.collision.outcome == "self-loses"
    assert decision.collision.peer_id == "alice"


def test_claim_collision_whole_file_vs_scoped_peer():
    """We own the whole file; peer scopes their CLAIM — must still collide."""

    claims = ClaimRegistry()
    claims.record_observed("alice", "/workspace/shared/calc.py")

    incoming = _msg(
        "CLAIM /workspace/shared/calc.py#multiply-divide: drafting",
        sender="bob",
    )
    decision = should_reply(
        incoming,
        "alice",
        "alice-swe",
        recent_replies=[(999.5, "prev")],
        now=1000.0,
        claims=claims,
    )
    assert decision.respond is True
    assert "claim collision" in decision.reason
    assert decision.collision is not None
    assert decision.collision.outcome == "self-wins"


def test_defer_only_message_does_not_trigger_reply():
    """A peer's `DEFER to @you` is one-way ack; the @mention inside it
    must not wake the recipient and start a ping-pong loop."""

    d = should_reply(
        _msg("DEFER to @alice-swe", sender="bob"),
        "alice",
        "alice-swe",
        [],
        now=1000.0,
    )
    assert d.respond is False
    assert "directly addressed" not in d.reason


def test_release_only_message_does_not_trigger_reply():
    """A bare RELEASE line should not trigger a reply on its own."""

    d = should_reply(
        _msg("RELEASE /workspace/shared/calc.py#multiply-divide", sender="bob"),
        "alice",
        "alice-swe",
        [],
        now=1000.0,
    )
    assert d.respond is False


def test_real_mention_alongside_defer_still_triggers_reply():
    """Real `@alice-swe` outside the DEFER line must still address alice."""

    text = "@alice-swe please review my DEFER to @bob-swe"
    d = should_reply(_msg(text, sender="charlie"), "alice", "alice-swe", [], now=1000.0)
    assert d.respond is True
    assert "addressed" in d.reason


def test_claim_collision_only_fires_on_self_claim():
    """A peer CLAIM for a path we do not own should not bypass cooldown."""

    claims = ClaimRegistry()  # registry is empty: we own nothing.

    incoming = _msg(
        "CLAIM /workspace/shared/calc.py: drafting",
        sender="bob",
    )
    decision = should_reply(
        incoming,
        "alice",
        "alice-swe",
        recent_replies=[(999.5, "prev")],
        now=1000.0,
        claims=claims,
    )
    assert decision.respond is False
    assert "cooldown" in decision.reason
