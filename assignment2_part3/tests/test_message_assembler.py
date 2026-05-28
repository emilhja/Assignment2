from message_assembler import (
    DEFAULT_TIMEOUT_SECONDS,
    MultipartAssembler,
)
from peer import PeerMessage


def _msg(id_: str, sender: str, text: str, received_at: float = 1000.0) -> PeerMessage:
    return PeerMessage(id=id_, sender_id=sender, text=text, received_at=received_at)


def test_passthrough_for_non_part_message():
    a = MultipartAssembler()
    msg = _msg("m1", "alice", "hello world")
    out = a.feed(msg, now=1000.0)
    assert out == [msg]
    assert a.pending_count() == 0


def test_buffers_first_part_returns_empty():
    a = MultipartAssembler()
    out = a.feed(_msg("m1", "alice", "(part 1/2)\nfirst body"), now=1000.0)
    assert out == []
    assert a.pending_count() == 1


def test_delivers_reassembled_on_last_part():
    a = MultipartAssembler()
    a.feed(_msg("m1", "alice", "(part 1/2)\nfirst body"), now=1000.0)
    out = a.feed(_msg("m2", "alice", "(part 2/2)\nsecond body", received_at=1001.0), now=1001.0)
    assert len(out) == 1
    assembled = out[0]
    assert assembled.sender_id == "alice"
    assert assembled.id == "m1"  # first part's id wins for trace continuity
    assert assembled.text == "first body\nsecond body"
    assert assembled.received_at == 1001.0
    assert a.pending_count() == 0


def test_out_of_order_parts_assemble_correctly():
    a = MultipartAssembler()
    a.feed(_msg("m2", "alice", "(part 2/2)\nsecond"), now=1001.0)
    out = a.feed(_msg("m1", "alice", "(part 1/2)\nfirst"), now=1002.0)
    assert len(out) == 1
    assert out[0].text == "first\nsecond"


def test_part_1_of_1_strips_header_and_delivers():
    a = MultipartAssembler()
    out = a.feed(_msg("m1", "alice", "(part 1/1)\nonly body"), now=1000.0)
    assert len(out) == 1
    assert out[0].text == "only body"
    assert a.pending_count() == 0


def test_separate_senders_do_not_interfere():
    a = MultipartAssembler()
    assert a.feed(_msg("a1", "alice", "(part 1/2)\nalice-1"), now=1000.0) == []
    assert a.feed(_msg("b1", "bob",   "(part 1/2)\nbob-1"),   now=1000.5) == []
    assert a.pending_count() == 2

    alice_done = a.feed(_msg("a2", "alice", "(part 2/2)\nalice-2"), now=1001.0)
    assert len(alice_done) == 1
    assert alice_done[0].text == "alice-1\nalice-2"
    assert a.pending_count() == 1

    bob_done = a.feed(_msg("b2", "bob", "(part 2/2)\nbob-2"), now=1002.0)
    assert len(bob_done) == 1
    assert bob_done[0].text == "bob-1\nbob-2"
    assert a.pending_count() == 0


def test_timeout_flushes_incomplete_with_marker():
    a = MultipartAssembler(timeout_seconds=30.0)
    a.feed(_msg("m1", "alice", "(part 1/3)\nfirst"), now=1000.0)
    a.feed(_msg("m2", "alice", "(part 3/3)\nthird"), now=1005.0)

    # Not yet expired.
    assert a.flush_expired(now=1020.0) == []

    flushed = a.flush_expired(now=1031.0)
    assert len(flushed) == 1
    text = flushed[0].text
    assert "[incomplete multi-part message from alice: received parts 1, 3 of 3]" in text
    assert "first" in text and "third" in text
    assert a.pending_count() == 0


def test_unrelated_message_from_same_sender_flushes_pending():
    a = MultipartAssembler()
    a.feed(_msg("m1", "alice", "(part 1/3)\nstart of code"), now=1000.0)
    out = a.feed(_msg("m2", "alice", "actually never mind, different topic"), now=1001.0)
    assert len(out) == 2
    # Incomplete group delivered first (arrived first), then the new plain msg.
    assert "[incomplete multi-part message from alice" in out[0].text
    assert out[1].text == "actually never mind, different topic"
    assert a.pending_count() == 0


def test_duplicate_part_index_keeps_last():
    a = MultipartAssembler()
    a.feed(_msg("m1a", "alice", "(part 1/2)\nfirst attempt"), now=1000.0)
    a.feed(_msg("m1b", "alice", "(part 1/2)\nsecond attempt"), now=1001.0)
    out = a.feed(_msg("m2", "alice", "(part 2/2)\nthe tail"), now=1002.0)
    assert len(out) == 1
    assert out[0].text == "second attempt\nthe tail"


def test_invalid_index_or_out_of_range_treated_as_plain():
    a = MultipartAssembler()
    msg = _msg("m1", "alice", "(part 5/2)\nbogus")
    out = a.feed(msg, now=1000.0)
    assert out == [msg]
    assert a.pending_count() == 0


def test_max_parts_cap_rejects_huge_n():
    a = MultipartAssembler(max_parts=20)
    msg = _msg("m1", "alice", "(part 1/9999)\nspammy")
    out = a.feed(msg, now=1000.0)
    assert out == [msg]
    assert a.pending_count() == 0


def test_assembled_message_uses_first_part_id_and_last_part_timestamp():
    a = MultipartAssembler()
    a.feed(_msg("first-id", "alice", "(part 1/2)\nA", received_at=1000.0), now=1000.0)
    out = a.feed(
        _msg("second-id", "alice", "(part 2/2)\nB", received_at=1005.0),
        now=1005.0,
    )
    assert out[0].id == "first-id"
    assert out[0].received_at == 1005.0


def test_per_sender_group_cap_evicts_oldest_incomplete():
    a = MultipartAssembler(max_groups_per_sender=2)
    a.feed(_msg("m1", "alice", "(part 1/2)\ngroupA"), now=1000.0)
    a.feed(_msg("m2", "alice", "(part 1/3)\ngroupB"), now=1001.0)
    # Third concurrent group from alice forces eviction of the oldest.
    out = a.feed(_msg("m3", "alice", "(part 1/4)\ngroupC"), now=1002.0)
    assert len(out) == 1
    assert "[incomplete multi-part message from alice" in out[0].text
    assert "groupA" in out[0].text
    assert a.pending_count() == 2


def test_default_timeout_is_60_seconds():
    assert DEFAULT_TIMEOUT_SECONDS == 60.0


def test_header_tolerates_whitespace_and_case():
    a = MultipartAssembler()
    a.feed(_msg("m1", "alice", "(Part 1/2)\nupper"), now=1000.0)
    out = a.feed(_msg("m2", "alice", "( part 2 / 2 )\nlower"), now=1001.0)
    assert len(out) == 1
    assert out[0].text == "upper\nlower"
