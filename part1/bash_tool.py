import shutil
import subprocess


MAX_OUTPUT_CHARS = 4000
COMMAND_TIMEOUT_SECONDS = 10
BASH_NOT_FOUND = "bash not found in PATH"


def run_bash(command: str) -> str:
    bash_path = shutil.which("bash")
    if bash_path is None:
        return BASH_NOT_FOUND

    try:
        completed = subprocess.run(
            [bash_path, "-lc", command],
            shell=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return BASH_NOT_FOUND
    except subprocess.TimeoutExpired:
        return f"Command timed out after {COMMAND_TIMEOUT_SECONDS} seconds."

    output = completed.stdout.strip() or completed.stderr.strip() or "(no output)"
    if completed.returncode != 0:
        output = f"Command exited with code {completed.returncode}.\n{output}"
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
    return output
