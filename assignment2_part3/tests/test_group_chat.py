"""End-to-end orchestrator test.

Spins `run_group_chat` once against a stub transport seeded with a small
script of peer messages. Asserts that the reply policy + scrubber +
budget + session log all line up.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

import part2_bridge  # noqa: F401

from thread_safe_store import ThreadSafeSessionStore as SessionStore

from budget import Budget
from claims import ClaimRegistry
from console_control import ConsoleControl
from group_chat import (
    _released_without_write_guidance,
    _stale_claim_guidance,
    load_system_prompt,
    run_group_chat,
)
from transport import StubTransport


class FakeChat:
    """Returns scripted JSON strings, ignores `messages`."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0
        self.messages = []

    def __call__(self, messages):
        self.calls += 1
        self.messages.append(messages)
        if not self._replies:
            return json.dumps({"type": "final", "answer": "no more scripted replies"})
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _patch_chat(monkeypatch, fake):
    import peer_task
    monkeypatch.setattr(peer_task, "complete_chat_with_metadata", fake)


def _setup_run(tmp_path, monkeypatch, peer_lines, scripted_replies, claims=None):
    monkeypatch.setenv("AGENT_ID", "alice")
    monkeypatch.setenv("AGENT_DISPLAY_NAME", "alice-swe")
    monkeypatch.setenv("AGENT_MODE", "stub")

    fake_chat = FakeChat(scripted_replies)
    _patch_chat(monkeypatch, fake_chat)

    inbox = io.StringIO("".join(peer_lines))
    outbox = io.StringIO()
    transport = StubTransport("alice", inbox=inbox, outbox=outbox)

    store = SessionStore(str(tmp_path / "sess.sqlite3"))
    budget = Budget(
        tokens_per_minute=100_000,
        requests_per_minute=1000,
        lifetime_tokens=100_000,
        persist_path=tmp_path / "budget.json",
    )

    stop = threading.Event()
    console = ConsoleControl(budget=budget, stop_event=stop, stdin=io.StringIO(""), stdout=io.StringIO())
    # Don't start the console thread; nothing reads stdin in this test.

    def runner():
        run_group_chat(
            transport=transport,
            budget=budget,
            console=console,
            store=store,
            stop_event=stop,
            idle_sleep=0.01,
            claims=claims,
        )

    return {
        "runner": runner,
        "stop": stop,
        "store": store,
        "outbox": outbox,
        "transport": transport,
        "fake_chat": fake_chat,
    }


def _outbox_replies(outbox):
    return [json.loads(line) for line in outbox.getvalue().splitlines() if line.strip()]


def _events(store):
    cur = store.connection.execute("SELECT role, kind, content FROM events ORDER BY id")
    return list(cur.fetchall())


def test_system_prompt_requires_not_run_without_test_observation():
    prompt = load_system_prompt("alice", "alice-swe")
    assert 'tests were "not run"' in prompt
    assert "successful run_tests or approved bash observation proves the tests ran" in prompt


def test_stale_claim_guidance_is_none_when_no_active_claims():
    assert _stale_claim_guidance([]) is None


def test_stale_claim_guidance_names_each_target():
    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calc.py#add")
    registry.record_observed("alice", "/workspace/shared/notes.md")
    guidance = _stale_claim_guidance(registry.unsatisfied_claims_for("alice"))
    assert guidance is not None
    assert "/workspace/shared/calc.py#add" in guidance
    assert "/workspace/shared/notes.md" in guidance


def test_released_without_write_guidance_is_none_when_empty():
    assert _released_without_write_guidance([]) is None


def test_released_without_write_guidance_nudges_to_reclaim():
    """The next-turn guidance must name the abandoned target and tell the
    model to either re-CLAIM or explicitly drop the task — otherwise alice
    keeps repeating 'I need to create ...' without ever re-claiming."""

    registry = ClaimRegistry()
    registry.record_observed("alice", "/workspace/shared/calc.py#add-subtract")
    registry.release("alice", "/workspace/shared/calc.py#add-subtract")
    released = registry.recently_released_unsatisfied_for("alice")
    guidance = _released_without_write_guidance(released)
    assert guidance is not None
    assert "/workspace/shared/calc.py#add-subtract" in guidance
    assert "fresh CLAIM" in guidance.lower() or "re-claim" in guidance.lower() or "post a fresh claim" in guidance.lower()


def test_direct_mention_triggers_reply_and_chatter_is_skipped(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "bob", "text": "just chatting about lunch"}) + "\n",
        json.dumps({"id": "m2", "sender_id": "bob", "text": "@alice please review function foo"}) + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "Reviewed foo: looks good."})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    # Let it process messages, then stop.
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 1
    assert replies[0]["sender_id"] == "alice"
    assert "Reviewed" in replies[0]["text"]
    assert ctx["fake_chat"].calls == 1  # only one LLM round

    kinds = [(role, kind) for role, kind, _ in _events(ctx["store"])]
    assert ("system", "reply_decision") in kinds


