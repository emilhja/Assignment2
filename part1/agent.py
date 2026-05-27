import os

try:
    from .bash_tool import run_bash
    from .llm_client import complete_chat
    from .protocol import format_protocol_guidance, parse_model_response
    from .safety import check_command, confirm_command, refuse_user_intent
except ImportError:  # Allows `python agent.py` from inside this folder.
    from bash_tool import run_bash
    from llm_client import complete_chat
    from protocol import format_protocol_guidance, parse_model_response
    from safety import check_command, confirm_command, refuse_user_intent


MAX_STEPS = 20

SYSTEM_PROMPT = """You are a small ReAct software assistant for local file and shell tasks.

Respond with raw text only. Use exactly one of these formats:

Thought: <brief reason>
Action: bash
Command: <one safe local bash command>

or:

Thought: <brief reason>
Final Answer: <answer to the user>

Rules:
- Use Action: bash only when you need local command output.
- Request one command at a time.
- If the user asks to create and run a script, first create the file, then run
  it with the right interpreter, such as `python hello_world.py`.
- If a file creation command returns no error, treat it as successful and do
  not repeat the same creation command.
- Do not rewrite or transform a file unless the user asked for that change or
  the previous command failed.
- Do not use JSON, markdown code fences, schemas, or tool/function calling syntax.
- Treat Observation messages as factual command output.
- If an Observation answers the task, stop with Final Answer.
- Prefer narrow read-only commands: pwd, ls, cat, head, sed, grep, find, and wc.
- Do not request destructive commands, sudo, package managers, Docker, shutdown, or reboot.
- A standalone cd does not persist. Use `cd path && command` when needed.
"""


def _debug_enabled() -> bool:
    return os.getenv("AGENT_DEBUG", "").strip() == "1"


def run_task(user_task: str) -> None:
    refusal = refuse_user_intent(user_task)
    if refusal:
        print("\nFinal answer:")
        print(f"I cannot do that. {refusal}")
        return

    debug = _debug_enabled()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_task},
    ]

    for step in range(1, MAX_STEPS + 1):
        if debug:
            print(f"\n--- Step {step} ---")

        raw_response = complete_chat(messages)
        messages.append({"role": "assistant", "content": raw_response})

        if debug:
            print("\nAssistant raw response:")
            print(raw_response)

        parsed = parse_model_response(raw_response)
        if parsed.kind == "final":
            print("\nFinal answer:")
            print(parsed.answer)
            return

        if parsed.kind == "action" and parsed.action == "bash":
            command = parsed.command or ""
            allowed, reason = check_command(command)
            if not allowed:
                print("\nFinal answer:")
                print(f"I cannot run that command. {reason}")
                return

            if not confirm_command(command):
                observation = "The user denied the command, so it was not run."
            else:
                observation = run_bash(command)

            if debug:
                print("\nObservation:")
                print(observation)
            messages.append({"role": "user", "content": f"Observation: {observation}"})
            continue

        guidance = format_protocol_guidance(parsed.error)
        if debug:
            print("\nParser guidance:")
            print(guidance)
        messages.append({"role": "user", "content": guidance})

    print(f"\nStopped after {MAX_STEPS} steps without a final answer.")


def main() -> None:
    print("Assignment 2 Part 1 ReAct Bash Agent")
    print("Enter a task, or type 'exit' or 'quit' to stop.")

    while True:
        try:
            user_task = input("\nTask > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return

        if not user_task:
            continue
        if user_task.lower() in {"exit", "quit"}:
            print("Goodbye.")
            return
        run_task(user_task)


if __name__ == "__main__":
    main()
