# Part 1 Architecture Blueprint

## Purpose

Part 1 is a minimal ReAct-style command-line agent. It demonstrates how an
agent can reason, choose a bash action, observe command output, and then answer
the user without relying on an agent framework or provider-side tool calling.

The implementation is deliberately simple so the full path is visible in the
code:

```text
user task -> agent loop -> LLM call -> raw model text -> local parser
          -> safety checks -> bash tool -> observation -> LLM call -> final answer
```

## Design Goals

- Keep the agent loop readable in one file.
- Use a homemade text protocol instead of JSON schemas or function calling.
- Let the model choose commands, but let local code decide whether commands may
  run.
- Treat command output as factual observation and send it back into the next
  model turn.
- Keep execution bounded with command timeouts, output truncation, and a maximum
  ReAct step count.

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `agent.py` | Owns the CLI, system prompt, message history, ReAct loop, parser dispatch, safety calls, observation append, and final printing. |
| `llm_client.py` | Builds an OpenAI-compatible client for Groq or a local model and returns raw assistant text. |
| `protocol.py` | Parses the raw text protocol and returns a `ParsedResponse` object with `kind`, `action`, `command`, `answer`, or `error`. |
| `safety.py` | Refuses broad destructive user requests, blocks unsafe command families, and asks the user to confirm allowed commands. |
| `bash_tool.py` | Runs one bash command through `bash -lc`, captures output, handles errors, times out long commands, and truncates large output. |

## ReAct Loop

The loop lives in `agent.py` inside `run_task(user_task)`.

1. The user's natural-language task is checked by
   `safety.refuse_user_intent`.
2. If the task is not refused, `agent.py` creates a message list containing:
   - the `SYSTEM_PROMPT`
   - the user task
3. For each step up to `MAX_STEPS`:
   - `llm_client.complete_chat(messages)` sends the current conversation to the
     configured LLM.
   - The raw assistant text is appended to `messages`.
   - `protocol.parse_model_response(raw_response)` parses the text.
   - A parsed final answer is printed and the loop stops.
   - A parsed bash action is safety checked, confirmed by the user, executed,
     and returned to the LLM as `Observation: <output>`.
   - An invalid response produces protocol guidance, which is appended as the
     next user message so the model can retry.
4. If no final answer appears after the step cap, the agent stops with a clear
   message.

## Model Protocol

The model must answer in exactly one of two raw text formats.

For a tool action:

```text
Thought: <brief reason>
Action: bash
Command: <one safe local bash command>
```

For a final response:

```text
Thought: <brief reason>
Final Answer: <answer to the user>
```

`protocol.py` rejects responses that are empty, missing `Thought:`, use JSON,
use Markdown code fences, include both `Action:` and `Final Answer:`, request an
unsupported action, omit `Command:`, or put `Command:` before `Action:`.

## LLM Client

`llm_client.py` uses the `openai` Python package against OpenAI-compatible
endpoints. It supports two providers:

- `groq`, using `GROQ_API_KEY` and `GROQ_MODEL`
- `local`, using `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_API_KEY`, and
  `LOCAL_LLM_MODEL`

The provider order is controlled by `LLM_PROVIDER_ORDER`. The client sends only
plain chat messages. It does not pass `tools=`, function definitions, JSON mode,
or structured output configuration.

## Bash Tool

`bash_tool.run_bash(command)` is the only execution tool. It:

- resolves `bash` from `PATH`
- runs `[bash_path, "-lc", command]` with `shell=False`
- captures stdout and stderr
- reports non-zero exit codes in the observation
- times out after `COMMAND_TIMEOUT_SECONDS`
- truncates output after `MAX_OUTPUT_CHARS`
- returns a clear message if bash is unavailable

This keeps shell execution local and explicit.

## Safety Gates

Part 1 has three layers of safety:

1. `refuse_user_intent(user_task)` blocks broad destructive requests before any
   model call, such as "delete everything" or "remove all files".
2. `check_command(command)` blocks unsafe command families before confirmation
   and before execution, including `rm`, `rmdir`, `sudo`, Docker commands,
   package managers, shutdown, reboot, and poweroff.
3. `confirm_command(command)` prints the proposed command and requires the user
   to answer `y` or `yes` before the command runs.

These checks are code-level guards. The system prompt also tells the model to
avoid destructive commands, but prompt instructions are not treated as the only
safety mechanism.

## Test Coverage

The tests verify the important agent mechanics:

- `tests/test_protocol.py` checks valid and invalid raw text parsing.
- `tests/test_safety.py` checks destructive intent refusal and blocked command
  families.
- `tests/test_bash_tool.py` checks command execution, failures, timeout,
  truncation, and missing bash handling.
- `tests/test_agent.py` checks final-answer handling, observation feedback,
  invalid-response retry guidance, command denial, blocked commands, debug
  traces, and `MAX_STEPS`.

Run from the repository root:

```powershell
python -m pytest part1 -q
```

## Rubric Evidence

| Requirement | Evidence |
|---|---|
| ReAct loop | `agent.py` appends observations and repeats until `Final Answer:` or `MAX_STEPS`. |
| Bash tool | `bash_tool.py` executes the parsed command through bash. |
| Homemade parsing | `protocol.py` parses raw marker lines from text. |
| No provider tool calling | `llm_client.py` sends only normal chat messages. |
| Destructive-command guard | `safety.py` blocks intents and command families; `agent.py` checks before execution. |
| Human approval | `safety.confirm_command` asks before running any allowed command. |
| Reproducibility | `README.md`, `requirements.txt`, Docker files, and tests document setup and verification. |
