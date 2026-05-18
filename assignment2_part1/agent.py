"""Interactive CLI for Assignment 2 Part 1: a minimal ReAct-style agent."""

from __future__ import annotations

import os

from llm_client import complete_chat
from parser import parse_response
from safety import confirm_command, safety_check
from tools import run_bash

# Stop after a few model replies so the agent cannot loop forever.
MAX_STEPS = 5
DEBUG_ENV_VAR = "AGENT_DEBUG"

SYSTEM_PROMPT = """You are a minimal ReAct-style software engineering assistant.

You must respond using exactly one of these two formats:

Thought: <brief reason>
Action: bash
Command: <one safe local bash command>

or:

Thought: <brief reason>
Final Answer: <answer to the user>

Core rules:
- Use raw text only.
- Do not use JSON, Markdown code fences, function calls, or any tool format other than Action: bash.
- If you use Action: bash, the next line must be Command:.
- Request only one command at a time.
- Do not write anything before Thought:.

Tool rules:
- Use bash only when needed.
- If the user asks about files, directories, command output, or local environment state, do not guess. Use a safe, narrow bash command.
- Never fabricate file names, command output, or local system state.
- Prefer safe, narrow commands such as pwd, ls, cat, head, sed, and wc.
- Do not request destructive commands, sudo, package managers, Docker commands, shutdown, or reboot.

Workspace rules:
- The agent code may run from /app.
- User-created files should be placed in /workspace unless the user explicitly says otherwise.
- Do not run Docker commands from inside the container. Docker commands must be run on the host machine.

Observation rules:
- Treat Observation as factual tool output.
- If the Observation answers the user's request, stop and give Final Answer.
- A standalone cd command does not persist across tool calls. Use cd /workspace && <command> only when needed.
"""


def _debug_enabled() -> bool:
    """Return True when AGENT_DEBUG asks the agent to print extra details."""

    value = os.getenv(DEBUG_ENV_VAR, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run_task(user_task: str) -> None:
    """Handle one user task from the first prompt to the final answer."""

    debug = _debug_enabled()

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_task},
    ]

    # Ask the model for the next step: one Bash command or a final answer.
    for step in range(1, MAX_STEPS + 1):
        if debug:
            print(f"\n--- Step {step} ---")
        raw_response = complete_chat(messages)
        if debug:
            print("\nAssistant raw response:")
            print(raw_response)

        messages.append({"role": "assistant", "content": raw_response})
        parsed = parse_response(raw_response)

        if parsed.kind == "final":
            print("\nFinal answer:")
            print(parsed.answer)
            return

        if parsed.kind == "action" and parsed.action == "bash":
            assert parsed.command is not None
            # Check dangerous commands before asking the user; blocked commands never run.
            allowed, reason = safety_check(parsed.command)
            if not allowed:
                observation = reason or "Blocked by safety check."
            elif not confirm_command(parsed.command):
                observation = "Command denied by user; it was not executed."
            else:
                observation = run_bash(parsed.command)

            if debug:
                print("\nObservation:")
                print(observation)

            messages.append({"role": "user", "content": f"Observation: {observation}"})
            continue

        # Tell the model what was wrong, then let the loop ask it again.
        guidance = (
            "Your previous response was invalid.\n\n"
            "You must now respond with exactly one of these formats:\n\n"
            "Thought: ...\nAction: bash\nCommand: ...\n\n"
            "or:\n"
            "Thought: ...\nFinal Answer: ...\n\n"
            "Do not omit Thought.\n"
            "Do not omit Action: bash when requesting a command.\n"
            "Do not put the command on the Action line.\n"
            "Do not use code fences.\n"
            "Do not invent observations."
        )
        if parsed.error:
            guidance += f"\nParser error: {parsed.error}"
        if debug:
            print("\nParser guidance:")
            print(guidance)
        messages.append({"role": "user", "content": guidance})

    print("\nStopped: reached the max step limit without a final answer.")


def main() -> None:
    print("Assignment 2 Part 1 Minimal ReAct Agent")
    print("Enter a task, or type 'exit' or 'quit' to stop.")

    # Keep asking for new tasks until the user exits or presses Ctrl+C/Ctrl+D.
    while True:
        try:
            user_task = input("\nInput to: HAL 9000 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return

        if not user_task:
            continue
        if user_task.lower() in {"exit", "quit"}:
            print("Goodbye.")
            return

        try:
            run_task(user_task)
        except Exception as exc:
            print(f"\nError: {exc}")


if __name__ == "__main__":
    main()
