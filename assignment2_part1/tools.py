import shutil
import subprocess

MAX_OUTPUT_CHARS = 4000
COMMAND_TIMEOUT_SECONDS = 10
BASH_NOT_FOUND_MESSAGE = "bash not found in PATH"


def run_bash(command):
    bash_path = shutil.which("bash")
    if bash_path is None:
        return BASH_NOT_FOUND_MESSAGE

    # run it and grab whatever comes out
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
        out = completed.stdout.strip()
    elif completed.stderr:
        out = completed.stderr.strip()
    else:
        out = ""

    if not out:
        out = "(no output)"

    if completed.returncode != 0:
        out = f"Command exited with code {completed.returncode}.\n{out}"

    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
    return out
