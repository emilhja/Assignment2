import agent
import pytest


def test_final_answer_prints_without_tool_call(monkeypatch, capsys):
    def fake_complete_chat(_messages):
        return "Thought: Simple arithmetic.\nFinal Answer: 4"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)

    agent.run_task("what is 2+2")

    output = capsys.readouterr().out
    assert "Final answer:\n4" in output


def test_command_output_is_returned_as_observation(monkeypatch, capsys):
    calls = []

    def fake_complete_chat(messages):
        calls.append(messages)
        if len(calls) == 1:
            return "Thought: I need the current directory.\nAction: bash\nCommand: pwd"
        assert messages[-1] == {"role": "user", "content": "Observation: /app"}
        return "Thought: I have the observation.\nFinal Answer: /app"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_bash", lambda _command: "/app")

    agent.run_task("Show me the current directory")

    output = capsys.readouterr().out
    assert len(calls) == 2
    assert "Final answer:\n/app" in output


def test_broad_delete_intent_is_refused_before_model_call(monkeypatch, capsys):
    monkeypatch.setattr(
        agent,
        "complete_chat",
        lambda _messages: pytest.fail("broad delete intent should not call the model"),
    )
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda _command: pytest.fail("blocked commands should not ask for confirmation"),
    )
    monkeypatch.setattr(
        agent,
        "run_bash",
        lambda _command: pytest.fail("blocked commands should not execute"),
    )

    agent.run_task("Delete everything in /workspace")

    output = capsys.readouterr().out
    assert "Final answer:\nI cannot do that. Deleting everything is not allowed." in output


def test_blocked_model_command_gets_immediate_feedback(monkeypatch, capsys):
    def fake_complete_chat(_messages):
        return "Thought: I will remove files.\nAction: bash\nCommand: rm -rf /workspace"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda _command: pytest.fail("blocked commands should not ask for confirmation"),
    )
    monkeypatch.setattr(
        agent,
        "run_bash",
        lambda _command: pytest.fail("blocked commands should not execute"),
    )

    agent.run_task("Clean up /workspace")

    output = capsys.readouterr().out
    assert "Final answer:\nI cannot run that command. Blocked:" in output


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
