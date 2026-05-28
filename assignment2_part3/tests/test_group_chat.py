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
    _ProjectState,
    _build_project_handler,
    _local_workspace_guidance,
    _project_root,
    _released_without_write_guidance,
    _remote_workspace_guidance,
    _stale_claim_guidance,
    load_system_prompt,
    run_group_chat,
)
from transport import StubTransport


def test_remote_workspace_guidance_rejects_wrong_remote_paths():
    guidance = _remote_workspace_guidance("emil_hjaertfors_bot", "project2")
    assert "/workspace/emil_hjaertfors_bot/project2/" in guidance
    assert "/sandbox" in guidance
    assert "/workspace/shared" in guidance
    assert "# file: <filename>" in guidance
    assert "exact saved /workspace/<agent>/<project>/<filename> path" in guidance


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


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _setup_run(
    tmp_path,
    monkeypatch,
    peer_lines,
    scripted_replies,
    claims=None,
    console_stdin_text="",
):
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
    console_stdout = io.StringIO()
    console = ConsoleControl(
        budget=budget,
        stop_event=stop,
        stdin=io.StringIO(console_stdin_text),
        stdout=console_stdout,
    )
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
        "console": console,
        "console_stdout": console_stdout,
    }


def _outbox_replies(outbox):
    return [json.loads(line) for line in outbox.getvalue().splitlines() if line.strip()]


def _events(store):
    cur = store.connection.execute("SELECT role, kind, content FROM events ORDER BY id")
    return list(cur.fetchall())


class FailingSendTransport(StubTransport):
    def send(self, text: str) -> bool:
        return False


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


def test_failed_send_is_not_recorded_as_delivered_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ID", "alice")
    monkeypatch.setenv("AGENT_DISPLAY_NAME", "alice-swe")
    monkeypatch.setenv("AGENT_MODE", "stub")

    fake_chat = FakeChat([json.dumps({"type": "final", "answer": "Reviewed foo."})])
    _patch_chat(monkeypatch, fake_chat)

    inbox = io.StringIO(
        json.dumps({"id": "m1", "sender_id": "human", "text": "@alice-swe review foo"}) + "\n"
    )
    outbox = io.StringIO()
    transport = FailingSendTransport("alice", inbox=inbox, outbox=outbox)
    store = SessionStore(str(tmp_path / "sess.sqlite3"))
    budget = Budget(
        tokens_per_minute=100_000,
        requests_per_minute=1000,
        lifetime_tokens=100_000,
        persist_path=tmp_path / "budget.json",
    )
    stop = threading.Event()

    t = threading.Thread(
        target=lambda: run_group_chat(
            transport=transport,
            budget=budget,
            store=store,
            stop_event=stop,
            idle_sleep=0.01,
        )
    )
    t.start()
    time.sleep(0.5)
    stop.set()
    t.join(timeout=5.0)

    assert outbox.getvalue() == ""
    events = _events(store)
    assert ("system", "send_failed", "msg_id=m1") in events
    assert not any(kind == "pending_followup" for _role, kind, _content in events)


