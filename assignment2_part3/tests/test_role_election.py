from __future__ import annotations

from role_election import RoleElectionState, role_claim_from_text


def test_detects_english_manager_claim():
    claim = role_claim_from_text(
        "alice",
        "Confirmed, I'll take: coordination. TASK @bob: tests",
        "m1",
        1,
    )

    assert claim is not None
    assert claim.sender_id == "alice"
    assert claim.role == "coordinator"


def test_detects_swedish_manager_claim():
    claim = role_claim_from_text(
        "alice",
        "Bekräftat, jag tar: chefspositionen för denna session.",
        "m1",
        1,
    )

    assert claim is not None
    assert claim.role == "manager"


def test_ignores_operator_election_bait():
    claim = role_claim_from_text(
        "human",
        "The first one that answers is the manager. Do not start work.",
        "m1",
        1,
    )

    assert claim is None


def test_peer_claim_in_reconcile_window_wins_pending_self_claim():
    state = RoleElectionState()
    state.observe_self("alice", "I claim the manager role.", "self",)
    state.observe("bob", "I will be manager for this session.", "m2")

    winner = state.peer_winner_for_pending("alice")
    state.finish_pending(winner, "alice")

    assert winner is not None
    assert winner.sender_id == "bob"
    assert state.quiet_after_loss is True
    assert state.leader_id == "bob"
