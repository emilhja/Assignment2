import shutil
import subprocess

MAX_OUTPUT_CHARS = 4000
COMMAND_TIMEOUT_SECONDS = 10
BASH_NOT_FOUND_MESSAGE = "I could not find bash. Install Git Bash or WSL, or add bash to PATH."


def _truncate(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [output truncated]"


def run_bash(command):
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
