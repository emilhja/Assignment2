from pathlib import Path
from types import SimpleNamespace

import runtime_helpers
import safety
import tools


def test_system_prompt_lists_every_registered_tool():
    """Guard against tool/prompt drift: every tool in TOOL_REGISTRY must be
    advertised in the system prompt so the model knows it can call it. (Part 2's
    prompt previously omitted read_file/append_text/run_tests.)"""

    prompt_path = Path(tools.__file__).resolve().parent / "config" / "system_prompt.txt"
    prompt = prompt_path.read_text(encoding="utf-8")
    missing = [name for name in tools.TOOL_REGISTRY if f"- {name}:" not in prompt]
    assert missing == [], f"system prompt does not list registered tools: {missing}"


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
    assert "OPENROUTER_API_KEY" not in env
    assert "PATH" in env


def test_run_bash_subprocess_env_strips_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "leak-me-if-you-can")
    monkeypatch.setenv("OPENROUTER_API_KEY", "another-secret")

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


def test_workspace_mutation_metadata_lists_only_write_tools():
    assert runtime_helpers.workspace_mutation_tools() == {
        "append_text",
        "create_file",
        "edit_section",
        "rename_file",
        "replace_text",
    }
    assert runtime_helpers.tool_succeeded(
        "append_text", "Appended text to /workspace/demo.txt."
    )
    assert not runtime_helpers.tool_succeeded(
        "read_file", "--- /workspace/demo.txt ---\ncontent"
    )


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


def test_rename_file_renames_workspace_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    source = tmp_path / "snippet1-2.py"
    target = tmp_path / "snippet1_2.py"
    source.write_text("x = 1\n", encoding="utf-8")

    output = tools.rename_file("/workspace/snippet1-2.py", "/workspace/snippet1_2.py")

    assert output == "Renamed file from /workspace/snippet1-2.py to /workspace/snippet1_2.py."
    assert not source.exists()
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_rename_file_blocks_missing_source(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))

    output = tools.rename_file("/workspace/missing.py", "/workspace/new.py")

    assert output.startswith("Edit blocked:")
    assert "source file does not exist" in output
    assert not (tmp_path / "new.py").exists()


def test_rename_file_refuses_existing_target_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    source = tmp_path / "old.py"
    target = tmp_path / "new.py"
    source.write_text("old\n", encoding="utf-8")
    target.write_text("target\n", encoding="utf-8")

    output = tools.rename_file("old.py", "new.py")

    assert "target file already exists" in output
    assert source.read_text(encoding="utf-8") == "old\n"
    assert target.read_text(encoding="utf-8") == "target\n"


def test_rename_file_overwrites_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    source = tmp_path / "old.py"
    target = tmp_path / "new.py"
    source.write_text("old\n", encoding="utf-8")
    target.write_text("target\n", encoding="utf-8")

    output = tools.rename_file("old.py", "new.py", overwrite=True)

    assert output == "Renamed file from /workspace/old.py to /workspace/new.py."
    assert not source.exists()
    assert target.read_text(encoding="utf-8") == "old\n"


def test_rename_file_refuses_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    (tmp_path / "source_dir").mkdir()

    output = tools.rename_file("source_dir", "target")

    assert output.startswith("Edit blocked:")
    assert "source path is not a file" in output
    assert (tmp_path / "source_dir").is_dir()


def test_rename_file_blocks_outside_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))

    output = tools.rename_file(str(outside), "/workspace/outside.py")

    assert output.startswith("Edit blocked:")
    assert outside.exists()
    assert not (workspace / "outside.py").exists()


def test_rename_file_blocks_cross_workspace_move(tmp_path, monkeypatch):
    private = tmp_path / "alice"
    shared = tmp_path / "shared"
    private.mkdir()
    shared.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    source = private / "calc.py"
    source.write_text("x = 1\n", encoding="utf-8")

    output = tools.rename_file("/workspace/calc.py", "/workspace/shared/calc.py")

    assert output.startswith("Edit blocked:")
    assert "same workspace root" in output
    assert source.exists()
    assert not (shared / "calc.py").exists()


def test_rename_file_via_tool_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    (tmp_path / "old.py").write_text("x = 1\n", encoding="utf-8")

    output = tools.run_tool(
        "rename_file",
        {"source_path": "/workspace/old.py", "target_path": "/workspace/new.py"},
    )

    assert "Renamed file" in output
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_append_text_appends_to_existing_workspace_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("one\n", encoding="utf-8")

    output = tools.append_text("/workspace/demo.txt", "two\n")

    assert output == "Appended text to /workspace/demo.txt."
    assert target.read_text(encoding="utf-8") == "one\ntwo\n"


def test_append_text_routes_workspace_shared_alias_to_shared_root(tmp_path, monkeypatch):
    private = tmp_path / "private"
    shared = tmp_path / "shared"
    private.mkdir()
    shared.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    target = shared / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")

    output = tools.append_text("/workspace/shared/calc.py", "y = 2\n")

    assert output == "Appended text to /workspace/shared/calc.py."
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


