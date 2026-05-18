# Assignment 2 Part 1 Minimal ReAct Agent

This directory contains only Part 1 of the assignment: a minimal ReAct-style
command-line agent. It uses raw text from an OpenAI-compatible chat model,
detects simple homemade tool calls with local parsing, and runs shell commands
only through `run_bash()` after basic safety checks and user confirmation.
`run_bash()` executes approved commands via explicit Bash (`bash -lc`) rather
than through the platform default shell.

It does not use agent frameworks, OpenAI tool/function calling, structured
outputs, schemas, JSON mode, persistent memory, or Part 2/3 features.

## Files

- `agent.py` - interactive CLI and task-local ReAct loop
- `llm_client.py` - OpenAI-compatible chat completion wrapper with provider routing
- `parser.py` - homemade parser for `Final Answer:` and `Action: bash`
- `safety.py` - simple blocklist checks and `Run this command? [y/N]`
- `tools.py` - local shell command runner
- `.env.example` - example environment variables
- `Dockerfile` - minimal non-root container image
- `docker-compose.yml` - interactive Docker Compose runner
- `workspace/` - dedicated mounted workspace for container runs
- `requirements.txt` - Python dependency list

## Install

```powershell
cd assignment2_part1
python -m pip install -r requirements.txt
```

Create a local `.env` file from the example:

```powershell
Copy-Item .env.example .env
```

Then edit `.env` with your keys and preferred provider order:

```env
LLM_PROVIDER_ORDER=groq,openai

GROQ_API_KEY=your_groq_key_here
GROQ_MODEL=llama-3.1-8b-instant

OPENAI_API_KEY=your_openai_key_here
OPENAI_MODEL=gpt-4o-mini
```

`LLM_PROVIDER_ORDER` is a comma-separated priority list. The agent skips
providers whose API key is missing and falls back to the next provider if a
provider call fails. Use `LLM_PROVIDER_ORDER=openai,groq` to prefer OpenAI.

You can also set the same variables directly in your shell instead of using
`.env`.

## Run

```powershell
python agent.py
```

Internal ReAct tracing is hidden by default. Enable it when you want to inspect
raw model responses, observations, parser guidance, and step numbers:

```powershell
$env:AGENT_DEBUG = "1"
python agent.py
```

For Docker Compose runs, either add `AGENT_DEBUG=1` to `.env` or pass it for a
single run:

```powershell
docker compose run --rm -e AGENT_DEBUG=1 agent
```

## Test

```powershell
python -m pytest
```

In Docker, rebuild after dependency changes and then run pytest inside the
agent image:

```powershell
docker compose build
docker compose run --rm agent pytest
```

## Running in Docker

Create a local `.env` file from the example and put your API keys in it:

```powershell
Copy-Item .env.example .env
```

Then edit `.env` so `GROQ_API_KEY`, `OPENAI_API_KEY`, or both contain real
keys. The default `LLM_PROVIDER_ORDER=groq,openai` tries Groq first and OpenAI
second.

Build the container:

```powershell
docker compose build
```

Run the agent interactively:

```powershell
docker compose run --rm agent
```

The Compose setup mounts only `./workspace` from this project into the
container at `/workspace`. It does not mount your home directory, `~/.ssh`,
`~/.config`, or other secret locations. The agent itself runs from `/app`
because the current Python files expect that project directory layout. If you
want commands to operate on files in the mounted workspace, ask the agent to
use `/workspace` or run commands such as `cd /workspace && ls`.

The image creates and runs as a non-root user named `agentuser`. This reduces
the impact of mistakes inside the container, but it does not replace the
Python safety checks. The agent still checks commands against the blocklist,
asks for `y/N` confirmation before every Bash command, executes approved
commands with `bash -lc`, uses a subprocess timeout, and truncates tool output.
For local Windows runs, Git Bash, WSL, or another `bash` executable must be
available in `PATH`; the Docker setup remains the recommended environment for
demo and submission.

Optional direct Docker run command:

```bash
docker run --rm -it \
  --cpus="1.0" \
  --memory="512m" \
  --env-file .env \
  -v "$PWD/workspace:/workspace" \
  assignment2_part1-agent
```

Do not mount your whole home directory or broad filesystem paths into this
container. Keep file access limited to the dedicated `workspace/` folder.

### Docker Security Limitations

Docker is defense-in-depth, not a complete security boundary. The default
setup keeps network access enabled because the Python client must call the LLM
API provider. A stricter future design could separate the LLM client from
command execution and run the command-execution sandbox with `--network=none`.

The container can still run any command that passes the Python safety layer and
that you approve. Keep reviewing each proposed command before answering `y`.

## Model Protocol

The model must respond with exactly one of these raw text formats:

```text
Thought: ...
Action: bash
Command: ...
```

or:

```text
Thought: ...
Final Answer: ...
```

The agent parses each raw assistant response locally, then either runs the
proposed command after approval or ends the task with the final answer. Set
`AGENT_DEBUG=1` to print the internal raw responses, observations, parser
guidance, and step numbers.

## Example Interaction

Default output:

```text
HAL 9000 show the current directory

Proposed command:
pwd
Run this command? [y/N] y

Final answer:
/app
```

Debug output with `AGENT_DEBUG=1`:

```text
HAL 9000 show the current directory

--- Step 1 ---

Assistant raw response:
Thought: I need to inspect the current directory.
Action: bash
Command: pwd

Proposed command:
pwd
Run this command? [y/N] y

Observation:
C:\Users\emil_\vscode\Assignment2\assignment2_part1

--- Step 2 ---

Assistant raw response:
Thought: I have the current directory from the observation.
Final Answer: The current directory is C:\Users\emil_\vscode\Assignment2\assignment2_part1.

Final answer:
The current directory is C:\Users\emil_\vscode\Assignment2\assignment2_part1.
```

## Safety

Commands are checked before confirmation. The current basic blocklist rejects
obvious dangerous patterns such as `rm -rf /`, `rm -rf *`, `sudo`, `mkfs`,
`shutdown`, `reboot`, `poweroff`, Docker commands, recursive permission
changes, shell wrappers such as `bash -c` and `sh -c`, broad reads/searches
such as `cat *`, `cat **/*`, `find /`, and `grep -R /`, `curl ... | bash`,
`wget ... | bash`, and the classic fork bomb pattern.

If a command is blocked or denied, the command is not executed. The agent sends
an observation explaining what happened back to the model. The model is
instructed not to retry the same or similar command after a denial or safety
block.

## Limitations

- Parser is intentionally simple and protocol-specific. It requires a leading
  `Thought:` line and either a `Final Answer:` or an exact `Action: bash` plus
  `Command:` shape.
- Safety checks are basic and not a complete sandbox.
- Command execution uses explicit Bash (`bash -lc`) with a 10 second timeout.
- Output is truncated to 4000 characters.
- No file editing feature beyond shell commands the user approves.
- No persistent memory; each task starts with fresh task-local history.
- No token budgeting beyond a small max-step guard.
