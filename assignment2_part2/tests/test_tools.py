import tools


# These tests run run_bash directly, without the agent loop around it.
def test_run_bash_echo_hello():
    assert tools.run_bash("echo hello") == "hello"


def test_run_bash_uses_bash_expansion():
    assert tools.run_bash("echo {1..3}") == "1 2 3"


def test_run_bash_timeout(monkeypatch):
    monkeypatch.setattr(tools, "COMMAND_TIMEOUT_SECONDS", 0.01)

    output = tools.run_bash("sleep 1")

    assert output == "I stopped the command after 0.01 seconds."


def test_run_bash_truncates_long_output():
    output = tools.run_bash("printf 'x%.0s' {1..4005}")

    assert output == ("x" * tools.MAX_OUTPUT_CHARS) + "\n... [output truncated]"


def test_run_bash_reports_missing_bash(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _command: None)

    assert tools.run_bash("echo hello") == tools.BASH_NOT_FOUND_MESSAGE
