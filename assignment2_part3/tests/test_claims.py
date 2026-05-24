from claims import ClaimRegistry, tie_break_winner


def test_absorb_text_records_claim_and_release():
    clock = iter([100.0, 101.0, 102.0, 103.0, 104.0])
    registry = ClaimRegistry(ttl_seconds=60.0, clock=lambda: next(clock))

    text = "CLAIM /workspace/shared/calc.py: I will draft the four ops"
    recorded = registry.absorb_text("alice", text)

    assert len(recorded) == 1
    assert recorded[0].path == "/workspace/shared/calc.py"
    assert recorded[0].claimant == "alice"
    assert recorded[0].reason.startswith("I will draft")

    # Different agent must be blocked.
    blocking = registry.is_claimed_by_other("/workspace/shared/calc.py", "bob")
    assert blocking is not None and blocking.claimant == "alice"

    # The claimant itself is not blocked.
    assert registry.is_claimed_by_other("/workspace/shared/calc.py", "alice") is None

    # RELEASE clears it.
    registry.absorb_text("alice", "RELEASE /workspace/shared/calc.py")
    assert registry.is_claimed_by_other("/workspace/shared/calc.py", "bob") is None


def test_claim_expires_after_ttl():
    times = iter([10.0, 11.0, 80.0])
    registry = ClaimRegistry(ttl_seconds=30.0, clock=lambda: next(times))

    registry.record_observed("alice", "/workspace/shared/x.py")
    # Within TTL the claim is visible.
    assert registry.lookup("/workspace/shared/x.py") is not None
    # Past TTL the claim disappears.
    assert registry.lookup("/workspace/shared/x.py") is None


def test_release_only_by_original_claimant():
    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/x.py")
    assert registry.release("bob", "/workspace/shared/x.py") is False
    assert registry.lookup("/workspace/shared/x.py") is not None
    assert registry.release("alice", "/workspace/shared/x.py") is True
    assert registry.lookup("/workspace/shared/x.py") is None


def test_non_shared_paths_are_ignored():
    registry = ClaimRegistry()
    # Only /workspace/shared/ paths match the pattern.
    assert registry.absorb_text("alice", "CLAIM /etc/passwd: nope") == []
    assert registry.absorb_text("alice", "CLAIM /workspace/alice/file.py: nope") == []


def test_scoped_claims_on_same_file_do_not_conflict():
    registry = ClaimRegistry()

    registry.record_observed("alice", "/workspace/shared/calculator.py#add-subtract")
    registry.record_observed("bob", "/workspace/shared/calculator.py#multiply-divide")

    assert registry.is_claimed_by_other(
        "/workspace/shared/calculator.py#add-subtract", "alice"
    ) is None
    assert registry.is_claimed_by_other(
        "/workspace/shared/calculator.py#multiply-divide", "bob"
    ) is None
    assert registry.own_claim_for_write("/workspace/shared/calculator.py", "alice") is not None
    assert registry.own_claim_for_write("/workspace/shared/calculator.py", "bob") is not None


def test_whole_file_claim_conflicts_with_scoped_claims():
    registry = ClaimRegistry()

    registry.record_observed("alice", "/workspace/shared/calculator.py")

    blocking = registry.is_claimed_by_other(
        "/workspace/shared/calculator.py#multiply-divide", "bob"
    )
    assert blocking is not None
    assert blocking.claimant == "alice"


def test_whole_file_release_clears_claimants_scopes():
    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calculator.py#add-subtract")
    registry.record_observed("alice", "/workspace/shared/calculator.py#docs")

    assert registry.release("alice", "/workspace/shared/calculator.py") is True

    assert registry.active_claims_for("alice") == []


def test_tie_break_winner_is_lexicographic_and_case_insensitive():
    assert tie_break_winner("alice", "bob") == "alice"
    assert tie_break_winner("bob", "alice") == "alice"
    assert tie_break_winner("Alice", "alice") in ("Alice", "alice")
    # Symmetry: same inputs return same winner regardless of arg order.
    assert tie_break_winner("alice-swe", "bob-swe") == tie_break_winner("bob-swe", "alice-swe")


