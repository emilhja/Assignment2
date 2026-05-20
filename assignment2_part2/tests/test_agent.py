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


def test_blocked_command_stops_without_retry(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    calls = 0

    def fake_complete_chat(_messages):
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "docker compose ps"},
                "reason": "inspect containers",
            }
        )

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
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


def test_invalid_bash_args_report_tool_error(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)

    monkeypatch.setattr(
        agent,
        "complete_chat",
        lambda _messages: json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": ""},
                "reason": "bad call",
            }
        ),
    )
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda _command: pytest.fail("invalid args should not ask for confirmation"),
    )

    agent.run_task("Inspect something")

    output = capsys.readouterr().out
    assert "Tool error: bash requires a non-empty string command." in output


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


def test_simple_read_answers_from_observation(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    calls = 0

    def fake_complete_chat(_messages):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("simple read should not need a second model call")
        return json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "cat demo.txt"},
                "reason": "read file",
            }
        )

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_tool", lambda _tool, _args: "status: done")

    answer = agent.run_task("what does the file contain")

    output = capsys.readouterr().out
    assert answer == "status: done"
    assert "Final answer:\nstatus: done" in output
    assert calls == 1


def test_open_file_answers_from_cat_observation(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    calls = 0

    def fake_complete_chat(_messages):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("open file should not need a second model call")
        return json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "cat /workspace/demo.txt"},
                "reason": "open file",
            }
        )

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_tool", lambda _tool, _args: "status: done")

    answer = agent.run_task("open demo.txt")

    output = capsys.readouterr().out
    assert answer == "status: done"
    assert "Final answer:\nstatus: done" in output
    assert calls == 1


def test_follow_up_about_bad_file_content_answers_from_cat_observation(
    monkeypatch, capsys, tmp_path
):
    _set_session_db(monkeypatch, tmp_path)
    responses = [
        json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "grep hello /workspace/demo.txt"},
                "reason": "check for hello",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "cat /workspace/demo.txt"},
                "reason": "verify file content",
            }
        ),
    ]

    def fake_complete_chat(_messages):
        return responses.pop(0)

    def fake_run_tool(_tool, args):
        if args["command"].startswith("grep "):
            return "Command exited with code 1.\n(no output)"
        return "status: done"

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "confirm_command", lambda _command: True)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)

    answer = agent.run_task("so where did hello world ! come from?")

    output = capsys.readouterr().out
    assert answer == "status: done"
    assert "Final answer:\nstatus: done" in output
    assert responses == []


