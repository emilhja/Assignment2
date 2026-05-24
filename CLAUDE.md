# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

Three independent Python assignment parts, each a self-contained app with its own `requirements.txt`, `Dockerfile`, `docker-compose.yml`, and `tests/`:

- `assignment2_part1/` — minimal ReAct CLI agent. Homemade text protocol (`Thought:` / `Action: bash` / `Final Answer:`), one bash tool, manual `y/N` approval.
- `assignment2_part2/` — structured-JSON tool agent. Agent loop, JSON parser, safety allowlist+blocklist, SQLite session log, four tools (`bash`, `create_file`, `edit_section`, `replace_text`).
- `assignment2_part3/` — hub-connected multi-agent. **Imports Part 2** via `part2_bridge.py` (the only file that knows where Part 2 lives) — do not copy Part 2 logic. Adds Transport, Budget, ReplyPolicy, ClaimRegistry, ConsoleControl on top.

Top-level `plans/` holds planning notes. `AGENTS.md` has repo-wide conventions.

## Commands

Install + run a part (from its directory):

```bash
cd assignment2_partN
python -m pip install -r requirements.txt
python agent.py
```

Tests (from repo root):

```bash
python -m pytest assignment2_part1 -q
python -m pytest assignment2_part2 -q
python -m pytest assignment2_part3/tests -q
```

Single test: `python -m pytest assignment2_part3/tests/test_budget.py::test_name -q`

Enable parser/observation trace: `AGENT_DEBUG=1 python agent.py` (Parts 1 & 2).

## Part 3 multi-agent local hub

`tools/local_hub.py` is a mock of the TH25 RunPod hub. Use the 4-terminal layout:

```bash
cd assignment2_part3
docker compose up -d                                # hub + alice + bob
docker compose logs -f                              # T1
docker attach assignment2_part3-agent-alice-1      # T2 (Ctrl-P Ctrl-Q to detach — NOT Ctrl-C)
docker attach assignment2_part3-agent-bob-1        # T3
python tools/chat.py live --as emil-user            # T4
```

`.env` needs `LOCAL_HUB_PASSWORD` and `RUNPOD_CHAT_PASSWORD` (same value). Compose refuses to start without them.

Cross-agent audit (read-only, doesn't need running agents):

```bash
python tools/audit.py traces -n 10
python tools/audit.py trace <trace_id>
python tools/audit.py tail --agent alice --kind tool
```

Useful event kinds: `claim_observed`, `claim_block`, `peer_refusal`, `peer_refusal_tool_args`, `budget_exceeded`, `tool`, `raw_json`.

## Docker rebuild discipline

`Dockerfile` does `COPY . .`; only `./workspace` (and Part 3's `./data`) are bind-mounted. **Edits to `agent.py`, `parser.py`, `tools.py`, etc. are not picked up by an already-built image.** After code changes:

```bash
docker compose build agent      # or build --no-cache before a demo
docker compose run --rm agent
```

If tests in the container show stale results, suspect a stale image first.

## Architecture — Part 2 (foundation)

The agent loop in `agent.py` drives one round at a time:

1. `llm_client.complete_chat` sends history → raw JSON text
2. `parser.parse_response` validates → either `{"type":"tool_call",...}` or `{"type":"final",...}`
3. `safety.intent_refusal` checks user intent; `safety.safety_check` gates bash commands (allowlist of read-only tokens + blocklist regexes + path-argument check)
4. `tools.run_tool` dispatches; bash runs through `bash --noprofile --norc -c` with a minimal env (no API keys inherited), 10s timeout, 4000-char output cap
5. Observation appended to history; loop continues
6. `session_store` (SQLite) logs every event

After any successful `create_file` / `edit_section` / `replace_text`, the runtime auto-runs the Part 2 pytest suite before the final answer.

System prompt is **configurable** at `config/system_prompt.txt`.

## Architecture — Part 3 (multi-agent on top of Part 2)

Per-agent process; identity = `AGENT_ID` + `AGENT_DISPLAY_NAME`; workspace isolated to `workspace/<AGENT_ID>/`.

Main loop (`group_chat.run_group_chat`): `recv → should_reply → run_peer_task → scrub → send`.

Key invariants:

- **Hub-only inter-agent text.** Everything between agents goes through `transport.Transport.send`. The local console is operator-only.
- **Per-round peer refusal** (`peer.peer_intent_refusal`): leak-attempt prompts (system prompt, `.env`, API keys, `/data`, source files, history) are refused **before** any LLM call. The same check re-runs on the model's tool args, so leaks that survive the model are still caught at the wire.
- **Outbound credential scrubber** (`peer.scrub_outbound`): OpenRouter / Anthropic / GitHub / Slack / AWS / JWT / dotenv-shaped strings → `[REDACTED:<kind>]`. Applies to LLM replies AND operator `:say`.
- **N×M reply gate** (`reply_policy.should_reply`, pure function, no LLM cost): self-msgs skip; direct mentions or `assigned: alice` handoff answer; per-thread cooldown; broadcasts (`everyone` / `anyone` / Swedish `alla` / `någon` / …) capped at 1 reply per 300 s window.
- **Budget** (`budget.Budget`): sliding 60 s window enforces TPM + RPM + lifetime token cap. Over-cap → `BudgetExceeded`; operator can `:approve` exactly one over-cap call. State persisted to `data/budget_<AGENT_ID>.json` (pause/resume survives restart).
- **Claim/defer for shared writes** (`claims.ClaimRegistry` + `peer_task._maybe_claim_block`): chat messages with `CLAIM /workspace/shared/<path>: ...` reserve that path; write tools refuse `refused: deferred: ...` for peers; `RELEASE` clears; 5-minute expiry.
- **Coordinator hints** (`coordination.py`): parses common assignment wording (e.g. "alice writes add+subtract, bob writes multiply") and injects per-agent runtime guidance before the LLM call so each agent only claims its own scope.
- **Trace IDs**: every event during a peer turn is tagged with `trace_id = inbound message.id`. Same id appears in every agent's SQLite log, so `tools/audit.py trace <id>` replays one hub interaction across all agents.

Console (`console_control.py`, daemon stdin thread, never reaches LLM):

`:budget` `:limit tpm|rpm|total <N>` `:pause`/`:resume` `:approve`/`:deny` `:say <text>` `:stop` `:help`

## Conventions

- Python 3, 4-space indent, lowercase snake_case modules, `test_*.py`.
- Keep policy logic as **pure functions** (e.g. `reply_policy.should_reply`); isolate env/path setup (e.g. `part2_bridge.py`, the env-setting top of `agent.py`).
- Commits: `type(scope): summary` (`feat(part3): ...`, `fix(tools): ...`). Imperative subject.
- Tests must be deterministic and avoid real provider calls — mock or use local stubs.
- Part 3 changes often regress Part 2; run both suites.
- Never commit `.env`, API keys, `data/*.sqlite3`, or generated `workspace/` contents.
- Don't weaken the safety stack (allowlist, blocklist, credential scrubber, workspace confinement, claim gate) unless the task explicitly calls for it.
