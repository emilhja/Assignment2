import pytest

from part1 import agent


def test_final_answer_prints_without_tool_call(monkeypatch, capsys):
    monkeypatch.setattr(
        agent,
        "complete_chat",
        lambda _messages: "Thought: Simple arithmetic.\nFinal Answer: 4",
    )

    agent.run_task("what is 2+2")

    assert "Final answer:\n4" in capsys.readouterr().out


def test_command_output_is_returned_as_observation(monkeypatch, capsys):
    calls = []

    def fake_complete_chat(messages):
        calls.append(messages)
        if len(calls) == 1:
            return "Thought: I need pwd.\nAction: bash\nCommand: pwd"
        assert messages[-1] == {"role": "user", "content": "Observation: /app"}
        return "Thought: I have it.\nFinal Answer: /app"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_bash", lambda _command: "/app")

    agent.run_task("Show pwd")

    output = capsys.readouterr().out
    assert len(calls) == 2
    assert "Final answer:\n/app" in output


def test_invalid_response_gets_guidance_and_retries(monkeypatch, capsys):
    calls = []

    def fake_complete_chat(messages):
        calls.append(messages)
        if len(calls) == 1:
            return "I should run pwd"
        assert "Parser error: missing Thought line" in messages[-1]["content"]
        return "Thought: Fixed.\nFinal Answer: done"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)

    agent.run_task("Show pwd")

    output = capsys.readouterr().out
    assert len(calls) == 2
    assert "Final answer:\ndone" in output


def test_broad_delete_intent_is_refused_before_model_call(monkeypatch, capsys):
    monkeypatch.setattr(
        agent,
        "complete_chat",
        lambda _messages: pytest.fail("refused task should not call model"),
    )

    agent.run_task("Delete everything in /workspace")

    assert "I cannot do that. Deleting everything is not allowed." in capsys.readouterr().out


def test_blocked_model_command_does_not_confirm_or_execute(monkeypatch, capsys):
    monkeypatch.setattr(
        agent,
        "complete_chat",
        lambda _messages: "Thought: Remove files.\nAction: bash\nCommand: rm -rf /workspace",
    )
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda _command: pytest.fail("blocked command should not ask confirmation"),
    )
    monkeypatch.setattr(
        agent,
        "run_bash",
        lambda _command: pytest.fail("blocked command should not execute"),
    )

    agent.run_task("Clean workspace")

    assert "I cannot run that command. Blocked:" in capsys.readouterr().out


def test_denied_command_becomes_observation(monkeypatch, capsys):
    calls = []

    def fake_complete_chat(messages):
        calls.append(messages)
        if len(calls) == 1:
            return "Thought: Need pwd.\nAction: bash\nCommand: pwd"
        assert "user denied the command" in messages[-1]["content"]
        return "Thought: I should stop.\nFinal Answer: command denied"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: False)
    monkeypatch.setattr(
        agent,
        "run_bash",
        lambda _command: pytest.fail("denied command should not execute"),
    )

    agent.run_task("Show pwd")

    assert "Final answer:\ncommand denied" in capsys.readouterr().out


def test_stops_after_max_steps(monkeypatch, capsys):
    monkeypatch.setattr(agent, "MAX_STEPS", 2)
    monkeypatch.setattr(
        agent,
        "complete_chat",
        lambda _messages: "Thought: Need more.\nAction: bash\nCommand: pwd",
    )
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_bash", lambda _command: "/app")

    agent.run_task("Loop")

    assert "Stopped after 2 steps without a final answer." in capsys.readouterr().out


def test_debug_trace_can_be_enabled(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_DEBUG", "1")
    monkeypatch.setattr(
        agent,
        "complete_chat",
        lambda _messages: "Thought: Simple arithmetic.\nFinal Answer: 4",
    )

    agent.run_task("what is 2+2")

    output = capsys.readouterr().out
    assert "--- Step 1 ---" in output
    assert "Assistant raw response:" in output
