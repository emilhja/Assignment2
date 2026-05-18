import agent
import pytest


# These tests replace the model and shell tool so only agent logic is tested.
def test_intent_refusal_happens_before_llm(monkeypatch, capsys):
    def fail_if_called(_messages):
        raise AssertionError("LLM should not be called for forbidden intent")

    monkeypatch.setattr(agent, "complete_chat", fail_if_called)

    agent.run_task("Delete everything in /workspace")

    output = capsys.readouterr().out
    assert "Final answer:" in output
    assert "I cannot help delete everything" in output


def test_blocked_command_stops_without_retry(monkeypatch, capsys):
    calls = 0

    def fake_complete_chat(_messages):
        nonlocal calls
        calls += 1
        return "Thought: I will inspect Docker.\nAction: bash\nCommand: docker compose ps"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    # Blocked commands should stop before the user is asked to confirm them.
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda _command: pytest.fail("blocked commands should not ask for confirmation"),
    )

    agent.run_task("Check containers")

    output = capsys.readouterr().out
    assert calls == 1
    assert "Final answer:" in output
    assert "Run Docker on the host machine instead" in output


def test_pwd_observation_becomes_final_answer(monkeypatch, capsys):
    calls = 0

    def fake_complete_chat(_messages):
        nonlocal calls
        calls += 1
        return "Thought: I need the current directory.\nAction: bash\nCommand: pwd"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    # Return a fake pwd result so the test does not depend on the real cwd.
    monkeypatch.setattr(agent, "run_bash", lambda _command: "/app")

    agent.run_task("Show me the current directory")

    output = capsys.readouterr().out
    assert calls == 1
    assert "Final answer:\n/app" in output
    assert "/app/workspace" not in output


def test_cat_observation_becomes_final_answer(monkeypatch, capsys):
    calls = 0

    def fake_complete_chat(_messages):
        nonlocal calls
        calls += 1
        return "Thought: I need to read the file.\nAction: bash\nCommand: cat /workspace/demo.txt"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_bash", lambda _command: "hello from demo")

    agent.run_task("Show the contents of /workspace/demo.txt")

    output = capsys.readouterr().out
    assert calls == 1
    assert "Final answer:\nhello from demo" in output


def test_internal_trace_is_hidden_by_default(monkeypatch, capsys):
    def fake_complete_chat(_messages):
        return "Thought: Simple arithmetic.\nFinal Answer: 4"

    monkeypatch.delenv("AGENT_DEBUG", raising=False)
    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)

    agent.run_task("what is 2+2")

    output = capsys.readouterr().out
    assert "Assistant raw response:" not in output
    assert "--- Step" not in output
    assert "Final answer:\n4" in output


def test_internal_trace_can_be_enabled(monkeypatch, capsys):
    def fake_complete_chat(_messages):
        return "Thought: Simple arithmetic.\nFinal Answer: 4"

    monkeypatch.setenv("AGENT_DEBUG", "1")
    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)

    agent.run_task("what is 2+2")

    output = capsys.readouterr().out
    assert "--- Step 1 ---" in output
    assert "Assistant raw response:" in output
    assert "Thought: Simple arithmetic." in output
    assert "Final answer:\n4" in output
