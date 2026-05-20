import json
import os
from pathlib import Path
import re
import subprocess
import sys

from llm_client import complete_chat
from parser import parse_response
from safety import confirm_command, intent_refusal
from session_store import SessionStore
from tools import MAX_OUTPUT_CHARS, TOOL_REGISTRY, run_tool


MAX_STEPS = 8
MAX_CONTEXT_TURNS = 4
MAX_CONTEXT_CHARS = 2000
EDIT_TOOLS = {"create_file", "edit_section", "replace_text"}
POST_EDIT_TEST_COMMAND = "python -m pytest assignment2_part2 -q"
POST_EDIT_TEST_LOCAL_COMMAND = "python -m pytest -q"
POST_EDIT_TEST_TIMEOUT_SECONDS = 120
DEBUG_ENV_VAR = "AGENT_DEBUG"
SESSION_DB_ENV_VAR = "AGENT_SESSION_DB"
CONFIG_DIR = Path(__file__).with_name("config")
SYSTEM_PROMPT_FILE = CONFIG_DIR / "system_prompt.txt"
EXIT_COMMANDS = {"exit", "quit", "q"}
EXECUTION_REQUEST_PATTERN = re.compile(
    r"(?i)\b(execute|run|launch|start)\s+(it|the\s+(script|file|program)|/workspace/\S+|\.\/\S+)"
)
CREATED_FILE_PATTERN = re.compile(r"^(?:Created|Overwrote) file in (?P<path>/workspace(?:/.*)?)\.$")
INTERNAL_TEST_NOISE_PATTERN = re.compile(
    r"(?i)\b(pytest|test suite|post-edit test|\.pytest_cache|cache warning|"
    r"read-only file\s*system|read-only filesystem)\b"
)
TEST_DETAIL_REQUEST_PATTERN = re.compile(r"(?i)\b(test|tests|pytest|warning|warnings|cache)\b")


def _debug_enabled() -> bool:
    value = os.getenv(DEBUG_ENV_VAR, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_system_prompt() -> str:
    prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
    tool_names = ", ".join(sorted(TOOL_REGISTRY))
    return (
        f"{prompt}\n\n"
        f"Runtime facts:\n"
        f"- Registered tool names: {tool_names}.\n"
        f"- Tool observations are limited to {MAX_OUTPUT_CHARS} characters.\n"
    )


def _session_db_path() -> str:
    configured = os.getenv(SESSION_DB_ENV_VAR)
    if configured:
        return configured
    data_dir = Path(__file__).with_name("data")
    data_dir.mkdir(exist_ok=True)
    return str(data_dir / "session_history.sqlite3")


def is_exit_command(text: str) -> bool:
    return text.strip().lower() in EXIT_COMMANDS


def _json_dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _format_prior_context(prior_context: list[str] | None) -> str | None:
    if not prior_context:
        return None

    context = "\n\n".join(prior_context[-MAX_CONTEXT_TURNS:])
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]

    return (
        "Recent CLI turns for resolving references in the current request only. "
        "Use the current user request as the task to answer.\n"
        f"{context}"
    )


def _invalid_response_guidance(error: str | None) -> str:
    tool_payload = {
        "type": "tool_call",
        "tool": "bash",
        "args": {"command": "pwd"},
        "reason": "brief reason",
    }
    final_payload = {"type": "final", "answer": "answer to the user"}
    guidance = (
        "Your previous response was invalid. Respond with exactly one JSON object and no prose.\n"
        f"Valid tool-call example: {_json_dump(tool_payload)}\n"
        f"Valid final-answer example: {_json_dump(final_payload)}"
    )
    if error:
        guidance += f"\nParser error: {error}"
    return guidance


def _tool_observation_message(tool: str, observation: str) -> str:
    return _json_dump({"type": "tool_observation", "tool": tool, "observation": observation})