def test_missing_part_request_guides_model_to_read_last_reported_file(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "human", "text": "@alice-swe build snake"}) + "\n",
        json.dumps({"id": "m2", "sender_id": "human", "text": "@alice-swe where is part 2/3"}) + "\n",
    ]
    scripted = [
        json.dumps(
            {
                "type": "final",
                "answer": "Current file path: /workspace/alice/project1/snake_game.html.",
            }
        ),
        json.dumps({"type": "final", "answer": "I will read it."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    assert _wait_for(lambda: ctx["fake_chat"].calls == 2, timeout=5.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    second_call_messages = ctx["fake_chat"].messages[1]
    joined = "\n".join(message["content"] for message in second_call_messages)
    assert "missing part or a resend" in joined
    assert "Call read_file on that exact path" in joined
    assert "/workspace/project1/snake_game.html" in joined
    assert "Do not regenerate" in joined


def test_direct_share_index_guides_model_to_read_active_project_file(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    project = shared / "webapp"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<main>Hello</main>\n", encoding="utf-8")
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "PROJECT: webapp\n@alice-swe, please share index.html with @alexia-kazim-agent",
        })
        + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Blocker: I will need to read the file before sharing it.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    assert _wait_for(lambda: ctx["fake_chat"].calls == 1, timeout=5.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    joined = "\n".join(message["content"] for message in ctx["fake_chat"].messages[0])
    assert "asking you to share index.html with a peer" in joined
    assert "Treat /workspace/shared/webapp/index.html as the source of truth" in joined
    assert "Call read_file on that exact path" in joined
    assert "Do not regenerate" in joined


def test_direct_share_missing_index_asks_for_exact_path(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    (shared / "webapp").mkdir(parents=True)
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "PROJECT: webapp\n@alice-swe, please share index.html with @alexia-kazim-agent",
        })
        + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Blocker: I need the exact workspace file path for index.html.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    assert _wait_for(lambda: ctx["fake_chat"].calls == 1, timeout=5.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    joined = "\n".join(message["content"] for message in ctx["fake_chat"].messages[0])
    assert "cannot find that file in the active project" in joined
    assert "ask for the exact workspace file path" in joined
    assert "do not go idle" in joined


def test_status_request_guidance_forbids_rewrite(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "human", "text": "@alice-swe build snake"}) + "\n",
        json.dumps({"id": "m2", "sender_id": "human", "text": "@alice-swe are you done?"}) + "\n",
    ]
    scripted = [
        json.dumps(
            {
                "type": "final",
                "answer": "Done: snake at /workspace/alice/project1/snake_game.html. Tests: not run. Blockers: none.",
            }
        ),
        json.dumps(
            {
                "type": "final",
                "answer": "Done: snake at /workspace/alice/project1/snake_game.html. Tests: not run. Blockers: none.",
            }
        ),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    assert _wait_for(lambda: ctx["fake_chat"].calls == 2, timeout=5.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    second_call_messages = ctx["fake_chat"].messages[1]
    joined = "\n".join(message["content"] for message in second_call_messages)
    assert "The operator is asking for completion status" in joined
    assert "Do not create, overwrite, or rewrite files just to answer a status request" in joined


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


def test_multipart_inbound_is_reassembled_before_reply_gate(tmp_path, monkeypatch):
    """Two `(part i/N)` messages from the same sender → one LLM round on the
    joined payload, not two on the fragments."""
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "bob",
            "text": "(part 1/2)\n@alice please review:\ndef add(a, b):",
        }) + "\n",
        json.dumps({
            "id": "m2",
            "sender_id": "bob",
            "text": "(part 2/2)\n    return a + b",
        }) + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "Reviewed: add looks correct."})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 1
    assert "Reviewed" in replies[0]["text"]
    # Exactly one LLM round — proves the parts were collapsed before the gate.
    assert ctx["fake_chat"].calls == 1
    # The LLM saw the joined body, with both halves of the function present.
    seen = json.dumps(ctx["fake_chat"].messages[-1])
    assert "def add(a, b):" in seen
    assert "return a + b" in seen
    # Neither raw "(part 1/2)" nor "(part 2/2)" should leak into the prompt —
    # the header is stripped during reassembly.
    assert "(part 1/2)" not in seen
    assert "(part 2/2)" not in seen


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
    # Fresh workspace so the runpod startup auto-allocates project1 instead of
    # deferring to the operator — otherwise the no-active-project gate fires
    # first and the reply-policy skip we want to assert never runs.
    private = tmp_path / "alice"
    private.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
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


def test_runpod_with_existing_projects_still_replies_without_writes(tmp_path, monkeypatch, capsys):
    """When the runpod agent boots and finds existing projectN/ dirs, it must
    NOT auto-select one (reconnect-safety). But it must also NOT park inbound
    — the agent should still converse, with file-write tools refused at
    dispatch until the operator runs :project new / :project use N."""

    private = tmp_path / "alice"
    private.mkdir()
    (private / "project1").mkdir()
    (private / "project2").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe please continue"}) + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "ack, no project yet"})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)
    monkeypatch.setenv("AGENT_MODE", "runpod")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    # The agent ran the LLM and emitted a reply despite no active project.
    assert ctx["fake_chat"].calls == 1
    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 1
    assert "no project yet" in replies[0]["text"]

    captured = capsys.readouterr()
    # Boot-time banner still lists the existing projects so a fresh
    # `docker attach` sees them.
    assert "[project?] existing: project1, project2" in captured.out
    # Per-inbound advisory tells the operator chat is live but writes aren't.
    assert "replying without writes" in captured.out

    events = _events(ctx["store"])
    # No park/skip events should fire any more.
    assert not any(kind == "inbound_parked_no_project" for _role, kind, _content in events)
    assert any(kind == "no_active_project_advisory" for _role, kind, _content in events)


