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