def test_absorb_defer_records_pair():
    registry = ClaimRegistry()
    registry.absorb_text("alice", "DEFER to @bob-swe")

    # Until bob defers back, mutual-defer is False.
    assert registry.mutual_defer_detected("alice", "bob-swe") is False

    registry.absorb_text("bob-swe", "DEFER to @alice")
    assert registry.mutual_defer_detected("alice", "bob-swe") is True
    # Symmetric: the detection works from either side's perspective.
    assert registry.mutual_defer_detected("bob-swe", "alice") is True


def test_release_clears_deferrer_outstanding_defers():
    registry = ClaimRegistry()
    registry.absorb_text("alice", "DEFER to @bob")
    registry.absorb_text("bob", "DEFER to @alice")
    assert registry.mutual_defer_detected("alice", "bob") is True

    # Alice releases — her side of the mutual-defer should clear so she
    # is not still considered "currently deferring" after moving on.
    registry.record_observed("alice", "/workspace/shared/x.py")
    registry.absorb_text("alice", "RELEASE /workspace/shared/x.py")
    assert registry.mutual_defer_detected("alice", "bob") is False


def test_defer_window_expires():
    times = iter([100.0, 100.0, 250.0, 250.0])
    registry = ClaimRegistry(defer_window_seconds=60.0, clock=lambda: next(times))
    registry.absorb_text("alice", "DEFER to @bob")
    registry.absorb_text("bob", "DEFER to @alice")
    # By the time we check, clock has advanced past the defer window.
    assert registry.mutual_defer_detected("alice", "bob") is False


def test_clear_defers_between_targets_pair():
    registry = ClaimRegistry()
    registry.absorb_text("alice", "DEFER to @bob")
    registry.absorb_text("bob", "DEFER to @alice")
    registry.absorb_text("alice", "DEFER to @carol")

    registry.clear_defers_between("alice", "bob")
    assert registry.mutual_defer_detected("alice", "bob") is False
    # Unrelated defers remain.
    assert registry.mutual_defer_detected("alice", "carol") is False  # carol hasn't deferred back


def test_mark_satisfied_excludes_claim_from_unsatisfied_list():
    """A write to the base path satisfies the scoped claim too — the agent
    only owes one successful write per scoped claim, so the unsatisfied
    nudge in group_chat should disappear once the write lands."""

    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calc.py#add")

    unsatisfied = registry.unsatisfied_claims_for("alice")
    assert len(unsatisfied) == 1
    assert unsatisfied[0].target == "/workspace/shared/calc.py#add"

    # The write tool fires on the base path — the scope is implicit.
    assert registry.mark_satisfied("alice", "/workspace/shared/calc.py") is True
    assert registry.unsatisfied_claims_for("alice") == []


def test_mark_satisfied_only_affects_matching_claimant():
    """Alice's write must not silence bob's open claim — otherwise bob would
    get no nudge for work he still owes."""

    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calc.py#add")
    registry.record_observed("bob", "/workspace/shared/calc.py#multiply")

    registry.mark_satisfied("alice", "/workspace/shared/calc.py")
    assert registry.unsatisfied_claims_for("alice") == []
    bob_unsatisfied = registry.unsatisfied_claims_for("bob")
    assert len(bob_unsatisfied) == 1
    assert bob_unsatisfied[0].target == "/workspace/shared/calc.py#multiply"


def test_mark_satisfied_no_matching_claim_returns_false():
    registry = ClaimRegistry()
    assert registry.mark_satisfied("alice", "/workspace/shared/calc.py") is False