def test_continue_retries_last_request_after_project_selection(tmp_path, monkeypatch):
    """Runpod reconnect flow: a task can arrive before the operator selects
    a project. After `:project new`, `:continue` should wake the agent and
    retry the same request with active-project guidance."""

    private = tmp_path / "alice"
    private.mkdir()
    (private / "project1").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice-swe create calculator.py with add and subtract",
        })
        + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "Cannot write yet; no active project."}),
        json.dumps({"type": "final", "answer": "Continuing in the active project."}),
    ]
    ctx = _setup_run(
        tmp_path,
        monkeypatch,
        peer_lines,
        scripted,
        console_stdin_text=":project new\n:continue\n",
    )
    monkeypatch.setenv("AGENT_MODE", "runpod")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    assert _wait_for(lambda: ctx["fake_chat"].calls == 1, timeout=5.0)
    ctx["console"].start()
    assert _wait_for(lambda: ctx["fake_chat"].calls == 2, timeout=5.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert (private / "project2").is_dir()
    assert "[continue queued]" in ctx["console_stdout"].getvalue()
    replies = _outbox_replies(ctx["outbox"])
    assert any("Continuing in the active project" in payload["text"] for payload in replies)

    second_call = ctx["fake_chat"].messages[1]
    combined = "\n".join(m.get("content", "") for m in second_call if isinstance(m, dict))
    assert "Operator typed :continue" in combined
    assert "Original request: @alice-swe create calculator.py" in combined
    assert "/workspace/alice/project2/" in combined

    events = _events(ctx["store"])
    assert any(kind == "operator_continue" for _role, kind, _content in events)


def test_runpod_with_no_existing_projects_auto_creates_project1(tmp_path, monkeypatch, capsys):
    """First boot in a fresh workspace has no choice to make — auto-create
    project1 and start replying immediately."""

    private = tmp_path / "alice"
    private.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe say hi"}) + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "hi"})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)
    monkeypatch.setenv("AGENT_MODE", "runpod")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    captured = capsys.readouterr()
    assert "[project] active=project1 (new)" in captured.out
    assert (private / "project1").is_dir()
    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 1
    assert replies[0]["text"] == "hi"


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


def test_task_status_acceptance_triggers_internal_continuation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    project = workspace / "alice" / "project1"
    project.mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice skapa en terminal-kalkylator",
        })
        + "\n",
    ]
    content = "def add(a, b):\n    return a + b\n"
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Bekräftat, jag tar: terminal-kalkylator",
        }),
        json.dumps({
            "type": "tool_call",
            "tool": "create_file",
            "args": {
                "path": "/workspace/alice/project1/calculator.py",
                "content": content,
            },
        }),
        json.dumps({
            "type": "final",
            "answer": (
                "Klar med: terminal-kalkylator. "
                "Filer: /workspace/alice/project1/calculator.py. Tester: inte körda."
            ),
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    # The agent id segment is stripped on the wire (see peer.scrub_outbound)
    # so peers cannot guess sibling project paths. The stripped form also
    # matches what the tool observation layer shows the LLM locally.
    assert [payload["text"] for payload in replies] == [
        "Bekräftat, jag tar: terminal-kalkylator",
        (
            "Klar med: terminal-kalkylator. "
            "Filer: /workspace/project1/calculator.py. Tester: inte körda."
        ),
    ]
    assert (project / "calculator.py").read_text(encoding="utf-8") == content
    assert ctx["fake_chat"].calls == 3

    events = _events(ctx["store"])
    assert any(kind == "task_status_continuation" for _role, kind, _content in events)


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


def test_intro_line_is_posted_once_then_suppressed(tmp_path, monkeypatch):
    """Defense-in-depth for the P3.7 "intro at most once" rule.

    The system prompt forbids re-posting "Hej, jag är ...", but the model
    occasionally regresses on long sessions. The runtime must drop the
    duplicate intro on the wire instead of sending it to the hub.
    """

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe please say hi"}) + "\n",
        json.dumps({"id": "m2", "sender_id": "emil-user", "text": "@alice-swe new agent joined, introduce again please"}) + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "Hej, jag är alice-swe"}),
        json.dumps({"type": "final", "answer": "Hej, jag är alice-swe"}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(2.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    intro_replies = [r for r in replies if r["text"].startswith("Hej, jag är")]
    assert len(intro_replies) == 1, f"intro line should be sent exactly once: {replies}"

    events = _events(ctx["store"])
    assert any(kind == "intro_suppressed" for _role, kind, _content in events)


def test_prior_intro_in_session_log_suppresses_restart_intro(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe please say hi"}) + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "Hej, jag är alice-swe"}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)
    ctx["store"].record("assistant", "peer_reply_raw", "Hej, jag är alice-swe")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert _outbox_replies(ctx["outbox"]) == []
    events = _events(ctx["store"])
    assert any(kind == "intro_suppressed" for _role, kind, _content in events)


def test_direct_actionable_intro_reprompts_not_sent(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice-swe, please share index.html with @alexia-kazim-agent",
        })
        + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "Hej, jag är alice-swe"}),
        json.dumps({
            "type": "final",
            "answer": "Blocker: I need the exact workspace file path for index.html.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    assert _wait_for(lambda: ctx["fake_chat"].calls == 2, timeout=5.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert [reply["text"] for reply in replies] == [
        "Blocker: I need the exact workspace file path for index.html."
    ]
    events = _events(ctx["store"])
    assert any(kind == "user_action_non_action_reprompt" for _role, kind, _content in events)


def test_presence_reply_is_not_misclassified_as_intro(tmp_path, monkeypatch):
    """A Swedish presence answer starts like the intro but is not the canonical
    one-line intro and must still reach the hub."""

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe please say hi"}) + "\n",
        json.dumps({"id": "m2", "sender_id": "emil-user", "text": "@alice-swe are you here?"}) + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "Hej, jag är alice-swe"}),
        json.dumps({"type": "final", "answer": "Hej, jag är här."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(2.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert [r["text"] for r in replies] == ["Hej, jag är alice-swe", "Hej, jag är här."]

    events = _events(ctx["store"])
    assert not any(kind == "intro_suppressed" for _role, kind, _content in events)


def test_empty_acknowledgment_reply_is_suppressed(tmp_path, monkeypatch):
    """Reply-discipline runtime gate: "Okej, jag förstår. Jag avvaktar..." is
    a content-free reply and never reaches the hub."""

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe heads up, peer wrote game.py"}) + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Okej, jag förstår. Jag avvaktar nästa instruktion.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert replies == [], f"empty acknowledgment must not leave the runtime: {replies}"

    events = _events(ctx["store"])
    assert any(kind == "acknowledgment_suppressed" for _role, kind, _content in events)


def test_direct_coordinator_assignment_reprompts_suppressed_intro(tmp_path, monkeypatch):
    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": (
                "@alice-swe, You are the manager/coordinator for this task; "
                "delegate implementation, testing, review, bug checking, and documentation."
            ),
        })
        + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "Hej, jag är alice-swe"}),
        json.dumps({
            "type": "final",
            "answer": (
                "Confirmed, I'll take: coordination\n"
                "TASK @bob-swe: implement calculator.py\n"
                "TASK @carol-swe: write pytest tests"
            ),
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(2.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 1
    assert replies[0]["text"].startswith("Confirmed, I'll take: coordination")
    assert "TASK @bob-swe" in replies[0]["text"]
    assert ctx["fake_chat"].calls == 2

    first_call = "\n".join(
        m.get("content", "") for m in ctx["fake_chat"].messages[0] if isinstance(m, dict)
    )
    assert "Direct coordinator assignment detected" in first_call

    events = _events(ctx["store"])
    assert any(kind == "intro_suppressed" for _role, kind, _content in events)
    assert any(kind == "direct_assignment_reprompt" for _role, kind, _content in events)
    assert any(
        kind == "task_status_continuation_skipped"
        and "coordination role status" in content
        for _role, kind, content in events
    )


def test_substantive_reply_is_not_suppressed(tmp_path, monkeypatch):
    """Sanity: a real status reply (no empty-ack markers) goes out unchanged."""

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe what file should I touch?"}) + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Looking at /workspace/shared/calc.py first — propose a #division scope.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.5)
    ctx["stop"].set()
    t.join(timeout=5.0)

    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 1
    assert "#division" in replies[0]["text"]


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


def test_local_workspace_guidance_invariants(tmp_path):
    """Local-mode guidance must invert the remote stanza: peers CAN read shared
    files, CLAIM is mandatory before any write, and the agent must not redirect
    to a private workspace when the operator named a shared path."""

    guidance = _local_workspace_guidance("alice-swe", tmp_path / "calc")
    assert "/workspace/shared/calc/" in guidance
    assert "CLAIM /workspace/shared/" in guidance
    assert "RELEASE /workspace/shared/" in guidance
    # Inverse of the remote rule — the local hub explicitly allows shared writes.
    assert "do not redirect" in guidance.lower()
    # The remote-hub `# file: <filename>` payload convention should NOT appear here.
    assert "# file: <filename>" not in guidance


def test_named_project_inferred_from_inbound_shared_path(tmp_path, monkeypatch):
    """A `/workspace/shared/<name>/...` path in the operator broadcast must
    set the active project to `<name>` (not a flat `shared/` write) so peers
    write into a co-visible project subfolder."""

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    monkeypatch.setenv("CLAIM_CONTINUATION_GRACE_SECONDS", "0")

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": (
                "@alice-swe build a calculator in /workspace/shared/calc/calculator.py. "
                "alice owns add/subtract."
            ),
        })
        + "\n",
    ]
    calc_src = "def add(a, b):\n    return a + b\n"
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "CLAIM /workspace/shared/calc/calculator.py#add-subtract: implement add",
        }),
        json.dumps({
            "type": "tool_call",
            "tool": "create_file",
            "args": {"path": "/workspace/shared/calc/calculator.py", "content": calc_src},
        }),
        json.dumps({
            "type": "final",
            "answer": "Created /workspace/shared/calc/calculator.py.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert (shared / "calc" / "calculator.py").read_text(encoding="utf-8") == calc_src
    # The flat-file landing-on-shared-root path must NOT be used.
    assert not (shared / "calculator.py").exists()

    events = _events(ctx["store"])
    inferred = [
        content for _role, kind, content in events if kind == "project_set_from_inbound"
    ]
    assert any("name=calc" in content for content in inferred)


def test_project_directive_overrides_path(tmp_path, monkeypatch):
    """A `PROJECT: <name>` directive in the inbound must win over any
    `/workspace/shared/<other>/...` path mentioned in the same message."""

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    monkeypatch.setenv("CLAIM_CONTINUATION_GRACE_SECONDS", "0")

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": (
                "PROJECT: foo\n"
                "@alice-swe collaborate in /workspace/shared/calc/calculator.py: "
                "alice owns add"
            ),
        })
        + "\n",
    ]
    scripted = [
        json.dumps({
            "type": "final",
            "answer": "Ack.",
        }),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    events = _events(ctx["store"])
    inferred = [
        content for _role, kind, content in events if kind == "project_set_from_inbound"
    ]
    assert any("name=foo" in content for content in inferred)
    assert not any("name=calc" in content for content in inferred)


def test_peer_claim_blocks_auto_save_in_shared(tmp_path, monkeypatch):
    """When a peer holds an active CLAIM on a file, an incoming code block for
    that same file must NOT be auto-saved to the shared project — saving would
    overwrite the claim-holder's in-flight work. A log row records the skip
    and the runtime guidance nudges the agent to read_file instead."""

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    monkeypatch.setenv("CLAIM_CONTINUATION_GRACE_SECONDS", "0")

    # Pre-seed: bob has already claimed calculator.py#multiply.
    registry = ClaimRegistry()
    registry.record_observed("bob", "/workspace/shared/calc/calculator.py#multiply")

    peer_lines = [
        # First message anchors the project so `project_state.active` is set.
        json.dumps({
            "id": "m0",
            "sender_id": "emil-user",
            "text": "@alice-swe project in /workspace/shared/calc/calculator.py",
        })
        + "\n",
        # Then bob posts code for the file he has under CLAIM. Auto-save must skip.
        json.dumps({
            "id": "m1",
            "sender_id": "bob",
            "text": (
                "Sharing my in-progress code:\n\n"
                "```python\n"
                "# file: calculator.py\n"
                "def multiply(a, b):\n"
                "    return a * b\n"
                "```\n"
            ),
        })
        + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "Ack."}),
        json.dumps({"type": "final", "answer": "Noted, will read_file before editing."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted, claims=registry)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    # Poll for the expected event rather than sleeping a fixed wall-clock duration;
    # under a loaded test suite a fixed sleep can stop the runner before bob's
    # second message is even read off the inbox.
    deadline = time.time() + 10.0
    skipped: list[str] = []
    while time.time() < deadline:
        events = _events(ctx["store"])
        skipped = [
            content for _role, kind, content in events if kind == "code_save_skipped_claim_conflict"
        ]
        if skipped:
            break
        time.sleep(0.05)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert skipped, "expected a code_save_skipped_claim_conflict event"
    assert any("calculator.py" in row for row in skipped)
    # The file must not have been written by the auto-save path.
    assert not (shared / "calc" / "calculator.py").exists()


# ---------------------------------------------------------------------------
# Phase 2: PROJECT-directive auto-allocate + system-prompt cleanup
# ---------------------------------------------------------------------------


def test_remote_mode_auto_allocates_on_project_directive(tmp_path, monkeypatch, capsys):
    """Remote (runpod) mode + no active project + inbound carries
    `PROJECT: <name>` → the runtime allocates the next numeric `projectN`
    and processes the message in the same round. No skip, no
    `everyone continue` required."""

    private = tmp_path / "alice"
    private.mkdir()
    (private / "project1").mkdir()  # force "existing projects" branch on startup
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice-swe build a calculator.\nPROJECT: calc",
        })
        + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "On it."})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)
    monkeypatch.setenv("AGENT_MODE", "runpod")  # flip after _setup_run

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.2)
    ctx["stop"].set()
    t.join(timeout=5.0)

    events = _events(ctx["store"])
    alloc = [
        content for _role, kind, content in events if kind == "project_auto_allocated"
    ]
    assert alloc, "expected a project_auto_allocated event"
    assert any("reason=directive" in row and "directive_name=calc" in row for row in alloc)
    # The dir name is numeric in remote mode (not "calc").
    assert any("name=project2" in row for row in alloc)
    # The LLM round ran for this message — no skip.
    assert ctx["fake_chat"].calls == 1


