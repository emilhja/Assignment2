import json

import agent
import pytest


def _set_session_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSION_DB", str(tmp_path / "session.sqlite3"))


def test_intent_refusal_happens_before_llm(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)

    def fail_if_called(_messages):
        raise AssertionError("LLM should not be called for forbidden intent")

    monkeypatch.setattr(agent, "complete_chat", fail_if_called)

    agent.run_task("Delete everything in /workspace")

    output = capsys.readouterr().out
    assert "Final answer:" in output
    assert "I cannot help delete everything" in output


def test_invalid_bash_args_observation_returns_to_model(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    observations_seen = []

    responses = [
        json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": ""},
                "reason": "bad call",
            }
        ),
        json.dumps({"type": "final", "answer": "I cannot run an empty command."}),
    ]

    def fake_complete_chat(messages):
        if len(messages) >= 4:
            observations_seen.append(messages[-1]["content"])
        return responses.pop(0)

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda _command: pytest.fail("invalid args should not ask for confirmation"),
    )

    agent.run_task("Inspect something")

    output = capsys.readouterr().out
    assert "Final answer:\nI cannot run an empty command." in output
    assert any("Tool error: bash requires a non-empty string command." in obs for obs in observations_seen)


def test_multiple_tool_rounds_before_final(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    responses = [
        json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "pwd"},
                "reason": "need cwd",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "ls -la /workspace"},
                "reason": "need files",
            }
        ),
        json.dumps({"type": "final", "answer": "The workspace has files."}),
    ]

    def fake_complete_chat(messages):
        if len(messages) >= 4:
            assert "tool_observation" in messages[-1]["content"]
        return responses.pop(0)

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_tool", lambda _tool, _args: "tool output")

    agent.run_task("Inspect the workspace")

    output = capsys.readouterr().out
    assert "Final answer:\nThe workspace has files." in output
    assert responses == []


def test_model_drives_edit_then_show(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    tool_calls = []
    confirmed_commands = []

    responses = [
        json.dumps(
            {
                "type": "tool_call",
                "tool": "replace_text",
                "args": {
                    "path": "/workspace/demo.txt",
                    "old_text": "draft",
                    "new_text": "done",
                    "all_occurrences": False,
                },
                "reason": "perform the edit",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "cat /workspace/demo.txt"},
                "reason": "show the result",
            }
        ),
        json.dumps({"type": "final", "answer": "status: done"}),
    ]

    def fake_complete_chat(_messages):
        return responses.pop(0)

    def fake_run_tool(tool, args):
        tool_calls.append((tool, args.copy()))
        if tool == "replace_text":
            return "Replaced 1 occurrence(s) in /workspace/demo.txt."
        if tool == "bash":
            return "status: done"
        raise AssertionError(f"unexpected tool: {tool}")

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda command: confirmed_commands.append(command) or True,
    )

    answer = agent.run_task(
        'change "draft" to "done" in /workspace/demo.txt and then show it'
    )

    output = capsys.readouterr().out
    assert answer == "status: done"
    assert "Final answer:\nstatus: done" in output
    assert tool_calls == [
        (
            "replace_text",
            {
                "path": "/workspace/demo.txt",
                "old_text": "draft",
                "new_text": "done",
                "all_occurrences": False,
            },
        ),
        ("bash", {"command": "cat /workspace/demo.txt"}),
    ]
    assert confirmed_commands == ["cat /workspace/demo.txt"]
    assert responses == []


def test_prior_context_is_sent_to_llm(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    seen_messages = []

    def fake_complete_chat(messages):
        seen_messages.extend(message.copy() for message in messages)
        return json.dumps({"type": "final", "answer": "Read /workspace/demo.txt"})

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)

    answer = agent.run_task(
        "you can open and read it",
        prior_context=["User: In /workspace/demo.txt, replace draft with done\nAssistant: Done"],
    )

    output = capsys.readouterr().out
    assert answer == "Read /workspace/demo.txt"
    assert "Final answer:\nRead /workspace/demo.txt" in output
    assert any("Recent CLI turns" in message["content"] for message in seen_messages)
    assert any("/workspace/demo.txt" in message["content"] for message in seen_messages)


def test_invalid_json_gets_parser_guidance(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    responses = iter(
        [
            "not json",
            json.dumps({"type": "final", "answer": "Recovered"}),
        ]
    )
    seen_messages = []

    def fake_complete_chat(messages):
        seen_messages.append([message.copy() for message in messages])
        return next(responses)

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)

    agent.run_task("Say hello")

    output = capsys.readouterr().out
    assert "Final answer:\nRecovered" in output
    assert "previous response was invalid" in seen_messages[-1][-1]["content"]


def test_max_step_limit_stops_runaway_loop(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "MAX_STEPS", 2)
    monkeypatch.setattr(
        agent,
        "complete_chat",
        lambda _messages: json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "pwd"},
                "reason": "loop",
            }
        ),
    )
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_tool", lambda _tool, _args: "tool output")

    agent.run_task("Loop")

    output = capsys.readouterr().out
    assert "Stopped: reached the max step limit" in output


def test_internal_trace_is_hidden_by_default(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)

    def fake_complete_chat(_messages):
        return json.dumps({"type": "final", "answer": "4"})

    monkeypatch.delenv("AGENT_DEBUG", raising=False)
    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)

    agent.run_task("what is 2+2")

    output = capsys.readouterr().out
    assert "Assistant raw response:" not in output
    assert "--- Step" not in output
    assert "Final answer:\n4" in output


def test_internal_trace_can_be_enabled(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)

    def fake_complete_chat(_messages):
        return json.dumps({"type": "final", "answer": "4"})

    monkeypatch.setenv("AGENT_DEBUG", "1")
    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)

    agent.run_task("what is 2+2")

    output = capsys.readouterr().out
    assert "--- Step 1 ---" in output
    assert "Assistant raw response:" in output
    assert '"answer": "4"' in output
    assert "Final answer:\n4" in output


def test_exit_command_recognizes_standard_quits():
    assert agent.is_exit_command("exit")
    assert agent.is_exit_command("quit")
    assert agent.is_exit_command(" q ")
    assert not agent.is_exit_command("quit deleting files")


def test_default_session_db_path_uses_data_directory(monkeypatch):
    monkeypatch.delenv("AGENT_SESSION_DB", raising=False)

    db_path = agent.Path(agent._session_db_path())

    assert db_path.name == "session_history.sqlite3"
    assert db_path.parent.name == "data"
    assert db_path.parent.exists()
