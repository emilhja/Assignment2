# Assignment 2 Part 1 Minimal ReAct Agent

This directory contains Part 1 of the assignment: a small ReAct-style
command-line agent. It sends a user task to an OpenAI-compatible chat model,
reads the model's raw text response, parses a homemade tool call,
runs one approved Bash command, sends the command output back as an
`Observation:`, and repeats until the model returns `Final Answer:`.

It does not use agent frameworks, built-in function calling, built-in tool
calling, structured outputs, schemas, or JSON mode.

## Files

- `agent.py` - interactive CLI and task-local ReAct loop
- `llm_client.py` - OpenAI-compatible provider wrapper
- `parser.py` - homemade parser for `Final Answer:` and `Action: bash`
- `safety.py` - small blocklist and `Run this command? [y/N]`
- `tools.py` - local Bash command runner
- `.env.example` - example Groq environment variables
- `Dockerfile` - minimal non-root container image
- `docker-compose.yml` - interactive Docker Compose runner
- `workspace/` - mounted workspace for container runs

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

When the model requests `Action: bash`, the Python code parses the command,
checks it, asks for confirmation, runs it through `run_bash()`, and appends the
result to the conversation as:

```text
Observation: ...
```

## Setup

Install dependencies:

```powershell
cd assignment2_part1
python -m pip install -r requirements.txt
```

Create `.env` from the example and add your Groq key, or switch to `local`:

```powershell
Copy-Item .env.example .env
```

```env
LLM_PROVIDER_ORDER=groq
GROQ_API_KEY=your_groq_key_here
GROQ_MODEL=llama-3.1-8b-instant
```

To use a local OpenAI-compatible server such as `llama-server`:

```env
LLM_PROVIDER_ORDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080
LOCAL_LLM_MODEL=local-model
```

`LOCAL_LLM_BASE_URL` may include `/v1`, but it does not have to. No real API
key is required for the local provider.

## Run Locally

```powershell
python agent.py
```

Enable internal tracing when you want to inspect raw responses, observations,
parser guidance, and step numbers:

```powershell
$env:AGENT_DEBUG = "1"
python agent.py
```

## Run With Docker

Build the image:

```powershell
docker compose build
```

Run the agent interactively:

```powershell
docker compose run --rm agent
```

Run the tests in Docker:

```powershell
docker compose run --rm agent python -m pytest
```

The Compose setup loads `.env`, runs the app from `/app`, and mounts only
`./workspace` into the container at `/workspace`. Ask the agent to use
`/workspace` when you want commands to operate on mounted files.

## Test

```powershell
python -m pytest
```

## Safety

Before executing a command, the agent prints the proposed command and asks:

```text
Run this command? [y/N]
```

It also blocks a small set of obviously unsafe command families, including
`rm`, `rmdir`, `sudo`, package managers, Docker commands from inside the
container, shutdown, reboot, and poweroff. This is a basic guard for Part 1,
not a complete sandbox.