def test_remote_mode_auto_allocates_on_peer_shared_code(tmp_path, monkeypatch):
    """Remote (runpod) mode + no active project + inbound carries a fenced
    code block but NO `PROJECT:` directive → the runtime allocates the next
    numeric `projectN`, saves the block to disk, and a second fenced-code
    inbound lands in the SAME project (stickiness)."""

    private = tmp_path / "alice"
    private.mkdir()
    # Force the startup "existing projects found" branch so the runtime defers
    # allocation to the inbound — otherwise an empty workspace auto-allocates
    # project1 at boot and our message can't drive the code-share branch.
    (private / "project1").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    first_text = (
        "I will take the role of writing the main game logic.\n"
        "```python\n"
        "# file: game.py\n"
        "def play():\n"
        "    return 42\n"
        "```\n"
        "Next, I will wait for others."
    )
    second_text = (
        "Here is the helper too.\n"
        "```python\n"
        "# file: helper.py\n"
        "def helper():\n"
        "    return 1\n"
        "```"
    )
    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "sonia-agent", "text": first_text}) + "\n",
        json.dumps({"id": "m2", "sender_id": "sonia-agent", "text": second_text}) + "\n",
    ]
    scripted = [
        json.dumps({"type": "final", "answer": "ack"}),
        json.dumps({"type": "final", "answer": "ack2"}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)
    monkeypatch.setenv("AGENT_MODE", "runpod")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.6)
    ctx["stop"].set()
    t.join(timeout=5.0)

    events = _events(ctx["store"])
    alloc = [
        content for _role, kind, content in events
        if kind == "project_auto_allocated_from_code"
    ]
    assert alloc, "expected a project_auto_allocated_from_code event"
    assert any(
        "name=project2" in row and "sender=sonia-agent" in row for row in alloc
    )
    # Exactly one auto-allocation across the two inbounds (stickiness).
    assert len(alloc) == 1
    # The block from message 1 landed under project2 (project1 was pre-created).
    assert (private / "project2" / "game.py").is_file()
    assert "def play()" in (private / "project2" / "game.py").read_text()
    # The block from message 2 also landed under the same project2.
    assert (private / "project2" / "helper.py").is_file()
    # No PROJECT-directive-based allocation event was logged.
    assert not any(
        kind == "project_auto_allocated" for _role, kind, _content in events
    )


