from part1.protocol import parse_model_response


def test_parse_bash_action():
    result = parse_model_response(
        "Thought: I need to list files\n"
        "Action: bash\n"
        "Command: ls -la /workspace"
    )

    assert result.kind == "action"
    assert result.action == "bash"
    assert result.command == "ls -la /workspace"


def test_parse_final_answer():
    result = parse_model_response("Thought: I know it\nFinal Answer: Done")

    assert result.kind == "final"
    assert result.answer == "Done"


def test_parse_multiline_final_answer():
    result = parse_model_response("Thought: I know it\nFinal Answer: First\nSecond")

    assert result.kind == "final"
    assert result.answer == "First\nSecond"


def test_rejects_missing_thought():
    result = parse_model_response("Action: bash\nCommand: pwd")

    assert result.kind == "invalid"


def test_rejects_command_without_action():
    result = parse_model_response("Thought: Need pwd\nCommand: pwd")

    assert result.kind == "invalid"


def test_rejects_json_style_response():
    result = parse_model_response('{"action": "bash", "command": "pwd"}')

    assert result.kind == "invalid"
    assert "JSON" in result.error


def test_rejects_markdown_fence():
    result = parse_model_response("Thought: Need pwd\n```bash\npwd\n```")

    assert result.kind == "invalid"


def test_rejects_fully_quoted_command():
    result = parse_model_response(
        "Thought: Search\n"
        "Action: bash\n"
        "Command: 'find . -type f'"
    )

    assert result.kind == "invalid"
