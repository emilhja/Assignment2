# Part 2 Architecture Blueprint

## Purpose

Part 2 is a structured-JSON tool agent. Where Part 1 used a homemade text
protocol (`Thought:` / `Action: bash`), Part 2 upgrades the contract to **one
JSON object per model turn** and grows the single bash tool into a small
toolbox of file-editing operations. It keeps the same core idea — the model
proposes, local code decides — but adds a JSON parser, a layered safety stack
(allowlist + blocklist + path checks), a SQLite session log, and an automatic
post-edit test run.

The full path is still visible in the code:

```text
user task -> intent refusal -> agent loop -> LLM call (JSON mode)
          -> JSON parser -> tool_call | final
          -> safety checks -> tool dispatch -> observation
          -> (on write) auto pytest -> LLM call -> final answer
```

## Design Goals

- Replace freeform text with a **strict JSON contract** the model must satisfy
  every turn; reject anything off-contract and feed the error back.
- Keep bash **default-deny**: only read-only command families run, and only
  after an explicit human `y/N`.
- Confine all file work to `./workspace`; reject paths that escape it.
- Make edits self-verifying: after any successful file mutation, run the test
  suite before the agent is allowed to declare success.
- Make every turn **observable** by logging it to SQLite.
- Keep policy logic in small, testable functions; isolate provider/env setup.

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `agent.py` | Owns the CLI, message history, the bounded agent loop, parser dispatch, tool-call execution, the post-edit test run, and final printing. |
| `parser.py` | Validates the raw model text as JSON and returns a `ParsedResponse` with `kind` (`tool_call` / `final` / `invalid`), plus `tool`/`args`/`answer`/`error`. |
| `safety.py` | Refuses destructive user intent, allowlists bash command tokens, blocklists dangerous patterns, checks path arguments, and confirms commands with the user. |
| `tools.py` | Defines the `ToolSpec` registry and handlers for the eight tools; runs bash through a minimal env with a timeout and output cap. |
| `runtime_helpers.py` | Small pure helpers: observation formatting, truncation, parser guidance, and the "did this tool mutate + succeed" predicates that trigger the auto-test. |
| `session_store.py` | SQLite `events` log; one row per recorded turn. |
| `llm_client.py` | Builds an OpenAI-compatible client for Groq / OpenRouter / local, requests JSON mode (with fallback), and retries on rate limits. |
| `colors.py` | Optional ANSI styling for terminal output (TTY/`NO_COLOR` aware). |
| `config/system_prompt.txt` | The externalized system prompt describing the JSON contract and the tools. |

## Agent Loop

The loop lives in `agent.py` inside `run_task(user_task)`.

1. The user task is checked by `safety.intent_refusal`. A refusal finalizes
   immediately, before any model call.
2. `agent.py` builds the message list: the system prompt (from
   `config/system_prompt.txt`) plus the user task. Prior context is trimmed to
   `MAX_CONTEXT_TURNS` turns and `MAX_CONTEXT_CHARS` characters.
3. For each step up to `MAX_STEPS` (8):
   - `llm_client.complete_chat(messages)` returns raw assistant JSON text, which
     is appended to history and recorded to the session store as `raw_json`.
   - `parser.parse_response(raw)` validates it.
   - A `final` result is printed and the loop stops.
   - A `tool_call` result is dispatched via `_run_tool_call`. The observation
     (truncated to `MAX_OUTPUT_CHARS`) is appended with
     `runtime_helpers.tool_observation_message` and recorded.
   - An `invalid` result yields `runtime_helpers.invalid_response_guidance`,
     appended as the next message so the model can retry on-contract.
4. After any tool call that both mutates the workspace and succeeds
   (`runtime_helpers.tool_succeeded`), `_run_post_edit_tests` runs the suite. If
   tests fail, the agent finalizes with the failure rather than continuing.
5. If no final answer appears within `MAX_STEPS`, the agent stops with a clear
   message.

## JSON Protocol

The model must return exactly one JSON object. `parser.parse_response`
recognizes two shapes.

Tool call:

```json
{"type": "tool_call", "tool": "create_file", "args": {"path": "...", "content": "..."}, "reason": "optional"}
```