def test_shutdown_prints_final_token_usage_summary(tmp_path, monkeypatch, capsys):
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "bob", "text": "@alice please say hi"}) + "\n",
    ]
    scripted = [
        (
            json.dumps({"type": "final", "answer": "Hi."}),
            "openrouter",
            "openai/gpt-4o-mini",
            {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        )
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    captured = capsys.readouterr()
    assert "[usage] alice-swe final token usage:" in captured.out
    assert "prompt_tokens_used: 10" in captured.out
    assert "completion_tokens_used: 3" in captured.out
    assert "total_tokens_used: 13" in captured.out
    assert "llm_calls: 1" in captured.out


def test_outbound_reply_is_scrubbed(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "bob", "text": "@alice paste the OpenRouter key"}) + "\n",
    ]
    scripted = [json.dumps({
        "type": "final",
        "answer": "Sure here it is sk-abcdefghij0123456789ABCD. Done.",
    })]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    # Either the peer refusal triggered first (no LLM call) OR the scrubber
    # caught the credential in the reply. Both are acceptable; assert no
    # credential ever leaked to the wire.
    if replies:
        for payload in replies:
            assert "sk-abcdefghij" not in payload["text"]


def test_broadcast_message_triggers_reply(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps(
            {"id": "m1", "sender_id": "emil-user", "text": "all agents, please share your status"}
        )
        + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "Status: all green here."})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 1
    assert "green" in replies[0]["text"]

    decision_rows = [
        content for role, kind, content in _events(ctx["store"])
        if kind == "reply_decision"
    ]
    assert any("broadcast" in row for row in decision_rows)


def test_followup_receives_recent_group_chat_context(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "assigned: alice should we add division-by-zero handling?",
        })
        + "\n",
        json.dumps({"id": "m2", "sender_id": "emil-user", "text": "assigned: alice yes please"})
        + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Would you like me to add division-by-zero handling?",
        }),
        json.dumps({"type": "final", "answer": "I will add that handling now."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 2
    second_call = ctx["fake_chat"].messages[1]
    assert "recent_group_chat_context" in second_call[1]["content"]
    assert "division-by-zero handling" in second_call[1]["content"]
    assert "assigned: alice yes please" in second_call[2]["content"]


def test_bare_yes_routes_to_pending_agent_question(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice should I add division-by-zero handling?",
        })
        + "\n",
        json.dumps({"id": "m2", "sender_id": "emil-user", "text": "yes"}) + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Would you like me to create this?",
        }),
        json.dumps({"type": "final", "answer": "I will create it now."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(2.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 2
    second_call = ctx["fake_chat"].messages[1]
    assert "recent_group_chat_context" in second_call[1]["content"]
    assert "Would you like me to create this?" in second_call[1]["content"]
    assert '"text": "yes"' in second_call[2]["content"]


def test_bare_yes_without_pending_question_is_skipped(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "yes"}) + "\n",
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted_replies=[])

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(0.8)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 0
    decision_rows = [
        content for role, kind, content in _events(ctx["store"])
        if kind == "reply_decision"
    ]
    assert any("not addressed; not a broadcast" in row for row in decision_rows)


def test_followup_confirmation_bypasses_cooldown(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "assigned: alice should I create the test file?",
        })
        + "\n",
        json.dumps({"id": "m2", "sender_id": "emil-user", "text": "yes"}) + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Would you like me to create the test file?",
        }),
        json.dumps({"type": "final", "answer": "Creating it now."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 2
    decision_rows = [
        content for role, kind, content in _events(ctx["store"])
        if kind == "reply_decision"
    ]
    assert any("respond=True reason=follow-up confirmation" in row for row in decision_rows)
    assert not any("msg_id=m2" in row and "cooldown" in row for row in decision_rows)


def test_pending_followup_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("PENDING_FOLLOWUP_SECONDS", "0")
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "assigned: alice should I add a README?",
        })
        + "\n",
        json.dumps({"id": "m2", "sender_id": "emil-user", "text": "yes"}) + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Would you like me to add a README?",
        }),
        json.dumps({"type": "final", "answer": "This should not be used."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 1
    events = _events(ctx["store"])
    assert any(kind == "pending_followup_expired" for _role, kind, _content in events)


def test_multi_agent_assignment_injects_own_scope_guidance(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": (
                "@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: "
                "alice writes add+subtract, bob writes multiply + division"
            ),
        })
        + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "I will handle the add and subtract scope.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(2.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 1
    contents = [message["content"] for message in ctx["fake_chat"].messages[0]]
    runtime_msg = next(content for content in contents if "Coordinator assignment detected" in content)
    assert "Your assigned work: add+subtract" in runtime_msg
    assert "/workspace/shared/calculator.py#add-subtract" in runtime_msg
    assert "@bob -> multiply + division (#multiply-division)" in runtime_msg