def test_no_directive_still_converses_without_auto_allocating(tmp_path, monkeypatch, capsys):
    """An inbound with neither `PROJECT: <name>` nor any fenced code block
    must NOT auto-allocate a project (that's the reconnect-safety brake),
    but the agent should still run the LLM and converse. Writes are blocked
    at dispatch instead of at the inbound gate."""

    private = tmp_path / "alice"
    private.mkdir()
    (private / "project1").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice-swe ping",
        })
        + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "pong"})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)
    monkeypatch.setenv("AGENT_MODE", "runpod")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    assert ctx["fake_chat"].calls == 1
    replies = _outbox_replies(ctx["outbox"])
    assert len(replies) == 1
    assert replies[0]["text"] == "pong"
    events = _events(ctx["store"])
    # No project_auto_allocated event — only an explicit PROJECT: directive
    # can flip the safety brake.
    assert not any(kind == "project_auto_allocated" for _role, kind, _content in events)


def test_local_mode_auto_allocates_named_dir_on_project_directive(tmp_path, monkeypatch):
    """Local-shared mode + inbound carries `PROJECT: <name>` → the active
    project is `<shared>/<name>/`, not a numeric `projectN`. (Verifies the
    existing local-mode lazy-inference path still wins in shared mode.)"""

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    monkeypatch.setenv("CLAIM_CONTINUATION_GRACE_SECONDS", "0")

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice-swe build a calculator.\nPROJECT: calc",
        })
        + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "ack"})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    events = _events(ctx["store"])
    inferred = [
        content for _role, kind, content in events if kind == "project_set_from_inbound"
    ]
    assert any("name=calc" in row for row in inferred)
    # No numeric project allocation in local-shared mode.
    assert not any(kind == "project_auto_allocated" for _role, kind, _content in events)
    assert (shared / "calc").is_dir()