def test_release_clears_satisfaction_so_reclaim_is_unsatisfied():
    """After RELEASE the path is free; a fresh claim on the same path must
    appear in unsatisfied — otherwise the new write would be skipped by the
    nudge logic because the prior satisfaction lingered."""

    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calc.py#add")
    registry.mark_satisfied("alice", "/workspace/shared/calc.py")
    assert registry.release("alice", "/workspace/shared/calc.py#add") is True

    registry.record_observed("alice", "/workspace/shared/calc.py#add")
    unsatisfied = registry.unsatisfied_claims_for("alice")
    assert len(unsatisfied) == 1
    assert unsatisfied[0].target == "/workspace/shared/calc.py#add"


def test_ttl_expiry_clears_satisfaction_so_reclaim_is_unsatisfied():
    times = iter([10.0, 11.0, 80.0, 81.0, 82.0])
    registry = ClaimRegistry(ttl_seconds=30.0, clock=lambda: next(times))
    registry.record_observed("alice", "/workspace/shared/calc.py#add")   # claimed_at=10
    registry.mark_satisfied("alice", "/workspace/shared/calc.py")        # satisfied_at=11
    # Lookup at t=80 is past the 30s TTL, so the claim (and its
    # satisfaction entry) must be swept.
    assert registry.lookup("/workspace/shared/calc.py#add") is None

    registry.record_observed("alice", "/workspace/shared/calc.py#add")   # claimed_at=81
    unsatisfied = registry.unsatisfied_claims_for("alice")               # now=82
    assert len(unsatisfied) == 1


def test_release_without_satisfaction_is_tracked_for_next_turn_nudge():
    """The bug we're fixing: alice posts CLAIM then RELEASE in the same
    exchange without ever calling a write tool. The registry has nothing to
    nudge with on her next turn unless we remember the abandonment."""

    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calc.py#add")
    assert registry.release("alice", "/workspace/shared/calc.py#add") is True

    released = registry.recently_released_unsatisfied_for("alice")
    assert len(released) == 1
    assert released[0].target == "/workspace/shared/calc.py#add"


def test_release_after_satisfaction_is_not_tracked_as_abandoned():
    """If alice actually wrote the file, RELEASE is the correct end-of-work
    signal — the nudge must NOT fire on her next turn."""

    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calc.py#add")
    registry.mark_satisfied("alice", "/workspace/shared/calc.py")
    assert registry.release("alice", "/workspace/shared/calc.py#add") is True

    assert registry.recently_released_unsatisfied_for("alice") == []


def test_recently_released_unsatisfied_window_expires():
    times = iter([10.0, 11.0, 12.0, 200.0])
    registry = ClaimRegistry(
        released_unsatisfied_window_seconds=60.0,
        clock=lambda: next(times),
    )
    registry.record_observed("alice", "/workspace/shared/calc.py#add")   # t=10
    registry.release("alice", "/workspace/shared/calc.py#add")           # released_at=11

    # Within window we see it; far beyond, it's gone.
    assert len(registry.recently_released_unsatisfied_for("alice")) == 1  # t=12
    assert registry.recently_released_unsatisfied_for("alice") == []      # t=200


def test_reclaiming_same_target_clears_released_unsatisfied_record():
    """Re-CLAIMing the same path means the agent intends to do the work
    again — the prior abandonment record is stale and should not nudge."""

    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calc.py#add")
    registry.release("alice", "/workspace/shared/calc.py#add")
    assert len(registry.recently_released_unsatisfied_for("alice")) == 1

    registry.record_observed("alice", "/workspace/shared/calc.py#add")
    assert registry.recently_released_unsatisfied_for("alice") == []


def test_released_unsatisfied_is_scoped_per_claimant():
    """Bob abandoning his multiply-divide scope must not show up as alice's
    abandoned work."""

    registry = ClaimRegistry()
    registry.record_observed("bob", "/workspace/shared/calc.py#multiply")
    registry.release("bob", "/workspace/shared/calc.py#multiply")

    assert registry.recently_released_unsatisfied_for("alice") == []
    assert len(registry.recently_released_unsatisfied_for("bob")) == 1