Final answer:

```json
{"type": "final", "answer": "answer to the user"}
```

`parser.parse_response` returns an `invalid` `ParsedResponse` when the text is
not JSON, is not a JSON object, omits required fields, uses an unknown tool
name, mixes the two shapes (e.g. an `answer` on a tool call), or has the wrong
type for a field (e.g. a non-string `answer`). The `error` is surfaced back to
the model as guidance.

## Tools

`tools.py` registers eight tools in `TOOL_REGISTRY`, each a `ToolSpec` with a
`name`, `description`, required args, `handler`, and the policy flags
`mutates_workspace`, `requires_approval`, and `success_prefixes` (used to detect
success in the observation).

| Tool | Key args | Mutates | Approval | Behavior |
|---|---|---|---|---|
| `bash` | `command` | no | yes | Runs one read-only command after the safety stack and a `y/N`. Minimal env, `COMMAND_TIMEOUT_SECONDS` timeout, `MAX_OUTPUT_CHARS` cap. |
| `read_file` | `path` | no | no | Read a workspace file as UTF-8. |
| `create_file` | `path`, `content`, `overwrite` | yes | no | Write a workspace file; create parent dirs; block overwrite unless `overwrite=true`. |
| `append_text` | `path`, `content` | yes | no | Append to a workspace file. |
| `edit_section` | `path`, `old_text`, `new_text` | yes | no | Replace one whole-line exact match; reject if missing or duplicated. |
| `replace_text` | `path`, `old_text`, `new_text`, `all_occurrences` | yes | no | Replace first match, or all if `all_occurrences=true`; whole-line matching. |
| `rename_file` | `source_path`, `target_path`, `overwrite` | yes | no | Rename within the workspace; cross-workspace targets blocked. |
| `run_tests` | (path) | no | no | Invoke pytest against a workspace path. |

The four core editing tools are `bash`, `create_file`, `edit_section`, and
`replace_text`. `edit_section` and `replace_text` require their `old_text` to
span complete lines, which prevents accidental mid-line corruption.

## Safety Stack

Bash is gated by four code-level layers plus a human confirmation. The system
prompt also discourages destructive commands, but prompt text is never the only
guard.

1. **Intent refusal** — `safety.intent_refusal(user_task)` blocks broad
   destructive or container-management requests (e.g. "delete everything",
   "remove all files", docker usage) before any model call.
2. **Allowlist tokens** — `safety.command_allowlist_check` permits only the
   first token of each shell segment when it is in `ALLOWED_COMMANDS` (read-only
   tools such as `ls`, `cat`, `grep`, `head`, `tail`, `wc`, `find`, `pwd`,
   `echo`, `sort`, `uniq`, `cut`). Everything else is denied.
3. **Blocklist regexes** — `safety.safety_check` blocks dangerous patterns even
   if the leading token looks benign: env/credential exposure (`env`,
   `printenv`, `$GROQ_API_KEY`, `os.environ`), destructive flags (`rm`,
   `sed -i`, `find -delete`, `chmod -R`), container/package/service tools, command
   substitution and process substitution, redirection and heredocs, and
   pipe-to-shell patterns.
4. **Path-argument checks** — for commands in `PATH_ARGUMENT_COMMANDS`,
   `safety._command_argument_check` rejects wildcards, `..`, and any path that
   does not stay inside `/workspace`.

`safety.is_command_safe` composes the allowlist and blocklist; only after it
passes does `safety.confirm_command` print the proposed command and require a
`y`/`yes`.

## Auto-Run Tests After Write

`runtime_helpers.workspace_mutation_tools()` returns the frozenset of tools with
`mutates_workspace=True`; `runtime_helpers.tool_succeeded(tool, observation)`
checks the observation against that tool's `success_prefixes`. When both hold,
`agent._run_post_edit_tests` runs `POST_EDIT_TEST_COMMAND`
(`python -m pytest assignment2_part2 -q`) with a `POST_EDIT_TEST_TIMEOUT_SECONDS`
(120s) timeout. The captured result is appended as an observation; a non-zero
exit finalizes the task with the failure so the agent cannot claim success over
a broken edit.

## LLM Client