def _truncate_observation(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"


def _edit_succeeded(tool: str, observation: str) -> bool:
    if tool not in EDIT_TOOLS:
        return False
    return observation.startswith(("Created file", "Overwrote file", "Edited one section", "Replaced "))


def _user_requested_execution(user_task: str) -> bool:
    return bool(EXECUTION_REQUEST_PATTERN.search(user_task))


def _created_file_path(observation: str) -> str | None:
    match = CREATED_FILE_PATTERN.match(observation.strip())
    if not match:
        return None
    return match.group("path")


def _create_without_execution_answer(paths: list[str]) -> str:
    if len(paths) == 1:
        return (
            f"Created {paths[0]}. I did not execute it because script execution "
            "is blocked by the current safety policy."
        )
    joined_paths = ", ".join(paths)
    return (
        f"Created these files: {joined_paths}. I did not execute them because "
        "script execution is blocked by the current safety policy."
    )


def _create_without_execution_note(paths: list[str]) -> str:
    joined_paths = ", ".join(paths)
    return (
        f"Runtime note: create_file created {joined_paths}. The post-edit test suite "
        "is not execution of user-created files. Do not claim these files were run."
    )


def _user_requested_test_details(user_task: str) -> bool:
    return bool(TEST_DETAIL_REQUEST_PATTERN.search(user_task))


def _strip_internal_test_noise(answer: str, user_task: str, had_successful_edit: bool) -> str:
    if not had_successful_edit or _user_requested_test_details(user_task):
        return answer

    paragraphs = re.split(r"\n\s*\n", answer.strip())
    kept: list[str] = []
    removed_noise = False
    for paragraph in paragraphs:
        if INTERNAL_TEST_NOISE_PATTERN.search(paragraph):
            removed_noise = True
            continue
        if removed_noise and re.match(r"(?is)^\s*would you like\b", paragraph):
            continue
        kept.append(paragraph.strip())

    if not removed_noise or not kept:
        return answer
    return "\n\n".join(kept).strip()


def _post_edit_test_invocation() -> tuple[str, Path, list[str]]:
    part_root = Path(__file__).resolve().parent
    repo_root = part_root.parent
    if (repo_root / "assignment2_part2").exists():
        return (
            POST_EDIT_TEST_COMMAND,
            repo_root,
            [sys.executable, "-m", "pytest", "assignment2_part2", "-q"],
        )
    return (
        POST_EDIT_TEST_LOCAL_COMMAND,
        part_root,
        [sys.executable, "-m", "pytest", "-q"],
    )


def _run_post_edit_tests() -> str:
    _command, cwd, args = _post_edit_test_invocation()

    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=POST_EDIT_TEST_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return "Command exited with code 127.\nPython executable was not found."
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or exc.stderr or ""
        detail = f"\n{output.strip()}" if output.strip() else ""
        return _truncate_observation(
            f"Command exited with code 124.\nPost-edit tests timed out after "
            f"{POST_EDIT_TEST_TIMEOUT_SECONDS} seconds.{detail}"
        )

    output = completed.stdout.strip() or completed.stderr.strip() or "(no output)"
    if completed.returncode != 0:
        output = f"Command exited with code {completed.returncode}.\n{output}"
    return _truncate_observation(output)


def _run_tool_call(tool: str, args: dict) -> str:
    if tool == "bash":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return "Tool error: bash requires a non-empty string command."
        if not confirm_command(command):
            return "The command was denied, so I did not run it."

    return run_tool(tool, args)


def _finalize(store: SessionStore, answer: str, kind: str = "final") -> str:
    store.record("assistant", kind, answer)
    print("\nFinal answer:")
    print(answer)
    return answer


def run_task(
    user_task: str,
    store: SessionStore | None = None,
    prior_context: list[str] | None = None,
) -> str | None:
    """Handle one user task from first prompt to final JSON answer."""

    debug = _debug_enabled()
    owns_store = store is None
    if store is None:
        store = SessionStore(_session_db_path())

    try:
        refusal_reason = intent_refusal(user_task)
        if refusal_reason:
            store.record("user", "message", user_task)
            return _finalize(
                store,
                f"I'm sorry. I'm afraid I can't do that. {refusal_reason}",
            )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": load_system_prompt()},
        ]
        context_message = _format_prior_context(prior_context)
        if context_message:
            messages.append({"role": "system", "content": context_message})
        messages.append({"role": "user", "content": user_task})
        store.record("user", "message", user_task)
        execution_requested = _user_requested_execution(user_task)
        created_paths: list[str] = []
        had_successful_edit = False

        for step in range(1, MAX_STEPS + 1):
            if debug:
                print(f"\n--- Step {step} ---")

            raw_response = complete_chat(messages)
            store.record("assistant", "raw_json", raw_response)

            if debug:
                print("\nAssistant raw response:")
                print(raw_response)

            messages.append({"role": "assistant", "content": raw_response})
            parsed = parse_response(raw_response, allowed_tools=TOOL_REGISTRY.keys())

            if parsed.kind == "final":
                assert parsed.answer is not None
                if execution_requested and created_paths:
                    return _finalize(store, _create_without_execution_answer(created_paths))
                answer = _strip_internal_test_noise(
                    parsed.answer,
                    user_task,
                    had_successful_edit,
                )
                return _finalize(store, answer)

            if parsed.kind == "tool_call":
                observation = _run_tool_call(parsed.tool, parsed.args)
                store.record(
                    "tool",
                    parsed.tool,
                    _json_dump({"args": parsed.args, "observation": observation}),
                )

                if debug:
                    print("\nObservation:")
                    print(observation)

                messages.append(
                    {"role": "user", "content": _tool_observation_message(parsed.tool, observation)}
                )
                if _edit_succeeded(parsed.tool, observation):
                    had_successful_edit = True
                    created_path = None
                    if parsed.tool == "create_file":
                        created_path = _created_file_path(observation)
                        if created_path:
                            created_paths.append(created_path)
                    test_command, _cwd, _args = _post_edit_test_invocation()
                    test_observation = _run_post_edit_tests()
                    store.record(
                        "tool",
                        "bash",
                        _json_dump(
                            {
                                "args": {"command": test_command},
                                "observation": test_observation,
                            }
                        ),
                    )
                    if debug:
                        print("\nPost-edit test observation:")
                        print(test_observation)
                    messages.append(
                        {
                            "role": "user",
                            "content": _tool_observation_message("bash", test_observation),
                        }
                    )
                    if execution_requested and created_path:
                        messages.append(
                            {
                                "role": "user",
                                "content": _tool_observation_message(
                                    "runtime_note",
                                    _create_without_execution_note([created_path]),
                                ),
                            }
                        )
                    if test_observation.startswith("Command exited with code"):
                        return _finalize(
                            store,
                            "Edit completed, but the post-edit test suite failed:\n"
                            f"{test_observation}",
                        )
                continue

            guidance = _invalid_response_guidance(parsed.error)
            store.record("system", "parser_guidance", guidance)
            if debug:
                print("\nParser guidance:")
                print(guidance)
            messages.append({"role": "user", "content": guidance})

        return _finalize(
            store,
            "Stopped: reached the max step limit without a final answer.",
            kind="stopped",
        )
    finally:
        if owns_store:
            store.close()


def main() -> None:
    print("Assignment 2 Part 2 Structured Tool Agent")
    print("Enter a task, or type 'exit' or 'quit' to stop.")

    store = SessionStore(_session_db_path())
    recent_context: list[str] = []
    try:
        while True:
            try:
                user_task = input("\nInput to: HAL 9000 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                return

            if not user_task:
                continue
            if is_exit_command(user_task):
                print("Goodbye.")
                return

            try:
                answer = run_task(user_task, store=store, prior_context=recent_context)
                if answer:
                    recent_context.append(f"User: {user_task}\nAssistant: {answer}")
                    recent_context = recent_context[-MAX_CONTEXT_TURNS:]
            except Exception as exc:
                store.record("system", "error", repr(exc))
                print(f"\nError: {exc}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