def test_edit_result_answers_from_observation(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    calls = 0

    def fake_complete_chat(_messages):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("edit result should not be retried through the model")
        return json.dumps(
            {
                "type": "tool_call",
                "tool": "replace_text",
                "args": {
                    "path": "/workspace/demo.txt",
                    "old_text": "world",
                    "new_text": "Emil",
                    "all_occurrences": False,
                },
            }
        )

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(
        agent,
        "run_tool",
        lambda _tool, _args: "Edit blocked: old_text was not found in the file.",
    )

    answer = agent.run_task('can you change the text "world" in demo.txt to "Emil"')

    output = capsys.readouterr().out
    assert answer == "Edit blocked: old_text was not found in the file."
    assert "Final answer:\nEdit blocked: old_text was not found in the file." in output
    assert calls == 1


def test_edit_and_show_runs_second_read_tool(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    tool_calls = []
    confirmed_commands = []

    def fake_complete_chat(_messages):
        return json.dumps(
            {
                "type": "tool_call",
                "tool": "replace_text",
                "args": {
                    "path": "/workspace/demo.txt",
                    "old_text": "done",
                    "new_text": "draft",
                    "all_occurrences": False,
                },
            }
        )

    def fake_run_tool(tool, args):
        tool_calls.append((tool, args.copy()))
        if tool == "replace_text":
            return "Replaced 1 occurrence(s) in /workspace/demo.txt."
        if tool == "bash" and args["command"] == "cat /workspace/demo.txt":
            return "status: draft"
        raise AssertionError(f"unexpected tool call: {tool} {args}")

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda command: confirmed_commands.append(command) or True,
    )

    answer = agent.run_task(
        'change the text "done" in /workspace/demo.txt to "draft" and then show it'
    )

    output = capsys.readouterr().out
    assert answer == "status: draft"
    assert "Final answer:\nstatus: draft" in output
    assert tool_calls == [
        (
            "replace_text",
            {
                "path": "/workspace/demo.txt",
                "old_text": "done",
                "new_text": "draft",
                "all_occurrences": False,
            },
        ),
        ("bash", {"command": "cat /workspace/demo.txt"}),
    ]
    assert confirmed_commands == ["cat /workspace/demo.txt"]


def test_edit_and_show_bypasses_mistaken_bash_read(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    tool_calls = []
    confirmed_commands = []

    def fake_complete_chat(_messages):
        return json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "cat /workspace/demo.txt"},
                "reason": "mistaken read before edit",
            }
        )

    def fake_run_tool(tool, args):
        tool_calls.append((tool, args.copy()))
        if tool == "replace_text":
            return "Replaced 1 occurrence(s) in /workspace/demo.txt."
        if tool == "bash" and args["command"] == "cat /workspace/demo.txt":
            return "status: done"
        raise AssertionError(f"unexpected tool call: {tool} {args}")

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda command: confirmed_commands.append(command) or True,
    )

    answer = agent.run_task(
        'change the text "draft" in /workspace/demo.txt to "done" and then show it'
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


def test_all_file_contents_after_ls_runs_content_command(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    tool_calls = []
    confirmed_commands = []

    def fake_complete_chat(_messages):
        return json.dumps(
            {
                "type": "tool_call",
                "tool": "bash",
                "args": {"command": "ls -la /workspace"},
                "reason": "partial listing",
            }
        )

    def fake_run_tool(tool, args):
        tool_calls.append((tool, args.copy()))
        if args["command"] == "ls -la /workspace":
            return "demo.txt"
        if args["command"].startswith("find /workspace -maxdepth 1 -type f"):
            return "/workspace/demo.txt\nstatus: done"
        raise AssertionError(f"unexpected tool call: {tool} {args}")

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda command: confirmed_commands.append(command) or True,
    )

    answer = agent.run_task("list all files in workspace and then open each")

    output = capsys.readouterr().out
    assert answer == "/workspace/demo.txt\nstatus: done"
    assert "Final answer:\n/workspace/demo.txt\nstatus: done" in output
    assert tool_calls[0] == ("bash", {"command": "ls -la /workspace"})
    assert tool_calls[1][0] == "bash"
    assert tool_calls[1][1]["command"].startswith(
        "find /workspace -maxdepth 1 -type f"
    )
    assert confirmed_commands == [
        "ls -la /workspace",
        agent._workspace_file_contents_command(),
    ]


def test_blocked_edit_and_show_does_not_read_file(monkeypatch, capsys, tmp_path):
    _set_session_db(monkeypatch, tmp_path)
    tool_calls = []

    def fake_complete_chat(_messages):
        return json.dumps(
            {
                "type": "tool_call",
                "tool": "replace_text",
                "args": {
                    "path": "/workspace/demo.txt",
                    "old_text": "missing",
                    "new_text": "draft",
                    "all_occurrences": False,
                },
            }
        )

    def fake_run_tool(tool, args):
        tool_calls.append((tool, args.copy()))
        return "Edit blocked: old_text was not found in the file."

    monkeypatch.setattr(agent, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    monkeypatch.setattr(
        agent,
        "confirm_command",
        lambda _command: pytest.fail("blocked edit should not trigger cat"),
    )

    answer = agent.run_task("change missing to draft and then show it")

    output = capsys.readouterr().out
    assert answer == "Edit blocked: old_text was not found in the file."
    assert "Final answer:\nEdit blocked: old_text was not found in the file." in output
    assert tool_calls == [
        (
            "replace_text",
            {
                "path": "/workspace/demo.txt",
                "old_text": "missing",
                "new_text": "draft",
                "all_occurrences": False,
            },
        )
    ]


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


def test_exit_command_accepts_common_quit_typo():
    assert agent.is_exit_command("wuit")
    assert agent.is_exit_command(" q ")
    assert not agent.is_exit_command("quit deleting files")


def test_default_session_db_path_uses_data_directory(monkeypatch):
    monkeypatch.delenv("AGENT_SESSION_DB", raising=False)

    db_path = agent.Path(agent._session_db_path())

    assert db_path.name == "session_history.sqlite3"
    assert db_path.parent.name == "data"
    assert db_path.parent.exists()
