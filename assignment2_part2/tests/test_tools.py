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
    # bash must be invoked without profile/rc sourcing
    assert seen["args"][0] == ["bash", "--noprofile", "--norc", "-c", "pwd"]
    # subprocess env must not carry provider API keys or secrets
    env = seen["kwargs"]["env"]
    assert "GROQ_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "PATH" in env


def test_run_bash_subprocess_env_strips_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "leak-me-if-you-can")
    monkeypatch.setenv("OPENAI_API_KEY", "another-secret")

    output = tools.run_bash("echo done")

    assert "leak-me-if-you-can" not in output
    assert "another-secret" not in output


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


def test_edit_section_rejects_partial_line_match_with_indentation(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.py"
    original = (
        "def factorial(n):\n"
        "    result = 1\n"
        "    for value in range(2, n + 1):\n"
        "        result *= value\n"
        "    return result\n"
    )
    target.write_text(original, encoding="utf-8")

    output = tools.edit_section("demo.py", "return result", "        return result")

    namespace = {}
    exec(target.read_text(encoding="utf-8"), namespace)
    assert "complete line section" in output
    assert target.read_text(encoding="utf-8") == original
    assert namespace["factorial"](5) == 120


def test_edit_section_accepts_indented_whole_line_match(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.py"
    target.write_text(
        "def factorial(n):\n"
        "    result = 1\n"
        "    for value in range(2, n + 1):\n"
        "        result *= value\n"
        "    return result\n",
        encoding="utf-8",
    )

    output = tools.edit_section("demo.py", "    return result\n", "    return int(result)\n")

    namespace = {}
    exec(target.read_text(encoding="utf-8"), namespace)
    assert "Edited one section" in output
    assert namespace["factorial"](5) == 120


def test_edit_section_rejects_multiline_match_not_starting_on_line(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    original = "alpha\n    beta\n    gamma\nomega\n"
    target.write_text(original, encoding="utf-8")

    output = tools.edit_section("demo.txt", "beta\n    gamma", "BETA\n    GAMMA")

    assert "complete line section" in output
    assert target.read_text(encoding="utf-8") == original


def test_create_file_writes_exact_content_to_workspace_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "hello.txt"

    output = tools.create_file("/workspace/hello.txt", "Hello world!")

    assert output == "Created file in /workspace/hello.txt."
    assert target.read_text(encoding="utf-8") == "Hello world!"


def test_create_file_accepts_relative_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "hello.txt"

    output = tools.create_file("hello.txt", "Hello world!")

    assert output == "Created file in /workspace/hello.txt."
    assert target.read_text(encoding="utf-8") == "Hello world!"


def test_create_file_blocks_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "workspace"))
    outside = tmp_path / "outside.txt"

    output = tools.create_file(str(outside), "secret")

    assert output.startswith("Edit blocked:")
    assert not outside.exists()


def test_create_file_blocks_parent_directory_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "workspace"))
    outside = tmp_path / "outside.txt"

    output = tools.create_file("../outside.txt", "secret")

    assert output.startswith("Edit blocked:")
    assert not outside.exists()


def test_create_file_refuses_overwrite_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "hello.txt"
    target.write_text("old", encoding="utf-8")

    output = tools.create_file("hello.txt", "new")

    assert "file already exists" in output
    assert target.read_text(encoding="utf-8") == "old"


def test_create_file_overwrites_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "hello.txt"
    target.write_text("old", encoding="utf-8")

    output = tools.create_file("hello.txt", "new", overwrite=True)

    assert output == "Overwrote file in /workspace/hello.txt."
    assert target.read_text(encoding="utf-8") == "new"


def test_create_file_creates_nested_parents_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "scripts" / "hello.sh"

    output = tools.create_file("scripts/hello.sh", '#!/bin/bash\necho "Hello, World!"\n')

    assert output == "Created file in /workspace/scripts/hello.sh."
    assert target.read_text(encoding="utf-8") == '#!/bin/bash\necho "Hello, World!"\n'


def test_create_file_refuses_parent_path_that_is_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    parent = tmp_path / "scripts"
    parent.write_text("not a directory", encoding="utf-8")

    output = tools.create_file("scripts/hello.sh", "echo hello\n")

    assert "parent path is not a directory" in output
    assert parent.read_text(encoding="utf-8") == "not a directory"


def test_replace_text_replaces_all_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("draft\nready\ndraft\n", encoding="utf-8")

    output = tools.replace_text("/workspace/demo.txt", "draft\n", "done\n", all_occurrences=True)

    assert "Replaced 2 occurrence(s)" in output
    assert target.read_text(encoding="utf-8") == "done\nready\ndone\n"


def test_replace_text_rejects_partial_line_match(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    original = "status: draft\n"
    target.write_text(original, encoding="utf-8")

    output = tools.replace_text("/workspace/demo.txt", "draft", "done")

    assert "complete line section" in output
    assert target.read_text(encoding="utf-8") == original


def test_replace_text_requires_all_occurrences_for_repeated_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("draft\ndraft\n", encoding="utf-8")

    output = tools.replace_text("demo.txt", "draft", "done")

    assert "set all_occurrences to true" in output
    assert target.read_text(encoding="utf-8") == "draft\ndraft\n"
