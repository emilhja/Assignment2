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


def _resolve_workspace_path(path_text: str) -> Path:
    if not isinstance(path_text, str) or not path_text.strip():
        raise ValueError("path must be a non-empty string")

    root = workspace_root()
    normalized_text = path_text.strip().replace("\\", "/")
    if normalized_text == "/workspace":
        normalized_text = "."
    elif normalized_text.startswith("/workspace/"):
        normalized_text = normalized_text[len("/workspace/") :]

    raw_path = Path(normalized_text)
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    else:
        candidate = (root / raw_path).resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path must stay inside workspace: {root}") from exc
    return candidate


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
        completed = subprocess.run(
            [bash_path, "-lc", command],
            shell=False,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
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


def edit_section(path: str, old_text: str, new_text: str) -> str:
    """Replace exactly one matching section in a workspace file."""

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
    matches = original.count(old_text)
    if matches == 0:
        return "Edit blocked: old_text was not found in the file."
    if matches > 1:
        return "Edit blocked: old_text appears more than once; provide a unique section."

    updated = original.replace(old_text, new_text, 1)
    target.write_text(updated, encoding="utf-8")
    return _truncate(f"Edited one section in {target}.")


def replace_text(path: str, old_text: str, new_text: str, all_occurrences: bool = False) -> str:
    """Replace one or all exact text matches in a workspace file."""

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
    if not isinstance(all_occurrences, bool):
        return "Edit blocked: all_occurrences must be a boolean."

    original = target.read_text(encoding="utf-8")
    matches = original.count(old_text)
    if matches == 0:
        return "Edit blocked: old_text was not found in the file."
    if matches > 1 and not all_occurrences:
        return (
            f"Edit blocked: old_text appears {matches} times; "
            "set all_occurrences to true only if the user asked to replace every match."
        )

    replace_count = matches if all_occurrences else 1
    updated = original.replace(old_text, new_text, replace_count)
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
        description="Replace one exact section in one workspace file.",
        required_args=("path", "old_text", "new_text"),
        handler=lambda args: edit_section(args["path"], args["old_text"], args["new_text"]),
    ),
    "replace_text": ToolSpec(
        name="replace_text",
        description="Replace one or all exact text matches in one workspace file.",
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