def test_local_workspace_guidance_includes_full_claim_contract():
    """`_local_workspace_guidance` is the sole source of truth for the
    local-mode CLAIM/RELEASE/DEFER protocol after the system-prompt cleanup.
    It must carry the full contract that used to live in P3.9 of the prompt."""

    guidance = _local_workspace_guidance("alice-swe", Path("/tmp/calc"))
    # Core protocol verbs
    for term in ("CLAIM", "RELEASE", "DEFER"):
        assert term in guidance
    # Specific rules pulled out of the prompt
    assert "read_file" in guidance
    assert "lexicographically" in guidance
    assert "whole file" in guidance.lower() or "whole-file" in guidance.lower()
    assert "JSON envelope" in guidance or '{"type":"final"' in guidance
    # The "no assert without observation" rule
    assert "re-read" in guidance.lower() or "tool_observation" in guidance.lower()


def test_system_prompt_has_no_workspace_or_claim_policy_content():
    """Regression guard for the prompt cleanup: the system prompt is now
    mode-agnostic. Folder-policy stanzas (P3.8) and the full claim/defer
    protocol (P3.9 + tie-break) are owned by the runtime guidance helpers,
    not by the prompt."""

    prompt = load_system_prompt("alice", "alice-swe")
    # Section headings — these were the bodies that moved out.
    for heading in (
        "P3.8",
        "P3.9",
        "Workspace layout",
        "Claim/defer protocol",
        "Tie-break for racing CLAIMs",
    ):
        assert heading not in prompt, f"{heading!r} should no longer be in the prompt"
    # The per-agent path template is policy text; it shouldn't survive.
    assert "/workspace/{AGENT_ID}" not in prompt
    # Protocol-line templates (CLAIM/RELEASE plus the path shape).
    assert "CLAIM /workspace/shared/" not in prompt
    assert "RELEASE /workspace/shared/" not in prompt


