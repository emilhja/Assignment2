"""Unit tests for code_share — extraction, project allocation, save, pytest hook."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Part 2 must be importable (group_chat imports trigger this side-effect
# too, but tests under tests/ import code_share directly).
import part2_bridge  # noqa: F401

from code_share import (
    MAX_PROJECTS,
    CodeBlock,
    extract_code_blocks,
    maybe_run_pytest,
    most_recent_project_dir,
    named_project_dir,
    next_project_dir,
    process_shared_code,
    save_code_blocks,
)


# ---------------------------------------------------------------- extract


def test_extract_single_python_block_with_directive():
    text = (
        "here you go:\n"
        "```python\n"
        "# file: calc.py\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "```\n"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].lang == "python"
    assert blocks[0].filename == "calc.py"
    assert blocks[0].canonical is True
    # Directive line is stripped from saved content.
    assert "file:" not in blocks[0].content
    assert "def add(a, b):" in blocks[0].content


def test_extract_filename_from_english_prose_before_fence():
    text = (
        "Here is the content of `test_calculator.py`:\n"
        "```python\n"
        "from calculator import add\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        "```\n"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].filename == "test_calculator.py"


def test_extract_filename_from_swedish_prose_before_fence():
    text = (
        "Här är innehållet i `calculator.py`:\n"
        "```python\n"
        "def add(x, y):\n"
        "    return x + y\n"
        "```\n"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].filename == "calculator.py"


def test_extract_directive_overrides_prose_filename():
    text = (
        "Here is `wrong_name.py`:\n"
        "```python\n"
        "# file: right_name.py\n"
        "print('ok')\n"
        "```\n"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].filename == "right_name.py"
    assert "file:" not in blocks[0].content


def test_extract_multiple_blocks_filename_fallback():
    text = (
        "```python\nprint(1)\n```\n"
        "and another:\n"
        "```python\nprint(2)\n```\n"
    )
    blocks = extract_code_blocks(text)
    assert [b.filename for b in blocks] == ["snippet1.py", "snippet2.py"]
    assert [b.canonical for b in blocks] == [False, False]


def test_extract_language_extension_mapping():
    text = (
        "```js\nconsole.log(1)\n```\n"
        "```ts\nconst x: number = 1\n```\n"
        "```bash\nls\n```\n"
        "```rustlang\nfn main() {}\n```\n"
    )
    blocks = extract_code_blocks(text)
    assert [b.filename for b in blocks] == [
        "snippet1.js",
        "snippet2.ts",
        "snippet3.sh",
        "snippet4.txt",  # unknown → .txt
    ]


def test_extract_no_fences_returns_empty():
    assert extract_code_blocks("just some prose, no code at all") == []
    assert extract_code_blocks("") == []


def test_extract_directive_traversal_is_sanitized_to_basename():
    text = (
        "```python\n"
        "# file: ../../etc/passwd\n"
        "print('pwn')\n"
        "```\n"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    # rsplit on "/" leaves "passwd"; leading-dot stripping doesn't affect it.
    assert blocks[0].filename == "passwd"


def test_extract_javascript_slash_directive():
    text = (
        "```js\n"
        "// file: app.js\n"
        "console.log('hi')\n"
        "```\n"
    )
    blocks = extract_code_blocks(text)
    assert blocks[0].filename == "app.js"
    assert "// file:" not in blocks[0].content


# --------------------------------------------------------- project alloc


def test_next_project_dir_allocates_sequentially(tmp_path):
    ws = tmp_path / "agent_ws"
    ws.mkdir()
    first = next_project_dir(ws)
    assert first is not None and first.name == "project1"
    second = next_project_dir(ws)
    assert second is not None and second.name == "project2"


def test_next_project_dir_uses_max_plus_one_not_gap_fill(tmp_path):
    ws = tmp_path / "agent_ws"
    ws.mkdir()
    (ws / "project1").mkdir()
    (ws / "project3").mkdir()
    (ws / "not-a-project").mkdir()
    nxt = next_project_dir(ws)
    assert nxt is not None and nxt.name == "project4"


def test_next_project_dir_returns_none_at_cap(tmp_path):
    ws = tmp_path / "agent_ws"
    ws.mkdir()
    (ws / f"project{MAX_PROJECTS}").mkdir()
    assert next_project_dir(ws) is None


def test_most_recent_project_dir_picks_highest_existing(tmp_path):
    ws = tmp_path / "agent_ws"
    ws.mkdir()
    (ws / "project1").mkdir()
    (ws / "project3").mkdir()
    (ws / "project2").mkdir()
    (ws / "not-a-project").mkdir()
    recent = most_recent_project_dir(ws)
    assert recent is not None and recent.name == "project3"


def test_most_recent_project_dir_returns_none_when_empty(tmp_path):
    ws = tmp_path / "agent_ws"
    ws.mkdir()
    assert most_recent_project_dir(ws) is None


# --------------------------------------------------------------- save


def test_save_writes_files_and_dedupes_names(tmp_path):
    project = tmp_path / "project1"
    project.mkdir()
    blocks = [
        CodeBlock(lang="python", content="print(1)\n", filename="x.py"),
        CodeBlock(lang="python", content="print(2)\n", filename="x.py"),
    ]
    paths = save_code_blocks(blocks, project)
    assert [p.name for p in paths] == ["x.py", "x_2.py"]
    assert (project / "x.py").read_text() == "print(1)\n"
    assert (project / "x_2.py").read_text() == "print(2)\n"


def test_save_dedupes_snippet_names_with_import_safe_suffix(tmp_path):
    project = tmp_path / "project1"
    project.mkdir()
    blocks = [
        CodeBlock(lang="python", content="print(1)\n", filename="snippet1.py"),
        CodeBlock(lang="python", content="print(2)\n", filename="snippet1.py"),
    ]
    paths = save_code_blocks(blocks, project)
    assert [p.name for p in paths] == ["snippet1.py", "snippet1_2.py"]


# ----------------------------------------------------------- pytest hook


def test_maybe_run_pytest_skips_without_test_files(tmp_path, monkeypatch):
    project = tmp_path / "project1"
    project.mkdir()
    (project / "main.py").write_text("print('hi')\n")
    called = {"n": 0}

    def _fake_run(*args, **kwargs):  # pragma: no cover — should not be hit
        called["n"] += 1
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    ran, output = maybe_run_pytest(project)
    assert ran is False
    assert output == ""
    assert called["n"] == 0


def test_maybe_run_pytest_runs_when_test_file_present(tmp_path, monkeypatch):
    project = tmp_path / "project1"
    project.mkdir()
    (project / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "1 passed in 0.01s"
        stderr = ""

    def _fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    ran, output = maybe_run_pytest(project)
    assert ran is True
    assert "1 passed" in output
    assert captured["args"][1:] == ["-m", "pytest", str(project), "-q"]


# ---------------------------------------------------- orchestrator


def test_process_shared_code_returns_none_for_no_fences(tmp_path):
    project = tmp_path / "project1"
    project.mkdir()
    assert process_shared_code("hello there, no code", "alice", project) is None


def test_process_shared_code_end_to_end_no_tests(tmp_path, monkeypatch):
    project = tmp_path / "project1"
    project.mkdir()

    def _no_subprocess(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("pytest should not run when no test_*.py exists")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    text = "```python\n# file: calc.py\ndef add(a, b):\n    return a + b\n```\n"
    guidance = process_shared_code(text, "alice", project)
    assert guidance is not None
    assert "/workspace/alice/project1/calc.py" in guidance
    assert "Auto-pytest: not run" in guidance
    assert (project / "calc.py").exists()


def test_process_shared_code_marks_unnamed_blocks_as_noncanonical(tmp_path, monkeypatch):
    project = tmp_path / "project1"
    project.mkdir()

    def _no_subprocess(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("pytest should not run when no test_*.py exists")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    text = "```python\ndef add(a, b):\n    return a + b\n```\n"
    guidance = process_shared_code(text, "alice", project)
    assert guidance is not None
    assert "/workspace/alice/project1/snippet1.py (snippet; no # file directive)" in guidance
    assert "untrusted non-canonical context" in guidance


def test_process_shared_code_end_to_end_with_tests(tmp_path, monkeypatch):
    project = tmp_path / "project5"
    project.mkdir()

    class _FakeCompleted:
        returncode = 0
        stdout = "2 passed in 0.02s"
        stderr = ""

    def _fake_run(args, **kwargs):
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    text = (
        "```python\n# file: calc.py\ndef add(a, b):\n    return a + b\n```\n"
        "```python\n# file: test_calc.py\n"
        "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n```\n"
    )
    guidance = process_shared_code(text, "emil_bot", project)
    assert guidance is not None
    assert "/workspace/emil_bot/project5/calc.py" in guidance
    assert "/workspace/emil_bot/project5/test_calc.py" in guidance
    assert "Auto-pytest: ran" in guidance
    assert "2 passed" in guidance


# -------------------------------------------------- named project allocator


def test_named_project_dir_creates_and_is_idempotent(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    first = named_project_dir(root, "calc")
    assert first is not None and first.name == "calc"
    assert first.is_dir()
    # Second call returns the same dir without raising.
    second = named_project_dir(root, "calc")
    assert second is not None and second == first


def test_named_project_dir_lowercases_and_trims(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    result = named_project_dir(root, "  Calc  ")
    assert result is not None and result.name == "calc"


def test_named_project_dir_rejects_unsafe_names(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    for bad in ("..", "../etc", "name/sub", "weird.name", "with space", "", "/abs"):
        assert named_project_dir(root, bad) is None, f"should reject {bad!r}"


def test_next_project_dir_retries_on_race(tmp_path, monkeypatch):
    """Two agents racing on `next_project_dir` against the same shared root.

    Simulate by pre-creating project1 and forcing the first mkdir attempt to
    raise FileExistsError; the allocator should retry and land on project2.
    """
    ws = tmp_path / "shared"
    ws.mkdir()
    (ws / "project1").mkdir()

    original_mkdir = Path.mkdir
    state = {"raised": False}

    def _flaky_mkdir(self, *args, **kwargs):
        if (
            not state["raised"]
            and self.name == "project2"
            and kwargs.get("exist_ok") is False
        ):
            state["raised"] = True
            raise FileExistsError(self)
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _flaky_mkdir)
    result = next_project_dir(ws)
    assert state["raised"] is True
    assert result is not None
    # Either project2 (after the retry re-reads indices and finds project1
    # only) or project3 (if the simulated mkdir created project2 anyway) is
    # acceptable — the contract is "no FileExistsError leak, monotonic".
    assert result.name in {"project2", "project3"}


# ---------------------------------- process_shared_code auto_pytest + shared


def test_process_shared_code_auto_pytest_false_skips_subprocess(tmp_path, monkeypatch):
    project = tmp_path / "project1"
    project.mkdir()

    def _no_subprocess(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("pytest must not run when auto_pytest=False")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    text = (
        "```python\n# file: t.py\ndef test_x():\n    assert True\n```\n"
    )
    guidance = process_shared_code(text, "alice", project, auto_pytest=False)
    assert guidance is not None
    assert "Auto-pytest: skipped" in guidance
    assert (project / "t.py").exists()


def test_process_shared_code_reports_shared_path_when_shared_root_set(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    project = root / "calc"
    project.mkdir()
    text = "```python\n# file: calc.py\ndef add(a, b):\n    return a + b\n```\n"
    guidance = process_shared_code(
        text, "alice", project, auto_pytest=False, shared_root=root
    )
    assert guidance is not None
    assert "/workspace/shared/calc/calc.py" in guidance
    assert "/workspace/alice/" not in guidance
