# Part 1 Blueprint - Minimal ReAct Bash Agent

Sources:
- `dev_docs/assignment2_instructions.md`
- `dev_docs/assn2_grading_table_graderbot.md`

## Target

Build a small Python ReAct agent that calls an LLM, reads plain text model
responses, parses a homemade bash tool call from that raw text, executes the
chosen command through local Python code, feeds the output back as an
`Observation:`, and repeats until the model returns `Final Answer:` or a step
cap is reached.

Part 1 must stay deliberately simple. The point is to show the mechanics of an
agent loop from scratch, not to hide the loop behind framework or provider tool
features.

## Hard Requirements

The implementation must satisfy these gates before any Part 1 criterion matters:

- The submission is runnable and has a clear entry point.
- The code is real Python, not pseudocode.
- The LLM call is real and configurable through environment variables.
- The agent loop, parsing, and tool dispatch are local code.
- No IDE agent or coding agent product is wrapped as the submitted agent.

## Grading Criteria Mapping

| Criterion | Requirement | Concrete evidence to keep easy to grade |
|---|---|---|
| P1.1 ReAct loop | Reason -> Act -> Observe -> Repeat, with observations fed back into the next model call. | `agent.py` loop appends assistant response and `Observation:` messages, exits on final answer or max steps. |
| P1.2 Bash execution tool | The model chooses a command and the program executes that command. | `tools.py` exposes `run_bash(command)` and `agent.py` calls it only after parsing and safety checks. |
| P1.3 Homemade function-calling | Tool dispatch is hand-written; no agent framework and no built-in provider tool/function calling. | LLM calls pass plain chat messages only; no `tools=`, function calling, LangGraph, LangChain, CrewAI, or similar. |
| P1.4 Raw text parsing | The model emits plain text and the code parses marker lines. No JSON mode or structured output. | `parser.py` scans for `Thought:`, `Action: bash`, `Command:`, and `Final Answer:`. |
| P1.5 Destructive-command guard | Destructive commands are blocked or gated before execution. Prompt-only safety is not enough. | `safety.py` blocks unsafe command families and `agent.py` asks for confirmation before `run_bash`. |
| IR-2 Reproducible | Entry point and dependencies are documented. | `README.md`, `requirements.txt`, `.env.example`, Docker files. |
| IR-3 Real LLM calls | The agent calls an LLM API or local OpenAI-compatible model. | `llm_client.py` provider wrapper and documented env vars. |

## Architecture

Recommended module split:

- `agent.py`
  - CLI entry point.
  - Builds the system/user message list.
  - Runs the ReAct loop.
  - Calls the LLM client.
  - Parses the raw model response.
  - Dispatches only `Action: bash`.
  - Appends observations back into context.
  - Stops on `Final Answer:` or `MAX_STEPS`.
- `llm_client.py`
  - Thin OpenAI-compatible chat wrapper.
  - Reads provider, base URL, API key, and model from environment variables.
  - Returns only raw assistant text to the agent.
  - Does not expose built-in tools, function calling, JSON mode, or structured output.
- `parser.py`
  - Own string parser for the Part 1 protocol.
  - Accepts exactly:
    - `Thought: ...` plus `Action: bash` plus `Command: ...`
    - `Thought: ...` plus `Final Answer: ...`
  - Rejects missing `Thought:`, unknown actions, blank commands, both final and action in one response, and malformed command placement.
- `safety.py`
  - Refuses obvious destructive user intents before the LLM is called.
  - Blocks unsafe command families before execution.
  - Provides a confirmation gate before running allowed commands.
- `tools.py`
  - Implements `run_bash(command)`.
  - Uses bash as the only tool.
  - Captures stdout/stderr.
  - Applies timeout and output truncation.
  - Returns output text as the observation.

## Model Protocol

The system prompt should require exactly one of these raw text formats:

```text
Thought: <brief reason>
Action: bash
Command: <one safe local bash command>
```

or:

```text
Thought: <brief reason>
Final Answer: <answer to the user>
```

The prompt should also tell the model:

- use raw text only
- never use JSON
- never use Markdown code fences
- never claim local file or command state without using bash
- request only one command at a time
- use `Final Answer:` when the observation is enough
- avoid destructive, host-level, package-manager, Docker, shutdown, or reboot commands

These prompt rules help model behavior, but they do not replace code-level
safety checks.

## ReAct Control Flow

Expected loop:

