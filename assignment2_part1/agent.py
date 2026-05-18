import os

from llm_client import complete_chat
from parser import parse_response
from safety import confirm_command, safety_check
from tools import run_bash

MAX_STEPS = 5

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


def run_task(user_task):
    debug = os.getenv("AGENT_DEBUG", "").strip() == "1"

    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_task},
    ]

    for step in range(1, MAX_STEPS + 1):
        if debug:
            print(f"\n--- Step {step} ---")
        resp = complete_chat(msgs)
        if debug:
            print("\nAssistant raw response:")
            print(resp)

        msgs.append({"role": "assistant", "content": resp})
        result = parse_response(resp)

        if result.kind == "final":
            print("\nFinal answer:")
            print(result.answer)
            return

        if result.kind == "action" and result.action == "bash":
            command = result.command
            if not command:
                msgs.append({"role": "user", "content": "Observation: Missing command."})
                continue

            allowed, reason = safety_check(command)
            if not allowed:
                observation = reason or "Blocked by safety check."
            elif not confirm_command(command):
                observation = "The command was denied, so I did not run it"
            else:
                observation = run_bash(command)

            if debug:
                print("\nObservation:")
                print(observation)

            msgs.append({"role": "user", "content": f"Observation: {observation}"})
            continue

        guidance = (
            "That response isn't valid. You need to use one of these formats:\n\n"
            "Thought: ...\nAction: bash\nCommand: ...\n\n"
            "or:\n\n"
            "Thought: ...\nFinal Answer: ...\n\n"
            "No markdown, no code blocks. Start with Thought."
        )
        if result.error:
            guidance += f"\nParser error: {result.error}"
        if debug:
            print("\nParser guidance:")
            print(guidance)
        msgs.append({"role": "user", "content": guidance})

    print(f"\nStopped after {MAX_STEPS} steps, no final answer.")


def main():
    print("Assignment 2 Part 1 Minimal ReAct Agent")
    print("Enter a task, or type 'exit' or 'quit' to stop.")

    # keep asking until the user exits
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