# ---------------------------------------------------------------------------
# Runpod no-active-project: chat works, file writes are refused at dispatch
# ---------------------------------------------------------------------------


def test_runpod_no_project_refuses_write_tools(tmp_path, monkeypatch, capsys):
    """When runpod mode has no active project, the model can still talk, but
    every CLAIM_GATED write tool (create_file/append_text/edit_section/
    replace_text/rename_file) is refused at dispatch with the
    NO_PROJECT_REFUSAL observation. Nothing lands on disk."""

    from peer_task import NO_PROJECT_REFUSAL

    private = tmp_path / "alice"
    private.mkdir()
    (private / "project1").mkdir()  # existing → boot defers active selection
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    peer_lines = [
        json.dumps({
            "id": "m1",
            "sender_id": "emil-user",
            "text": "@alice-swe please create foo.py with print('hi')",
        })
        + "\n",
    ]
    # The model first emits a create_file (which the runtime refuses) and
    # then a final answer.
    scripted = [
        json.dumps({
            "type": "tool_call",
            "tool": "create_file",
            "args": {"path": "foo.py", "content": "print('hi')\n"},
        }),
        json.dumps({"type": "final", "answer": "Cannot write yet — no active project."}),
    ]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)
    monkeypatch.setenv("AGENT_MODE", "runpod")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    # No project1/foo.py should exist (write refused).
    assert not (private / "project1" / "foo.py").exists()
    assert not (private / "foo.py").exists()

    # The refusal was logged as a no_project_block system event.
    events = _events(ctx["store"])
    blocks = [
        content for _role, kind, content in events if kind == "no_project_block"
    ]
    assert blocks, "expected a no_project_block event"
    assert NO_PROJECT_REFUSAL in blocks[0]

    # The second scripted reply reached the wire.
    replies = _outbox_replies(ctx["outbox"])
    assert any("Cannot write yet" in payload["text"] for payload in replies)


