"""Local tool implementations for the minimal agent."""

from __future__ import annotations

import shutil
import subprocess

# Limit returned command output so very large results do not overwhelm the agent.
MAX_OUTPUT_CHARS = 4000
COMMAND_TIMEOUT_SECONDS = 10
BASH_NOT_FOUND_MESSAGE = (
    "bash executable was not found. Install Git Bash, WSL, or make bash available in PATH."
)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Return short text unchanged, but shorten text that is too long."""

    # Keep short output unchanged; add a marker when long output is cut off.
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [output truncated]"


def run_bash(command: str) -> str:
    """Run one Bash command and return the text the agent should see."""

    bash_path = shutil.which("bash")
    if bash_path is None:
        return BASH_NOT_FOUND_MESSAGE

    try:
        # Save both normal output and error output so we can return one result.
        completed = subprocess.run(
            [bash_path, "-lc", command],
            shell=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return BASH_NOT_FOUND_MESSAGE
    except subprocess.TimeoutExpired as exc:
        # If the command printed text before timing out, return that text too.
        partial_output = "".join(
            part or "" for part in (exc.stdout, exc.stderr) if isinstance(part, str)
        ).strip()
        if "fatal error in forked process" in partial_output or "child_copy:" in partial_output:
            partial_output = ""
        if partial_output:
            return _truncate(
                f"Command timed out after {COMMAND_TIMEOUT_SECONDS} seconds.\n{partial_output}"
            )
        return f"Command timed out after {COMMAND_TIMEOUT_SECONDS} seconds."

    output = "".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()
    if not output:
        # Some commands succeed without printing anything; return a clear message.
        output = "(no output)"

    if completed.returncode != 0:
        # Non-zero return codes mean failure; include the code with the output.
        output = f"Return code: {completed.returncode}\n{output}"

    return _truncate(output)
