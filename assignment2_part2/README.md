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
- `tools.py` - bounded bash tool, workspace file creation/editors, and tool registry
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

Create `.env` with one or more hosted provider keys:

```env
LLM_PROVIDER_ORDER=groq,openrouter

GROQ_API_KEY=your_groq_key_here
GROQ_MODEL=llama-3.1-8b-instant

OPENROUTER_API_KEY=your_openrouter_key_here
OPENROUTER_MODEL=openai/gpt-4o-mini
```

To use a local OpenAI-compatible server such as `llama-server`:

```env
LLM_PROVIDER_ORDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080
LOCAL_LLM_MODEL=local-model
```

`LOCAL_LLM_BASE_URL` may include `/v1`, but it does not have to. No real API
key is required for the local provider; `LOCAL_LLM_API_KEY` is only available
for servers that explicitly require one.

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

`bash` runs one local Bash command through `bash --noprofile --norc -c` with a
minimal subprocess environment (no API keys, no provider secrets) after the
allowlist + blocklist safety check and manual `y/N` approval. Output is
truncated to 4000 characters and commands time out after 10 seconds.

`create_file` creates one file inside the configured workspace without shell
redirection. It refuses outside-workspace paths and existing files unless
`overwrite` is explicitly true.

`edit_section` edits one file inside the configured workspace. It replaces one
exact whole-line `old_text` section with `new_text`, including indentation, and
refuses missing, repeated, partial-line, or outside-workspace edits.

`replace_text` edits one file inside the configured workspace. It replaces one
exact whole-line match by default, or every exact whole-line match when
`all_occurrences` is true. Repeated text is refused unless the caller
explicitly asks for every match.

After a successful `create_file`, `edit_section`, or `replace_text` call, the agent runtime
automatically runs the full Part 2 pytest suite before returning a final
answer. From the repository root it uses `python -m pytest assignment2_part2 -q`;
from the packaged Part 2 app root it uses `python -m pytest -q`.

## Safety

The bash tool passes commands through two layers in `safety.py`:

1. **Allowlist (default-deny):** the first token of every `; | &` segment must
   be one of a small set of read-only commands (`ls`, `cat`, `grep`, `head`,
   `tail`, `wc`, `find`, `pwd`, `echo`, `printf`, `sort`, `uniq`, `cut`,
   `true`, `false`). Anything else — including `curl`, `wget`, `nc`,
   `python`, `node` — is rejected before the blocklist runs.
2. **Blocklist:** regex patterns reject command substitution (`$(...)`,
   backticks), process substitution (`<(...)`, `>(...)`), shell redirection
   (`>`, `>>`, `2>`), in-place `sed -i`, recursive permission changes, and
   references to `.env`, `/data`, `/proc/self/environ`, and credential
   environment variables.
3. **Path argument check:** read commands that accept file paths reject
   wildcard paths, `..`, and absolute paths outside `/workspace`. `find -exec`
   and related execution modes are also rejected.

The bash subprocess receives an explicit minimal environment (`PATH`, `HOME`,
`LANG`, `LC_ALL`, `TERM`, `PWD`) — provider API keys are not inherited.

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
`./data` to `/data` for session history. It runs as a non-root user with a
read-only root filesystem (`/tmp` is a 64 MB tmpfs), drops all Linux
capabilities, blocks privilege escalation, and limits the container to 100
PIDs and 512 MB RAM. Inside the agent the bash allowlist plus blocklist still
gate every command.
