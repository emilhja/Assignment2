# Assignment 2 Part 1 ReAct Bash Agent

This is a small Part 1 ReAct agent. It calls an OpenAI-compatible chat model,
asks for raw text, parses a homemade bash action, runs approved commands, feeds
the command output back as an `Observation:`, and repeats until the model gives
a `Final Answer:`.

It does not use agent frameworks, built-in function calling, built-in tool
calling, structured outputs, JSON mode, or schema parsing.

## Files

- `agent.py` - CLI entry point and ReAct loop
- `protocol.py` - raw text parser for `Action: bash` and `Final Answer:`
- `safety.py` - destructive-command guard and confirmation prompt
- `bash_tool.py` - local bash runner
- `llm_client.py` - Groq/local OpenAI-compatible chat client
- `tests/` - focused pytest coverage

## Protocol

The model must reply with exactly one of these raw text formats:

```text
Thought: <brief reason>
Action: bash
Command: <one safe local bash command>
```

or:

```text
Thought: <brief reason>
Final Answer: <answer>
```

The agent parses this text itself. If the model requests bash, the command is
checked and confirmed before execution. The result is appended to the next model
turn as:

```text
Observation: <command output>
```

## Setup

```powershell
cd part1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set a Groq key:

```env
LLM_PROVIDER_ORDER=groq
GROQ_API_KEY=your_key_here
GROQ_MODEL=llama-3.1-8b-instant
```

Or use a local OpenAI-compatible server:

```env
LLM_PROVIDER_ORDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080
LOCAL_LLM_MODEL=local-model
```

## Run

```powershell
python agent.py
```

Enable trace output:

```powershell
$env:AGENT_DEBUG = "1"
python agent.py
```

## Docker

```powershell
docker compose build
docker compose run --rm agent
```

The container runs as a non-root user, mounts `./workspace` at `/workspace`,
and starts the agent there so plain commands such as `ls` and `python file.py`
operate on the mounted workspace by default.

## Test

```powershell
python -m pytest part1 -q
```

## Safety

Before a command runs, the agent asks:

```text
Run this command? [y/N]
```

It also blocks command families such as `rm`, `rmdir`, `sudo`, package
managers, Docker, shutdown, reboot, and poweroff before confirmation and before
execution. This is a small assignment guard, not a complete security sandbox.