def test_runpod_no_project_passes_conversation_guidance(tmp_path, monkeypatch):
    """The `_no_project_conversation_guidance()` text must be visible in the
    prompt sent to `complete_chat_with_metadata` so the model knows it can
    talk but not write."""

    private = tmp_path / "alice"
    private.mkdir()
    (private / "project1").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    peer_lines = [
        json.dumps({"id": "m1", "sender_id": "emil-user", "text": "@alice-swe ping"}) + "\n",
    ]
    scripted = [json.dumps({"type": "final", "answer": "pong"})]
    ctx = _setup_run(tmp_path, monkeypatch, peer_lines, scripted)
    monkeypatch.setenv("AGENT_MODE", "runpod")

    t = threading.Thread(target=ctx["runner"])
    t.start()
    time.sleep(1.0)
    ctx["stop"].set()
    t.join(timeout=5.0)

    # The guidance string is appended to the runtime context for the LLM call.
    assert ctx["fake_chat"].calls >= 1
    seen = ctx["fake_chat"].messages[0]
    combined = "\n".join(m.get("content", "") for m in seen if isinstance(m, dict))
    # Marker phrase from `_no_project_conversation_guidance()`.
    assert "no active project is allocated yet" in combined


# ---------------------------------------------------------------------------
# AGENT_MODE=local: docker-compose local-profile default
# ---------------------------------------------------------------------------


def test_project_root_treats_agent_mode_local_as_shared(tmp_path, monkeypatch):
    """`AGENT_MODE=local` (the docker-compose local-profile default) must
    activate shared mode when `SHARED_WORKSPACE` is set, matching how
    `agent-alice` / `agent-bob` containers run. Pins the contract so the
    compose default flip stays load-bearing."""

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))

    root, is_shared = _project_root("local", tmp_path / "alice")
    assert root == shared
    assert is_shared is True

    monkeypatch.delenv("SHARED_WORKSPACE", raising=False)
    root, is_shared = _project_root("local", tmp_path / "alice")
    assert root is None
    assert is_shared is False

    # And runpod still wins regardless of SHARED_WORKSPACE.
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    root, is_shared = _project_root("runpod", tmp_path / "alice")
    assert root == tmp_path / "alice"
    assert is_shared is False


# ---------------------------------------------------------------------------
# Project handler: `:project new/use/info/list` mutates state correctly
# ---------------------------------------------------------------------------


def test_project_handler_mutates_active_for_each_command(tmp_path):
    """`_build_project_handler` is called from the console daemon thread.
    Mutating commands (`new`, `use N`) update `project_state.active`; reading
    commands (`info`, `list`) do not. Invalid `use N` is a no-op error."""

    root = tmp_path / "alice"
    root.mkdir()
    (root / "project1").mkdir()

    state = _ProjectState()
    state.root = root
    state.active = None

    handler = _build_project_handler(state)

    # :project new -> allocates project2
    out = handler("new", [])
    assert out.startswith("active=project2")
    assert state.active is not None
    assert state.active.name == "project2"

    # :project use 1 -> switches to project1
    out = handler("use", ["1"])
    assert out == "active=project1"
    assert state.active.name == "project1"

    # :project info -> READ only
    out = handler("info", [])
    assert out == "active=project1"
    assert state.active.name == "project1"

    # :project list -> READ only
    handler("list", [])
    assert state.active.name == "project1"

    # :project use with invalid N -> error, state unchanged
    out = handler("use", ["999999"])
    assert out.startswith("[project error]")
    assert state.active.name == "project1"
