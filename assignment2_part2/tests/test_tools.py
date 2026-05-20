from types import SimpleNamespace

import safety
import tools


# These tests run run_bash directly, without the agent loop around it.
def test_run_bash_echo_hello():
    assert tools.run_bash("echo hello") == "hello"


def test_run_bash_uses_bash_expansion():
    assert tools.run_bash("echo {1..3}") == "1 2 3"


def test_run_bash_timeout(monkeypatch):
    monkeypatch.setattr(tools, "COMMAND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        safety, "ALLOWED_COMMANDS", safety.ALLOWED_COMMANDS | {"sleep"}
    )

    output = tools.run_bash("sleep 1")

    assert output.startswith("I stopped the command after 0.01 seconds.")


def test_run_bash_truncates_long_output():
    output = tools.run_bash("printf 'x%.0s' {1..4005}")

    assert output == ("x" * tools.MAX_OUTPUT_CHARS) + "\n... [output truncated]"


def test_run_bash_reports_missing_bash(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _command: None)

    assert tools.run_bash("echo hello") == tools.BASH_NOT_FOUND_MESSAGE


def test_run_bash_executes_from_workspace(tmp_path, monkeypatch):
    seen = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(tools.shutil, "which", lambda _command: "bash")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    assert tools.run_bash("pwd") == "ok"
    assert seen["kwargs"]["cwd"] == tmp_path.resolve()


def test_run_bash_blocks_dangerous_command_before_subprocess(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("unsafe command should not reach subprocess")

    monkeypatch.setattr(tools.subprocess, "run", fail_if_called)

    output = tools.run_bash("rm -rf /workspace")

    assert output.startswith("Blocked by safety check:")


def test_run_tool_bash_uses_safety_guard(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("unsafe command should not reach subprocess")

    monkeypatch.setattr(tools.subprocess, "run", fail_if_called)

    output = tools.run_tool("bash", {"command": "docker compose ps"})

    assert output.startswith("Blocked by safety check:")


def test_edit_section_replaces_unique_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    output = tools.edit_section("demo.txt", "two\n", "TWO\n")

    assert "Edited one section" in output
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\n"


def test_edit_section_accepts_workspace_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("hello\n", encoding="utf-8")

    output = tools.edit_section("/workspace/demo.txt", "hello\n", "hi\n")

    assert "Edited one section" in output
    assert target.read_text(encoding="utf-8") == "hi\n"


def test_edit_section_blocks_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "workspace"))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    output = tools.edit_section(str(outside), "secret", "changed")

    assert output.startswith("Edit blocked:")
    assert outside.read_text(encoding="utf-8") == "secret"


def test_edit_section_rejects_missing_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("hello\n", encoding="utf-8")

    output = tools.edit_section("demo.txt", "missing", "changed")

    assert "old_text was not found" in output
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_edit_section_rejects_repeated_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("same\nsame\n", encoding="utf-8")

    output = tools.edit_section("demo.txt", "same\n", "changed\n")

    assert "appears more than once" in output
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


def test_replace_text_replaces_all_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("status: draft\nreview: draft\n", encoding="utf-8")

    output = tools.replace_text("/workspace/demo.txt", "draft", "done", all_occurrences=True)

    assert "Replaced 2 occurrence(s)" in output
    assert target.read_text(encoding="utf-8") == "status: done\nreview: done\n"


def test_replace_text_requires_all_occurrences_for_repeated_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("draft\ndraft\n", encoding="utf-8")

    output = tools.replace_text("demo.txt", "draft", "done")

    assert "set all_occurrences to true" in output
    assert target.read_text(encoding="utf-8") == "draft\ndraft\n"
