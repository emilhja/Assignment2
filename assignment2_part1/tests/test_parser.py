from parser import parse_response


# These tests check the exact message format that agent.py accepts.
def test_parse_bash_action():
    text = """Thought: I need to list files
Action: bash
Command: ls -la /workspace"""
    result = parse_response(text)

    assert result.kind == "action"
    assert result.action == "bash"
    assert result.command == "ls -la /workspace"


def test_parse_final_answer():
    text = """Thought: I know the answer
Final Answer: Done"""
    result = parse_response(text)

    assert result.kind == "final"
    assert result.answer == "Done"


def test_rejects_missing_thought():
    # Without Thought:, the parser rejects the message before checking commands.
    result = parse_response("""Action: bash
Command: pwd""")

    assert result.kind == "invalid"


def test_rejects_command_without_action():
    result = parse_response("""Thought: I need the current directory
Command: pwd""")

    assert result.kind == "invalid"


def test_rejects_command_on_action_line():
    result = parse_response("Thought: I need the current directory\nAction: bash pwd")

    assert result.kind == "invalid"


def test_rejects_bare_command_after_action():
    result = parse_response("""Thought: I need the current directory
Action: bash
pwd""")

    assert result.kind == "invalid"


def test_rejects_fully_quoted_command_string():
    # The command should be raw shell text, not wrapped in one big quote pair.
    result = parse_response("""Thought: I need to search files
Action: bash
Command: 'find . -type f -print0 | xargs -0 grep -r --color -n'""")

    assert result.kind == "invalid"