1. Read a user task.
2. Refuse broad destructive user intents immediately.
3. Start messages with the system prompt and user task.
4. For each step up to `MAX_STEPS`:
   - Call the LLM with current messages.
   - Store the raw assistant response.
   - Parse it with the homemade parser.
   - If it is `Final Answer:`, print it and stop.
   - If it is `Action: bash`, extract `Command:`.
   - Check command safety before execution.
   - Ask the user to confirm the command.
   - Run the command only if it passes safety and confirmation.
   - Append `Observation: <tool output>` as the next user message.
   - Continue.
5. If the model never returns a final answer, stop after the cap and report that
   the cap was reached.

The important grading detail is that observations must influence later model
calls. A one-shot prompt-response program is not enough.

## Safety Design

Minimum guard:

- Block unsafe command names before execution, such as:
  - `rm`
  - `rmdir`
  - `sudo`
  - package managers such as `apt`, `apt-get`, `apk`, `dnf`, `yum`
  - `docker` and `docker-compose` inside the container
  - `shutdown`, `reboot`, `poweroff`
- Split simple chained commands enough to catch blocked command names after
  `;`, `|`, and `&`.
- Refuse broad destructive natural-language requests before the LLM call, such
  as "delete all files" or "delete the whole folder".
- Ask `Run this command? [y/N]` before executing any command that passes the
  blocklist.
- Execute commands only after both the safety check and confirmation pass.

This satisfies the rubric because the destructive-command guard takes effect in
code before `run_bash`.

## Bash Tool Behavior

`run_bash(command)` should:

- resolve `bash` from `PATH`
- run `[bash_path, "-lc", command]` with `shell=False`
- capture stdout and stderr
- return stderr if stdout is empty
- include non-zero exit code in the observation
- time out long-running commands
- truncate large output to a fixed maximum
- return a clear message if bash is missing

The model chooses the command. The student code should not hard-code a fixed
script and pretend it is an agent action.

## Tests To Keep

Parser tests:

- parses a valid bash action
- parses a valid final answer
- rejects missing `Thought:`
- rejects `Command:` without `Action: bash`
- rejects unsupported actions
- rejects action and final answer in the same response
- rejects empty or fully quoted command strings if the protocol requires raw command text

Safety tests:

- blocks `rm`, `rmdir`, `sudo`, package managers, Docker, shutdown/reboot
- catches blocked commands in simple chained or piped command strings
- allows safe read-only commands such as `pwd`, `ls`, `cat`
- refuses broad destructive user intents

Tool tests:

- runs a simple bash command
- confirms bash features work through `bash -lc`
- handles non-zero exit status
- handles timeout
- truncates long output
- reports missing bash cleanly

Agent-loop tests:

- mocked LLM first returns `Action: bash`, then returns `Final Answer:`
- observation from the command is appended before the second LLM call
- unsafe commands are blocked before `run_bash`
- denied confirmation produces an observation and does not execute
- invalid model format produces parser guidance and continues

## Demo Task For Substance Gate

Use a small but non-trivial task that proves observe-then-continue behavior.
Examples:

- "Inspect the workspace and tell me which Python files exist and what the
  largest one appears to be."
- "Find any README files in the workspace and summarize their first headings."
- "Check whether this directory has tests and tell me which test files are
  present."

A good demo transcript shows:

1. the model asks for a safe bash command,
2. the command is shown to the user for approval,
3. output becomes an `Observation:`,
4. the model uses that observation in a later answer.

Avoid a hello-world demo as the only evidence. The rubric's substance gate can
still fail a checkbox-complete agent if the demonstrated behavior is too thin.

## Submission Checklist

- `python agent.py` starts the CLI.
- `README.md` documents setup, `.env`, run command, Docker command, tests, and safety.
- `.env.example` contains placeholder provider config only, never real keys.
- `requirements.txt` contains only needed dependencies.
- Docker path is runnable if included.
- Tests run from repo root with:

```powershell
python -m pytest assignment2_part1 -q
```

- No Part 1 code uses:
  - built-in function calling
  - provider `tools=`
  - JSON mode
  - structured outputs
  - LangChain, LangGraph, LlamaIndex, CrewAI, or similar agent framework
  - IDE/coding-agent products as the agent

## Grader Evidence Notes

Before submission, collect or make easy to find:

- file location for the loop and observation append
- file location for `run_bash`
- file location for raw text parser
- file location proving no tool/function-call API is used
- file location for destructive-command block and confirmation gate
- one short transcript showing a real ReAct cycle
- test command output

The grading table requires concrete evidence. The easier these are to point to,
the less likely a real mechanism is marked ambiguous.
