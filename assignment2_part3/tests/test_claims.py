from claims import ClaimRegistry


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
