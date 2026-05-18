import shutil
import subprocess

MAX_OUTPUT_CHARS = 4000
COMMAND_TIMEOUT_SECONDS = 10
BASH_NOT_FOUND_MESSAGE = (
    "bash executable was not found. Install Git Bash, WSL, or make bash available in PATH."
)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [output truncated]"


def run_bash(command: str) -> str:
    bash_path = shutil.which("bash")
    if bash_path is None:
        return BASH_NOT_FOUND_MESSAGE

    try:
        completed = subprocess.run(
            [bash_path, "-lc", command],
            shell=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return BASH_NOT_FOUND_MESSAGE
    except subprocess.TimeoutExpired:
        return f"Command timed out after {COMMAND_TIMEOUT_SECONDS} seconds."

    output = "".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()
    if not output:
        output = "(no output)"

    if completed.returncode != 0:
        output = f"Return code: {completed.returncode}\n{output}"

    return _truncate(output)
