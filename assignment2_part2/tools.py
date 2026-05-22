import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from safety import safety_check


# Limit returned command output so very large results do not overwhelm the agent.
MAX_OUTPUT_CHARS = 4000
COMMAND_TIMEOUT_SECONDS = 10
BASH_NOT_FOUND_MESSAGE = "I could not find bash. Install Git Bash or WSL, or add bash to PATH."


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Return short text unchanged, but shorten text that is too long."""

    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [output truncated]"


def _default_workspace_root() -> Path:
    docker_workspace = Path("/workspace")
    if docker_workspace.exists():
        return docker_workspace.resolve()
    return Path(__file__).with_name("workspace").resolve()


def workspace_root() -> Path:
    configured = os.getenv("AGENT_WORKSPACE")
    if configured:
        return Path(configured).resolve()
    return _default_workspace_root()


def shared_workspace_root() -> Path | None:
    configured = os.getenv("SHARED_WORKSPACE")
    if not configured:
        return None
    return Path(configured).resolve()


def _allowed_roots() -> list[Path]:
    roots = [workspace_root()]
    shared = shared_workspace_root()
    if shared is not None and shared != roots[0]:
        roots.append(shared)
    return roots


def _resolve_workspace_path(path_text: str) -> Path:
    if not isinstance(path_text, str) or not path_text.strip():
        raise ValueError("path must be a non-empty string")

    roots = _allowed_roots()
    private_root = roots[0]
    normalized_text = path_text.strip().replace("\\", "/")

    # Agents often drop the leading slash and type "workspace/...". Treat
    # that as if it were the absolute "/workspace/..." so paths land at the
    # intended location instead of nesting under the private root.
    if normalized_text == "workspace" or normalized_text.startswith("workspace/"):
        normalized_text = "/" + normalized_text

    shared = shared_workspace_root()
    if shared is not None:
        if normalized_text == "/workspace/shared":
            return shared
        if normalized_text.startswith("/workspace/shared/"):
            relative_text = normalized_text[len("/workspace/shared/") :]
            candidate = (shared / relative_text).resolve()
            try:
                candidate.relative_to(shared)
                return candidate
            except ValueError:
                pass

    raw_path = Path(normalized_text)

    # Literal interpretation first: if the path already resolves inside an
    # allowed root, accept it as-is. This routes "/workspace/shared/..." to
    # the shared root when SHARED_WORKSPACE is set.
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
        for root in roots:
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue

    # Legacy fallback: strip "/workspace/" so the agent's mental "/workspace"
    # maps to its private workspace root. Required when AGENT_WORKSPACE is
    # not literally "/workspace" (e.g., in tests or local dev).
    if normalized_text == "/workspace":
        normalized_text = "."
    elif normalized_text.startswith("/workspace/"):
        normalized_text = normalized_text[len("/workspace/") :]

    raw_path = Path(normalized_text)
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    else:
        candidate = (private_root / raw_path).resolve()

    for root in roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue

    allowed = ", ".join(str(r) for r in roots)
    raise ValueError(f"path must stay inside an allowed workspace ({allowed})")


def _display_workspace_path(path: Path) -> str:
    resolved = path.resolve()
    shared = shared_workspace_root()
    if shared is not None:
        try:
            relative = resolved.relative_to(shared)
        except ValueError:
            pass
        else:
            if str(relative) == ".":
                return "/workspace/shared"
            return "/workspace/shared/" + relative.as_posix()

    root = workspace_root()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return str(resolved)
    if str(relative) == ".":
        return "/workspace"
    return "/workspace/" + relative.as_posix()


def _bash_subprocess_env(root: Path) -> dict:
    """Return a minimal environment for the bash subprocess (no provider keys)."""

    return {
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
        "HOME": str(root),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TERM": "dumb",
        "PWD": str(root),
    }


def run_bash(command: str) -> str:
    """Run one Bash command and return the text the agent should see."""

    allowed, reason = safety_check(command)
    if not allowed:
        return reason or "Blocked by safety check."

    bash_path = shutil.which("bash")
    if bash_path is None:
        return BASH_NOT_FOUND_MESSAGE

    try:
        root = workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        # --noprofile --norc stops ~/.bash_profile and ~/.bashrc from running
        # before the command, so the subprocess sees only the env we hand it.
        # The minimal env intentionally omits API keys and provider secrets so
        # a leak attempt like `echo $GROQ_API_KEY` finds nothing to print.
        completed = subprocess.run(
            [bash_path, "--noprofile", "--norc", "-c", command],
            shell=False,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=_bash_subprocess_env(root),
        )
    except FileNotFoundError:
        return BASH_NOT_FOUND_MESSAGE
    except subprocess.TimeoutExpired as exc:
        if isinstance(exc.stdout, str) and exc.stdout:
            partial_output = exc.stdout.strip()
        elif isinstance(exc.stderr, str) and exc.stderr:
            partial_output = exc.stderr.strip()
        else:
            partial_output = ""

        if partial_output:
            return _truncate(
                f"I stopped the command after {COMMAND_TIMEOUT_SECONDS} seconds.\n{partial_output}"
            )
        return f"I stopped the command after {COMMAND_TIMEOUT_SECONDS} seconds."

    if completed.stdout:
        output = completed.stdout.strip()
    elif completed.stderr:
        output = completed.stderr.strip()
    else:
        output = ""

    if not output:
        output = "(no output)"

    if completed.returncode != 0:
        output = f"Command exited with code {completed.returncode}.\n{output}"

    return _truncate(output)


def _whole_line_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """Return exact match spans that cover complete line sections."""

    spans = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index == -1:
            return spans

        end = index + len(needle)
        starts_on_line = index == 0 or text[index - 1] == "\n"
        ends_on_line = needle.endswith("\n") or end == len(text) or text[end] == "\n"
        if starts_on_line and ends_on_line:
            spans.append((index, end))
        start = end


def _replace_spans(text: str, spans: list[tuple[int, int]], replacement: str) -> str:
    updated = text
    for start, end in reversed(spans):
        updated = updated[:start] + replacement + updated[end:]
    return updated


def _load_edit_target(
    path: str, old_text: str, new_text: str
) -> tuple[Path, str, list[tuple[int, int]]] | str:
    try:
        target = _resolve_workspace_path(path)
    except ValueError as exc:
        return f"Edit blocked: {exc}"

    if not target.exists():
        return f"Edit blocked: file does not exist: {target}"
    if not target.is_file():
        return f"Edit blocked: path is not a file: {target}"
    if not isinstance(old_text, str) or not old_text:
        return "Edit blocked: old_text must be a non-empty string."
    if not isinstance(new_text, str):
        return "Edit blocked: new_text must be a string."

    original = target.read_text(encoding="utf-8")
    spans = _whole_line_spans(original, old_text)
    if not spans:
        return "Edit blocked: old_text was not found as a complete line section."

    return target, original, spans


def edit_section(path: str, old_text: str, new_text: str) -> str:
    """Replace exactly one whole-line matching section in a workspace file."""

    loaded = _load_edit_target(path, old_text, new_text)
    if isinstance(loaded, str):
        return loaded

    target, original, spans = loaded
    if len(spans) > 1:
        return "Edit blocked: old_text appears more than once; provide a unique section."

    updated = _replace_spans(original, spans, new_text)
    target.write_text(updated, encoding="utf-8")
    return _truncate(f"Edited one section in {target}.")


def read_file(path: str) -> str:
    """Return the UTF-8 contents of one workspace file, truncated to MAX_OUTPUT_CHARS.

    No shell, no operator approval. The agent uses this to verify shared-file
    state before claiming a scope or asserting contents in a final answer.
    Refusals use the `Edit blocked:` prefix so the existing "do not retry"
    rule in the system prompt covers them.
    """

    try:
        target = _resolve_workspace_path(path)
    except ValueError as exc:
        return f"Edit blocked: {exc}"

    if not target.exists():
        return f"Edit blocked: file does not exist: {target}"
    if not target.is_file():
        return f"Edit blocked: path is not a file: {target}"

    try:
        contents = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Edit blocked: file is not valid UTF-8: {target}"

    display = _display_workspace_path(target)
    return _truncate(f"--- {display} ---\n{contents}")


def create_file(path: str, content: str, overwrite: bool = False) -> str:
    """Create one file inside the workspace without shell redirection."""

    if not isinstance(content, str):
        return "Edit blocked: content must be a string."
    if not isinstance(overwrite, bool):
        return "Edit blocked: overwrite must be a boolean."

    try:
        target = _resolve_workspace_path(path)
    except ValueError as exc:
        return f"Edit blocked: {exc}"

    if target.exists() and target.is_dir():
        return f"Edit blocked: path is a directory: {target}"
    if target.exists() and not overwrite:
        return f"Edit blocked: file already exists: {target}"
    if target.parent.exists() and not target.parent.is_dir():
        return f"Edit blocked: parent path is not a directory: {target.parent}"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    action = "Overwrote" if overwrite else "Created"
    return _truncate(f"{action} file in {_display_workspace_path(target)}.")


def replace_text(path: str, old_text: str, new_text: str, all_occurrences: bool = False) -> str:
    """Replace one or all whole-line exact text matches in a workspace file."""

    if not isinstance(all_occurrences, bool):
        return "Edit blocked: all_occurrences must be a boolean."

    loaded = _load_edit_target(path, old_text, new_text)
    if isinstance(loaded, str):
        return loaded

    target, original, spans = loaded
    if len(spans) > 1 and not all_occurrences:
        return (
            f"Edit blocked: old_text appears {len(spans)} times; "
            "set all_occurrences to true only if the user asked to replace every match."
        )

    selected_spans = spans if all_occurrences else spans[:1]
    replace_count = len(selected_spans)
    updated = _replace_spans(original, selected_spans, new_text)
    target.write_text(updated, encoding="utf-8")
    return _truncate(f"Replaced {replace_count} occurrence(s) in {target}.")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_args: tuple[str, ...]
    handler: object


TOOL_REGISTRY = {
    "bash": ToolSpec(
        name="bash",
        description="Run one safe local Bash command after safety checks and manual approval.",
        required_args=("command",),
        handler=lambda args: run_bash(args["command"]),
    ),
    "edit_section": ToolSpec(
        name="edit_section",
        description="Replace one exact whole-line section in one workspace file.",
        required_args=("path", "old_text", "new_text"),
        handler=lambda args: edit_section(args["path"], args["old_text"], args["new_text"]),
    ),
    "read_file": ToolSpec(
        name="read_file",
        description="Read one workspace file as UTF-8 text without shell or operator approval.",
        required_args=("path",),
        handler=lambda args: read_file(args["path"]),
    ),
    "create_file": ToolSpec(
        name="create_file",
        description="Create one file inside the workspace without shell redirection.",
        required_args=("path", "content"),
        handler=lambda args: create_file(
            args["path"],
            args["content"],
            args.get("overwrite", False),
        ),
    ),
    "replace_text": ToolSpec(
        name="replace_text",
        description="Replace one or all exact whole-line text matches in one workspace file.",
        required_args=("path", "old_text", "new_text"),
        handler=lambda args: replace_text(
            args["path"],
            args["old_text"],
            args["new_text"],
            args.get("all_occurrences", False),
        ),
    ),
}


def validate_tool_args(tool_name: str, args: dict) -> str | None:
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        return f"Unknown tool: {tool_name}."
    if not isinstance(args, dict):
        return "Tool args must be an object."
    missing = [name for name in spec.required_args if name not in args]
    if missing:
        return f"Missing required args for {tool_name}: {', '.join(missing)}."
    return None


def run_tool(tool_name: str, args: dict) -> str:
    error = validate_tool_args(tool_name, args)
    if error:
        return error
    return TOOL_REGISTRY[tool_name].handler(args)
