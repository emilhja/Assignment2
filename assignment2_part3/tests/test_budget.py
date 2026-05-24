import json
import time

import pytest

from budget import Budget, BudgetExceeded, estimate_tokens


def test_permit_allows_under_caps():
    b = Budget(tokens_per_minute=100, requests_per_minute=10, lifetime_tokens=1000)
    b.permit(50)  # no exception
    b.record(50)
    b.permit(40)  # 50 + 40 <= 100


def test_permit_blocks_tpm():
    b = Budget(tokens_per_minute=100, requests_per_minute=10, lifetime_tokens=10_000)
    b.record(80)
    with pytest.raises(BudgetExceeded) as exc:
        b.permit(30)
    assert "tokens-per-minute" in exc.value.reason


def test_permit_blocks_rpm():
    b = Budget(tokens_per_minute=10_000, requests_per_minute=2, lifetime_tokens=10_000)
    b.record(1)
    b.record(1)
    with pytest.raises(BudgetExceeded) as exc:
        b.permit(1)
    assert "requests-per-minute" in exc.value.reason


def test_permit_blocks_lifetime():
    b = Budget(tokens_per_minute=10_000, requests_per_minute=100, lifetime_tokens=100)
    b.record(80)
    with pytest.raises(BudgetExceeded) as exc:
        b.permit(50)
    assert "lifetime" in exc.value.reason


def test_window_expires_after_60s():
    b = Budget(tokens_per_minute=100, requests_per_minute=10, lifetime_tokens=10_000)
    b.record(80, now=1000.0)
    b.permit(50, now=1061.0)  # 61s later, old event evicted


def test_pause_blocks_then_resume_allows():
    b = Budget(tokens_per_minute=100, requests_per_minute=10, lifetime_tokens=1000)
    b.pause()
    with pytest.raises(BudgetExceeded) as exc:
        b.permit(1)
    assert "paused" in exc.value.reason
    b.resume()
    b.permit(1)


def test_set_limit_takes_effect_immediately():
    b = Budget(tokens_per_minute=100, requests_per_minute=10, lifetime_tokens=1000)
    b.record(80)
    b.set_limit("tpm", 200)
    b.permit(100)  # would have failed at tpm=100


def test_set_limit_rejects_unknown_name():
    b = Budget()
    with pytest.raises(ValueError):
        b.set_limit("bogus", 1)


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "budget.json"
    a = Budget.load(path, tokens_per_minute=123, requests_per_minute=7, lifetime_tokens=999)
    a.record(50)
    a.save()
    b = Budget.load(path)
    assert b.tokens_per_minute == 123
    assert b.requests_per_minute == 7
    assert b.lifetime_tokens == 999
    assert b.lifetime_tokens_used == 50
    assert b.total_tokens_used == 50
    assert b.estimated_fallback_tokens == 50
    assert b.llm_calls == 1


def test_record_usage_tracks_exact_provider_tokens():
    b = Budget(tokens_per_minute=10_000, requests_per_minute=10, lifetime_tokens=10_000)
    b.record_usage(prompt_tokens=80, completion_tokens=20, total_tokens=100)

    snap = b.snapshot()
    assert snap["prompt_tokens_used"] == 80
    assert snap["completion_tokens_used"] == 20
    assert snap["total_tokens_used"] == 100
    assert snap["estimated_fallback_tokens"] == 0
    assert snap["lifetime_tokens_used"] == 100
    assert snap["llm_calls"] == 1


def test_record_usage_falls_back_to_estimate_when_exact_usage_missing():
    b = Budget(tokens_per_minute=10_000, requests_per_minute=10, lifetime_tokens=10_000)
    b.record_usage(estimated_tokens=33)

    snap = b.snapshot()
    assert snap["prompt_tokens_used"] == 0
    assert snap["completion_tokens_used"] == 0
    assert snap["total_tokens_used"] == 33
    assert snap["estimated_fallback_tokens"] == 33
    assert snap["llm_calls"] == 1


def test_snapshot_has_expected_keys():
    b = Budget(tokens_per_minute=10, requests_per_minute=2, lifetime_tokens=100)
    snap = b.snapshot()
    for key in (
        "paused",
        "tokens_per_minute",
        "requests_per_minute",
        "lifetime_tokens",
        "tokens_used_last_minute",
        "requests_used_last_minute",
        "lifetime_tokens_used",
        "prompt_tokens_used",
        "completion_tokens_used",
        "total_tokens_used",
        "estimated_fallback_tokens",
        "llm_calls",
    ):
        assert key in snap


def test_estimate_tokens_is_roughly_chars_over_four():
    assert estimate_tokens("abcd") in {1, 2}
    assert estimate_tokens("x" * 400) == 100
