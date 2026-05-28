"""Auto-save peer-shared code blocks to private project dirs (P3 remote-only).

When a peer message arrives on the RunPod hub containing markdown code
fences, the runtime extracts each block, writes it to a fresh
``workspace/<AGENT_ID>/projectN/`` directory (N=1..100, allocated
max-plus-one), and — if any ``test_*.py`` lands in the project — runs
pytest against the project dir and folds the result into a runtime
guidance string for the next LLM turn.

Filesystem-only: no LLM calls, no transport calls, no global state.
``process_shared_code`` is the single orchestrator called from
``group_chat._run_task_for_message``.

Note on trust: peer-shared code is untrusted. Saving it is filesystem
work in an isolated dir; auto-pytest is a real subprocess that executes
that untrusted code with this process's privileges. The feature is
remote-gated by ``group_chat`` (mode == "runpod"); local stub/demo runs
do not exercise it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


MAX_PROJECTS = 100
PYTEST_TIMEOUT_SECONDS = 30.0
PYTEST_OUTPUT_CHAR_CAP = 4000
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_PROJECT_DIR_RACE_RETRIES = 5

# Language hint (after the opening ```) → file extension.
_LANG_EXTENSIONS = {
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "bash": ".sh",
    "sh": ".sh",
    "shell": ".sh",
    "markdown": ".md",
    "md": ".md",
    "json": ".json",
    "yaml": ".yaml",
    "yml": ".yaml",
    "html": ".html",
    "css": ".css",
}

_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_+\-]*)[ \t]*\n(.*?)\n?```",
    re.DOTALL,
)

# `# file: name.py` or `// file: name.js` on the first non-blank line.
_FILE_DIRECTIVE_RE = re.compile(
    r"^[ \t]*(?:#|//)[ \t]*file[ \t]*:[ \t]*(\S+)[ \t]*$",
    re.MULTILINE,
)

_KNOWN_FILENAME_EXTENSIONS = sorted(
    {ext.lstrip(".") for ext in _LANG_EXTENSIONS.values()},
    key=len,
    reverse=True,
)
_PROSE_FILENAME_RE = re.compile(
    r"[`'\"]"
    r"(?P<name>[A-Za-z0-9_. /\\-]+?\.(?:"
    + "|".join(re.escape(ext) for ext in _KNOWN_FILENAME_EXTENSIONS)
    + r"))"
    r"[`'\"]"
)


@dataclass
class CodeBlock:
    lang: str
    content: str
    filename: str
    canonical: bool = False


def _sanitize_basename(name: str) -> str:
    """Strip path components and unsafe chars from a directive-provided name.

    Only the basename survives; ``..``, leading ``/``, drive letters,
    and NUL bytes are removed by the basename + replacement steps.
    """
    cleaned = name.replace("\x00", "").strip().strip('"').strip("'")
    cleaned = cleaned.replace("\\", "/")
    base = cleaned.rsplit("/", 1)[-1]
    base = base.lstrip(".")  # drop leading dots so ".." → "" and ".env" → "env"
    if not base:
        return ""
    return base


def _extension_for(lang: str) -> str:
    return _LANG_EXTENSIONS.get(lang.lower(), ".txt")


def _filename_from_preceding_prose(prose: str) -> str:
    """Return the last quoted/backticked filename near a code fence."""
    if not prose:
        return ""
    # Keep this local to the current block. Chatty peer messages often contain
    # several filenames; the nearest one before the fence is the safest signal.
    window = prose[-240:]
    matches = list(_PROSE_FILENAME_RE.finditer(window))
    if not matches:
        return ""
    return _sanitize_basename(matches[-1].group("name"))


def extract_code_blocks(text: str) -> List[CodeBlock]:
    """Parse markdown code fences out of `text` and assign each a filename.

    Filename heuristic:
      1. ``# file: <name>`` (or ``// file: <name>``) on a line inside the
         block → use sanitized basename of <name>. The directive line is
         removed from the saved content.
      2. A quoted/backticked filename immediately before the fence.
      3. Fall back to ``snippet<N>.<ext>`` where N is a global counter
         across all unnamed blocks in this message, and <ext> is derived
         from the fence language hint (unknown → .txt).
    """
    if not text:
        return []
    blocks: List[CodeBlock] = []
    fallback_n = 0
    previous_end = 0
    for match in _FENCE_RE.finditer(text):
        lang = (match.group(1) or "").strip()
        content = match.group(2)
        filename = ""
        directive = _FILE_DIRECTIVE_RE.search(content)
        if directive:
            filename = _sanitize_basename(directive.group(1))
            # Remove the directive line so it doesn't pollute saved code.
            content = (
                content[: directive.start()] + content[directive.end():]
            ).lstrip("\n")
            canonical = True
        else:
            canonical = False
        if not filename:
            filename = _filename_from_preceding_prose(text[previous_end:match.start()])
        if not filename:
            fallback_n += 1
            filename = f"snippet{fallback_n}{_extension_for(lang)}"
        blocks.append(
            CodeBlock(
                lang=lang,
                content=content,
                filename=filename,
                canonical=canonical,
            )
        )
        previous_end = match.end()
    return blocks


def _existing_project_indices(agent_workspace: Path) -> List[int]:
    if not agent_workspace.exists():
        return []
    indices: List[int] = []
    for entry in agent_workspace.iterdir():
        if not entry.is_dir():
            continue
        m = re.fullmatch(r"project(\d+)", entry.name)
        if m:
            indices.append(int(m.group(1)))
    return indices


def next_project_dir(
    agent_workspace: Path, max_projects: int = MAX_PROJECTS
) -> Optional[Path]:
    """Return a freshly-created ``projectN`` dir, or None if cap reached.

    Allocation is max-plus-one (does not fill gaps), so ``project1`` and
    ``project3`` existing means the next call returns ``project4``. Two
    processes sharing the same workspace (local-hub shared root) may both
    compute the same ``next_n`` and race; the loser retries up to
    ``_PROJECT_DIR_RACE_RETRIES`` times before giving up.
    """
    for _ in range(_PROJECT_DIR_RACE_RETRIES):
        indices = _existing_project_indices(agent_workspace)
        next_n = (max(indices) + 1) if indices else 1
        if next_n > max_projects:
            return None
        project_dir = agent_workspace / f"project{next_n}"
        try:
            project_dir.mkdir(parents=True, exist_ok=False)
            return project_dir
        except FileExistsError:
            continue
    return None


def named_project_dir(root: Path, name: str) -> Optional[Path]:
    """Return ``root/<sanitized name>``, creating it idempotently.

    The name is lower-cased, trimmed, and validated against
    ``PROJECT_NAME_PATTERN`` (alnum + ``-_`` only). Names containing path
    separators, ``..``, or that would resolve outside ``root`` are rejected
    with ``None``. ``mkdir`` uses ``exist_ok=True`` so two agents calling
    this with the same name converge on one shared directory.
    """
    if not isinstance(name, str):
        return None
    cleaned = name.strip().lower()
    if not cleaned or not PROJECT_NAME_PATTERN.fullmatch(cleaned):
        return None
    project_dir = root / cleaned
    try:
        resolved = project_dir.resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


def most_recent_project_dir(agent_workspace: Path) -> Optional[Path]:
    """Return the highest-numbered existing ``projectN`` dir, or None."""
    indices = _existing_project_indices(agent_workspace)
    if not indices:
        return None
    return agent_workspace / f"project{max(indices)}"


def save_code_blocks(blocks: List[CodeBlock], project_dir: Path) -> List[Path]:
    """Write each block to ``project_dir``; de-dup colliding names with _2, _3."""
    written: List[Path] = []
    used: set[str] = set()
    project_root = project_dir.resolve()
    for block in blocks:
        name = block.filename or "snippet.txt"
        if name in used or (project_dir / name).exists():
            stem, dot, ext = name.rpartition(".")
            if not dot:
                stem, ext = name, ""
            else:
                ext = "." + ext
            i = 2
            while True:
                candidate = f"{stem}_{i}{ext}"
                if candidate not in used and not (project_dir / candidate).exists():
                    name = candidate
                    break
                i += 1
        target = (project_dir / name).resolve()
        # Belt-and-braces: refuse anything that escaped the project dir.
        try:
            target.relative_to(project_root)
        except ValueError:
            continue
        target.write_text(block.content, encoding="utf-8")
        used.add(name)
        written.append(target)
    return written


def _has_test_file(project_dir: Path) -> bool:
    return any(project_dir.glob("test_*.py"))


def _truncate(text: str, limit: int = PYTEST_OUTPUT_CHAR_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


def maybe_run_pytest(
    project_dir: Path, timeout: float = PYTEST_TIMEOUT_SECONDS
) -> Tuple[bool, str]:
    """If ``project_dir`` contains any ``test_*.py``, run pytest against it.

    Mirrors the subprocess + timeout + truncation pattern used by Part 2's
    ``_run_post_edit_tests`` (assignment2_part2/agent.py:192-216).
    Returns ``(ran, output)``: when no test files are present, returns
    ``(False, "")`` without spawning a subprocess.
    """
    if not _has_test_file(project_dir):
        return (False, "")
    args = [sys.executable, "-m", "pytest", str(project_dir), "-q"]
    try:
        completed = subprocess.run(
            args,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return (True, "Command exited with code 127.\nPython executable was not found.")
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or exc.stderr or "")
        detail = f"\n{partial.strip()}" if partial and partial.strip() else ""
        return (
            True,
            _truncate(
                f"Command exited with code 124.\n"
                f"Auto-pytest timed out after {timeout:.0f} seconds.{detail}"
            ),
        )
    output = (completed.stdout.strip() or completed.stderr.strip() or "(no output)")
    if completed.returncode != 0:
        output = f"Command exited with code {completed.returncode}.\n{output}"
    return (True, _truncate(output))


def _agent_facing_path(agent_id: str, project_name: str, filename: str) -> str:
    return f"/workspace/{agent_id}/{project_name}/{filename}"


def process_shared_code(
    message_text: str,
    agent_id: str,
    active_project: Path,
    *,
    auto_pytest: bool = True,
    shared_root: Optional[Path] = None,
) -> Optional[str]:
    """Extract code from a peer message and save it into ``active_project``.

    Returns None when the message contains no code fences (no-op) so the
    caller can skip appending to ``runtime_guidance``. Otherwise returns a
    single string suitable for ``runtime_guidance.append(...)``. The
    caller (``group_chat``) owns project allocation — this function never
    creates or switches project dirs.

    When ``auto_pytest`` is False the runtime will not spawn pytest on the
    saved files; the caller is expected to require an explicit ``run_tests``
    tool call instead. Local docker-compose mode passes ``auto_pytest=False``
    because shared dirs are co-writeable and a peer could modify the file
    between save and pytest (TOCTOU).

    ``shared_root`` indicates that ``active_project`` lives under a
    co-visible shared root (``/workspace/shared``); when set, the guidance
    reports the path as ``/workspace/shared/<project>/<file>`` rather than
    as the per-agent private path.
    """
    blocks = extract_code_blocks(message_text)
    if not blocks:
        return None
    written = save_code_blocks(blocks, active_project)
    if not written:
        return None
    canonical_by_name = {
        block.filename: block.canonical
        for block in blocks
    }
    project_name = active_project.name
    if shared_root is not None:
        saved_lines = [
            (
                f"- /workspace/shared/{project_name}/{p.name}"
                if canonical_by_name.get(p.name, False)
                else f"- /workspace/shared/{project_name}/{p.name} (snippet; no # file directive)"
            )
            for p in written
        ]
        location = f"/workspace/shared/{project_name}/"
    else:
        saved_lines = [
            (
                f"- {_agent_facing_path(agent_id, project_name, p.name)}"
                if canonical_by_name.get(p.name, False)
                else (
                    f"- {_agent_facing_path(agent_id, project_name, p.name)} "
                    "(snippet; no # file directive)"
                )
            )
            for p in written
        ]
        location = f"/workspace/{agent_id}/{project_name}/"
    if auto_pytest:
        ran, output = maybe_run_pytest(active_project)
        if ran:
            pytest_line = f"Auto-pytest: ran. Result:\n{output}"
        else:
            pytest_line = "Auto-pytest: not run (no test_*.py files)."
    else:
        pytest_line = (
            "Auto-pytest: skipped (call run_tests on the saved test file to verify)."
        )
    return (
        f"A peer message contained code. The runtime saved it to {location}:\n"
        + "\n".join(saved_lines)
        + f"\n{pytest_line}\n"
        "You may read_file these paths, propose changes, or discuss in chat. "
        "Treat files marked as snippets as untrusted non-canonical context until "
        "a peer shares a block with `# file: <filename>`. Do not re-create or "
        "duplicate these files."
    )