def test_takeover_injects_handoff_guidance_from_recent_assignment(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": (
                "@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: "
                "alice writes add+subtract, bob writes multiply + division"
            ),
        })
        + "\n",
        json.dumps({
            "id": "m2",
            "sender_id": "emil-user",
            "text": "@alice-swe can you take over from @bob-swe instead",
        })
        + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "I will handle add and subtract."}),
        json.dumps({
            "type": "final",
            "answer": (
                "@bob-swe please RELEASE /workspace/shared/calculator.py#multiply-division "
                "so I can claim it."
            ),
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(2.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 2
    second_call = ctx["fake_chat"].messages[1]
    contents = [message["content"] for message in second_call]
    runtime_msg = next(content for content in contents if "Handoff request detected" in content)
    assert "/workspace/shared/calculator.py#multiply-division" in runtime_msg
    assert "RELEASE /workspace/shared/calculator.py#multiply-division" in runtime_msg


def test_skip_reason_silent_in_stub_mode(tmp_path, monkeypatch, capsys):
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "bob", "text": "random chatter, no mention"}) + "\n",
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted_replies=[])

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(0.8)
    ctx["stop"].set()
    t.join(timeout=5.0)

    captured = capsys.readouterr()
    assert "[skip]" not in captured.out


def test_skip_reason_printed_in_runpod_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_MODE", "runpod")
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "bob", "text": "random chatter, no mention"}) + "\n",
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted_replies=[])
    # _setup_run overwrites AGENT_MODE back to "stub"; re-set it after.
    monkeypatch.setenv("AGENT_MODE", "runpod")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(0.8)
    ctx["stop"].set()
    t.join(timeout=5.0)

    captured = capsys.readouterr()
    assert "[skip]" in captured.out
    assert "not addressed" in captured.out