def test_append_text_blocks_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))

    output = tools.append_text("/workspace/missing.txt", "text\n")

    assert "file does not exist" in output


def test_append_text_blocks_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "workspace"))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    output = tools.append_text(str(outside), "changed\n")

    assert "path must stay inside" in output


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


def test_create_file_writes_into_shared_workspace(tmp_path, monkeypatch):
    private = tmp_path / "alice"
    shared = tmp_path / "shared"
    private.mkdir()
    shared.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))

    output = tools.create_file(str(shared / "calc.py"), "x = 1\n")

    assert output == "Created file in /workspace/shared/calc.py."
    assert (shared / "calc.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (private / "calc.py").exists()


def test_create_file_routes_workspace_shared_alias_to_shared_root(tmp_path, monkeypatch):
    private = tmp_path / "alice"
    shared = tmp_path / "shared"
    private.mkdir()
    shared.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))

    output = tools.create_file("/workspace/shared/calc.py", "x = 1\n")

    assert output == "Created file in /workspace/shared/calc.py."
    assert (shared / "calc.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (private / "shared" / "calc.py").exists()


def test_resolve_workspace_normalizes_bare_workspace_prefix(tmp_path, monkeypatch):
    private = tmp_path / "alice"
    private.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))

    # The bug from a live session: agent typed "workspace/alice/<file>" with
    # no leading slash, and the resolver nested it under the private root.
    bare = str(private).replace("\\", "/").lstrip("/")
    output = tools.create_file(f"{bare}/calc.py", "ok\n")

    assert "Created file" in output
    assert (private / "calc.py").read_text(encoding="utf-8") == "ok\n"
    assert not (private / "alice").exists()


def test_read_file_returns_contents_with_header(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "notes.txt"
    target.write_text("line1\nline2\n", encoding="utf-8")

    output = tools.read_file("/workspace/notes.txt")

    assert output.startswith("--- /workspace/notes.txt ---\n")
    assert "line1\nline2\n" in output


def test_read_file_reads_shared_workspace(tmp_path, monkeypatch):
    private = tmp_path / "alice"
    shared = tmp_path / "shared"
    private.mkdir()
    shared.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))
    (shared / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    output = tools.read_file("/workspace/shared/calc.py")

    assert "def add" in output
    assert "/workspace/shared/calc.py" in output


def test_read_file_blocks_missing_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))

    output = tools.read_file("/workspace/missing.txt")

    assert output.startswith("Edit blocked:")
    assert "does not exist" in output


def test_read_file_blocks_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    (tmp_path / "sub").mkdir()

    output = tools.read_file("/workspace/sub")

    assert output.startswith("Edit blocked:")
    assert "not a file" in output


def test_read_file_blocks_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "workspace"))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    output = tools.read_file(str(outside))

    assert output.startswith("Edit blocked:")
    assert "secret" not in output


def test_read_file_truncates_oversized_content(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "big.txt"
    target.write_text("x" * (tools.MAX_OUTPUT_CHARS + 200), encoding="utf-8")

    output = tools.read_file("/workspace/big.txt")

    assert output.endswith("[output truncated]")
    assert len(output) <= tools.MAX_OUTPUT_CHARS + len("\n... [output truncated]")


def test_read_file_via_tool_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "demo.txt"
    target.write_text("hello\n", encoding="utf-8")

    output = tools.run_tool("read_file", {"path": "/workspace/demo.txt"})

    assert "hello" in output


def test_run_tests_executes_passing_pytest_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    (tmp_path / "test_demo.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    output = tools.run_tests("/workspace/test_demo.py")

    assert "1 passed" in output
    assert not output.startswith("pytest exited")


def test_run_tests_reports_failure_with_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    (tmp_path / "test_fail.py").write_text(
        "def test_bad():\n    assert 0\n",
        encoding="utf-8",
    )

    output = tools.run_tests("/workspace/test_fail.py")

    assert output.startswith("pytest exited with code")
    assert "1 failed" in output


def test_run_tests_blocks_path_outside_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def test_x(): assert True\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))

    output = tools.run_tests(str(outside))

    assert output.startswith("Edit blocked:")


def test_run_tests_via_tool_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    (tmp_path / "test_reg.py").write_text(
        "def test_two():\n    assert 2 == 2\n",
        encoding="utf-8",
    )

    output = tools.run_tool("run_tests", {"path": "/workspace/test_reg.py"})

    assert "1 passed" in output


def test_create_file_blocks_path_outside_both_roots(tmp_path, monkeypatch):
    private = tmp_path / "alice"
    shared = tmp_path / "shared"
    bob = tmp_path / "bob"
    private.mkdir()
    shared.mkdir()
    bob.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(private))
    monkeypatch.setenv("SHARED_WORKSPACE", str(shared))

    output = tools.create_file(str(bob / "stolen.py"), "secret")

    assert output.startswith("Edit blocked:")
    assert not (bob / "stolen.py").exists()
