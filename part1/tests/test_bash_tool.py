from part1 import bash_tool


def test_run_bash_echo():
    assert bash_tool.run_bash("echo hello") == "hello"


def test_run_bash_uses_bash_expansion():
    assert bash_tool.run_bash("echo {1..3}") == "1 2 3"


def test_run_bash_timeout(monkeypatch):
    monkeypatch.setattr(bash_tool, "COMMAND_TIMEOUT_SECONDS", 0.01)

    output = bash_tool.run_bash("sleep 1")

    assert output == "Command timed out after 0.01 seconds."


def test_run_bash_truncates_long_output():
    output = bash_tool.run_bash("printf 'x%.0s' {1..4005}")

    assert output == ("x" * bash_tool.MAX_OUTPUT_CHARS) + "\n... [output truncated]"


def test_run_bash_reports_missing_bash(monkeypatch):
    monkeypatch.setattr(bash_tool.shutil, "which", lambda _command: None)

    assert bash_tool.run_bash("echo hello") == bash_tool.BASH_NOT_FOUND