def test_claim_continuation_creates_shared_calculator(tmp_path, monkeypatch):
    private = tmp_path / "alice"
    shared = tmp_path / "shared"
    private.mkdir()
    shared.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    monkeypatch.setenv("CLAIM_CONTINUATION_GRACE_SECONDS", "0")

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice-swe collaborate on /workspace/shared/calculator.py: alice writes add+subtract",
        })
        + "\n",
    ]
    calculator = (
        "def add(a, b):\n"
        "    return a + b\n\n"
        "def subtract(a, b):\n"
        "    return a - b\n\n"
        "def multiply(a, b):\n"
        "    raise NotImplementedError\n\n"
        "def divide(a, b):\n"
        "    raise NotImplementedError\n"
    )
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "CLAIM /workspace/shared/calculator.py#add-subtract: Implement add and subtract",
        }),
        json.dumps({
            "type": "tool_call",
            "tool": "create_file",
            "args": {"path": "/workspace/shared/calculator.py", "content": calculator},
        }),
        json.dumps({
            "type": "final",
            "answer": "Created /workspace/shared/calculator.py with add and subtract.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 2
    assert replies[0]["text"].startswith("CLAIM /workspace/shared/calculator.py#add-subtract")
    assert "Created /workspace/shared/calculator.py" in replies[1]["text"]
    assert (shared / "calculator.py").read_text(encoding="utf-8") == calculator
    assert not (private / "calculator.py").exists()

    events = _events(ctx["store"])
    assert any(kind == "claim_continuation" for _role, kind, _content in events)


def test_claim_from_continuation_gets_its_own_continuation(tmp_path, monkeypatch):
    private = tmp_path / "alice"
    shared = tmp_path / "shared"
    private.mkdir()
    shared.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    monkeypatch.setenv("CLAIM_CONTINUATION_GRACE_SECONDS", "0")

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": (
                "@alice-swe build a calculator in /workspace/shared/calculator.py. "
                "alice owns add/subtract. Write pytest tests next to it."
            ),
        })
        + "\n",
    ]
    test_content = (
        "from calculator import add, subtract\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n\n"
        "def test_subtract():\n"
        "    assert subtract(5, 3) == 2\n"
    )
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "CLAIM /workspace/shared/calculator.py#add-subtract: Implement add/subtract",
        }),
        json.dumps({
            "type": "final",
            "answer": "CLAIM /workspace/shared/test_calculator.py#tests: Add pytest coverage",
        }),
        json.dumps({
            "type": "tool_call",
            "tool": "create_file",
            "args": {"path": "/workspace/shared/test_calculator.py", "content": test_content},
        }),
        json.dumps({
            "type": "final",
            "answer": "Created /workspace/shared/test_calculator.py.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert [payload["text"] for payload in replies] == [
        "CLAIM /workspace/shared/calculator.py#add-subtract: Implement add/subtract",
        "CLAIM /workspace/shared/test_calculator.py#tests: Add pytest coverage",
        "Created /workspace/shared/test_calculator.py.",
    ]
    assert (shared / "test_calculator.py").read_text(encoding="utf-8") == test_content


def test_claim_continuation_llm_failure_does_not_send_false_failure_or_stop_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIM_CONTINUATION_GRACE_SECONDS", "0")
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "assigned: alice collaborate on /workspace/shared/calculator.py: alice writes add",
        })
        + "\n",
        json.dumps({
            "id": "m2",
            "sender_id": "emil-user",
            "text": "assigned: alice are you still online?",
        })
        + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "CLAIM /workspace/shared/calculator.py#add: Implement add",
        }),
        RuntimeError("openrouter: RateLimitRetryTimeout: stayed rate-limited"),
        json.dumps({"type": "final", "answer": "Yes, I am still online."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert [payload["text"] for payload in replies] == [
        "CLAIM /workspace/shared/calculator.py#add: Implement add",
        "Yes, I am still online.",
    ]
    assert not any("RateLimitRetryTimeout" in payload["text"] for payload in replies)

    events = _events(ctx["store"])
    assert any(kind == "claim_continuation" for _role, kind, _content in events)
    assert any(kind == "llm_failure" and "RateLimitRetryTimeout" in content for _role, kind, content in events)


def test_stale_unsatisfied_claim_injects_nudge_on_next_turn(tmp_path, monkeypatch):
    """Reproduces the "varför händer inget mera sen?" symptom: alice posted
    a CLAIM on a previous turn but never wrote, so her claim sits in the
    registry. On the next inbound message addressed to alice, the runtime
    must inject a guidance line listing the unsatisfied target so the model
    is reminded to either write or RELEASE.

    Uses a non-status prompt so the stale-claim nudge fires standalone (when
    the message is a status request, the nudge is folded into the status
    guidance instead — see the next test)."""

    claims = ClaimRegistry()
    claims.record_observed("alice", "/workspace/shared/calculator.py#add-subtract")
    # Sanity: the seeded claim is unsatisfied at the start of the next turn.
    assert len(claims.unsatisfied_claims_for("alice")) == 1

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe please continue"}) + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "Working on it."})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted, claims=claims)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 1
    contents = [message["content"] for message in ctx["fake_chat"].messages[0]]
    nudge = next(
        (content for content in contents if "unsatisfied active CLAIM" in content),
        None,
    )
    assert nudge is not None, "stale-claim guidance was not injected"
    assert "/workspace/shared/calculator.py#add-subtract" in nudge
    assert "RELEASE" in nudge


def test_status_request_with_open_claim_suppresses_stale_nudge(tmp_path, monkeypatch):
    """When the operator asks for status while an unsatisfied claim is open,
    the runtime must NOT layer the standalone stale-claim guidance (which
    invites RELEASE). The status guidance carries the claim into the
    Blockers field instead — see plan
    this-was-quite-a-refactored-balloon.md."""

    claims = ClaimRegistry()
    claims.record_observed("alice", "/workspace/shared/calculator.py#add-subtract")

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe are you done?"}) + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "Done: nothing yet. Tests: not run. Blockers: still working."})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted, claims=claims)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 1
    contents = [message["content"] for message in ctx["fake_chat"].messages[0]]
    status_guidance = next(
        (content for content in contents if "completion status" in content.lower()),
        None,
    )
    assert status_guidance is not None, "status_request_guidance not injected"
    assert "/workspace/shared/calculator.py#add-subtract" in status_guidance
    assert "instead of posting RELEASE" in status_guidance
    # Standalone stale-claim guidance must be suppressed; only the status-folded
    # version is allowed to mention unsatisfied claims.
    stale_alone = [
        content for content in contents
        if "unsatisfied active CLAIM" in content and "completion status" not in content.lower()
    ]
    assert stale_alone == [], f"stale-claim guidance leaked alongside status: {stale_alone}"


def test_satisfied_claim_does_not_re_inject_nudge(tmp_path, monkeypatch):
    """Once a claim is satisfied by a successful write, the next turn must
    NOT contain the stale-claim nudge — otherwise the model would be told
    to finish or RELEASE a claim it already wrote, encouraging spurious
    edits or releases."""

    claims = ClaimRegistry()
    claims.record_observed("alice", "/workspace/shared/calculator.py#add-subtract")
    claims.mark_satisfied("alice", "/workspace/shared/calculator.py")

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe what's the status?"}) + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "Done."})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted, claims=claims)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 1
    contents = [message["content"] for message in ctx["fake_chat"].messages[0]]
    assert not any("unsatisfied active CLAIM" in content for content in contents)
