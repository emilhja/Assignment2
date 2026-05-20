# Assignment 2 Part 2 Structured Tool Agent

This directory contains the Part 2 agent. It keeps the agent loop, recent
interactive context, tool execution, safety checks, and session logging in our
own code, but replaces the Part 1 text protocol with JSON structured replies
from the LLM.

The model must return exactly one JSON object each round:

```json
{"type":"tool_call","tool":"bash","args":{"command":"ls -la /workspace"},"reason":"inspect files"}
```

or:

```json
{"type":"final","answer":"Done"}
```

## Files

- `agent.py` - interactive CLI, multi-round loop, context handling, and tool dispatch
- `config/system_prompt.txt` - configurable system prompt and tool contract
- `parser.py` - JSON response validation
- `tools.py` - bounded bash tool, workspace file editors, and tool registry
- `safety.py` - forbidden intent checks, command blocklist, and manual bash approval
- `session_store.py` - SQLite session event log
- `llm_client.py` - OpenAI-compatible provider wrapper
- `workspace/` - dedicated file workspace
- `data/` - local session database storage
- `tests/` - parser, agent-loop, safety, tool, and storage tests

## Install

```powershell
cd assignment2_part2
python -m pip install -r requirements.txt
```

Create `.env` with one or more provider keys:

```env
LLM_PROVIDER_ORDER=groq,openai

GROQ_API_KEY=your_groq_key_here
GROQ_MODEL=llama-3.1-8b-instant

OPENAI_API_KEY=your_openai_key_here
OPENAI_MODEL=gpt-4o-mini
```

## Run

```powershell
python agent.py
```

Debug trace:

```powershell
$env:AGENT_DEBUG = "1"
python agent.py
```

Optional environment variables:

```env
AGENT_WORKSPACE=/workspace
AGENT_SESSION_DB=data/session_history.sqlite3
```

## Test

From the repository root:

```powershell
python -m pytest assignment2_part2
```

From inside `assignment2_part2`:

```powershell
python -m pytest
```

## Tools

`bash` runs one local Bash command through `bash -lc` after safety checks and
manual `y/N` approval. Output is truncated to 4000 characters and commands time
out after 10 seconds.

`edit_section` edits one file inside the configured workspace. It replaces one
exact `old_text` match with `new_text`, and refuses missing, repeated, or
outside-workspace edits.

`replace_text` edits one file inside the configured workspace. It replaces one
exact match by default, or every exact match when `all_occurrences` is true.
Repeated text is refused unless the caller explicitly asks for every match.

## Session History

The agent records session events in SQLite: user messages, raw assistant JSON,
tool observations, parser guidance, final answers, and stop events. This is
session logging, not multi-session resume. During one interactive run, the CLI
also gives the model a short rolling summary of recent user prompts and final
answers so follow-ups can resolve references like "it".

## Docker

```powershell
docker compose build
docker compose run --rm agent
```

The Compose setup mounts `./workspace` to `/workspace` for user-task files and
`./data` to `/data` for session history. It runs as a non-root user, starts bash
commands from the workspace, blocks Docker commands inside the agent, and blocks
commands that expose `.env`, `/data`, or process environment values.