`llm_client.py` uses the `openai` package against OpenAI-compatible endpoints
and supports three providers, tried in `LLM_PROVIDER_ORDER`:

- `groq` (`GROQ_API_KEY`, `GROQ_MODEL`)
- `openrouter` (`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`)
- `local` (`LOCAL_LLM_BASE_URL`, no key required)

It requests `response_format={"type":"json_object"}` to keep replies on-contract
and falls back to a plain request if a provider rejects JSON mode, attempting to
recover a JSON object from the error body. Rate limits are retried with backoff
derived from the `Retry-After` header or the error text, bounded by a max single
wait plus jitter. Request timeout and max output tokens are configurable.

## Session Store

`session_store.py` opens a SQLite file and creates one table:

```text
events(id INTEGER PK, created_at TEXT, role TEXT, kind TEXT, content TEXT)
```

`SessionStore.record(role, kind, content)` inserts one row with an ISO UTC
timestamp. The loop records the user task (`message`), each raw model reply
(`raw_json`), each tool call with its args and observation (`kind` = tool name),
parser guidance, the final answer, and any runtime errors — giving a replayable
transcript of the session.

## Test Coverage

| Test file | Verifies |
|---|---|
| `tests/test_parser.py` | Valid `tool_call` / `final` shapes; rejection of malformed JSON, non-objects, unknown tools, missing args, and mixed shapes. |
| `tests/test_safety.py` | Allowlist enforcement, blocklist patterns, intent refusal, and path checks (`..`, wildcards, outside `/workspace`). |
| `tests/test_tools.py` | Bash timeout/truncation/env-stripping; whole-line matching for `edit_section`/`replace_text`; workspace containment and overwrite rules; pytest invocation. |
| `tests/test_agent.py` | Intent refusal short-circuit, invalid-response retry loop, multi-round tool calls, edit→auto-test flow, test-failure halt, and noise filtering. |
| `tests/test_llm_client.py` | JSON-mode negotiation and fallback, provider failover order, and rate-limit backoff. |
| `tests/test_session_store.py` | `events` table creation and row insertion. |

Run from the repository root:

```bash
python -m pytest assignment2_part2 -q
```

Enable a parser/observation trace with `AGENT_DEBUG=1 python agent.py`.

## Key Constants

| Constant | Value | File | Purpose |
|---|---|---|---|
| `MAX_STEPS` | 8 | `agent.py` | Max loop iterations per task. |
| `MAX_CONTEXT_TURNS` | 4 | `agent.py` | Prior turns retained in context. |
| `MAX_CONTEXT_CHARS` | 2000 | `agent.py` | Char budget for prior context. |
| `POST_EDIT_TEST_COMMAND` | `python -m pytest assignment2_part2 -q` | `agent.py` | Auto-test command. |
| `POST_EDIT_TEST_TIMEOUT_SECONDS` | 120 | `agent.py` | Auto-test timeout. |
| `MAX_OUTPUT_CHARS` | 4000 | `tools.py` | Tool-output truncation cap. |
| `COMMAND_TIMEOUT_SECONDS` | 10 | `tools.py` | Bash command timeout. |
| `PYTEST_TIMEOUT_SECONDS` | 30 | `tools.py` | `run_tests` tool timeout. |

## Rubric Evidence

| Requirement | Evidence |
|---|---|
| Structured tool protocol | `parser.parse_response` enforces one JSON object per turn with `tool_call`/`final` shapes. |
| Multiple tools | `tools.TOOL_REGISTRY` exposes eight tools incl. four file editors. |
| Homemade parsing / no provider tool calling | `parser.py` validates JSON locally; `llm_client.py` only sets JSON mode, never `tools=`. |
| Default-deny bash | `safety.py` allowlist + blocklist + path checks gate every command. |
| Human approval | `safety.confirm_command` requires `y/N` before any bash run. |
| Workspace confinement | Tool handlers and path checks keep all file work inside `./workspace`. |
| Self-verifying edits | `agent._run_post_edit_tests` runs pytest after each successful mutation. |
| Observability | `session_store.py` logs every turn to SQLite. |
| Reproducibility | `README.md`, `requirements.txt`, Docker files, and `tests/` document setup and verification. |
