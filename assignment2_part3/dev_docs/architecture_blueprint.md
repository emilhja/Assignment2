# Part 3 Architecture Blueprint

## Purpose

Part 3 is a hub-connected multi-agent system. It does **not** reimplement the
agent — it imports Part 2 (the loop, parser, tools, safety stack, session store)
through a single shim, `part2_bridge.py`, and layers coordination on top:
inter-agent transport, a token/rate budget, a reply-decision gate, a shared-write
claim registry, and an operator console. Each agent is its own process with its
own identity and workspace; everything agents say to each other goes through the
hub.

The per-agent path looks like:

```text
recv (hub) -> message assembly -> should_reply gate
           -> coordination hints -> run_peer_task (Part 2 loop)
           -> peer intent refusal -> budget gate -> claim gate
           -> scrub_outbound -> send (hub)
           -> claim / task-status continuations
```

A separate daemon thread reads operator console commands the entire time and
never reaches the LLM.

## Design Goals

- **Hub-only inter-agent text.** The local console is operator-only; all
  agent-to-agent communication crosses `transport.Transport.send`.
- **Treat peers as untrusted.** Leak-attempt prompts are refused per round
  before any LLM call, and again on the model's tool args; outbound text is
  scrubbed for credentials.
- **Cheap reply decisions.** `reply_policy.should_reply` is a pure function that
  gates replies (mentions, handoffs, cooldown, broadcast cap) without spending a
  token.
- **Bounded spend.** A sliding-window budget enforces per-minute and lifetime
  caps and survives restarts.
- **Safe shared writes.** A claim/defer protocol serializes writes to
  `/workspace/shared/...` with a deterministic tie-break.
- **Replayable across agents.** Every event in a turn is tagged with a trace id
  equal to the inbound message id.
- **Human in the loop.** An operator console can inspect budget, change limits,
  pause/approve, speak, and stop.

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `agent.py` | Entry point; `_bootstrap_env` pins identity/workspace before Part 2 imports, then starts the group chat. |
| `part2_bridge.py` | The only file that knows where Part 2 lives; injects it into `sys.path`. |
| `group_chat.py` | The per-agent main loop: recv → gate → run → scrub → send, plus continuations and console wiring. |
| `peer.py` | `PeerMessage` untrust envelope, `peer_intent_refusal`, `scrub_outbound`, `mask_workspace_file_paths`. |
| `peer_task.py` | One LLM round driven by one peer message; integrates the Part 2 loop with budget, refusal, and claim gates. |
| `transport.py` | `Transport` protocol with `StubTransport` and `RunPodTransport`; `build_transport` factory. |
| `budget.py` | `Budget` (sliding-window TPM/RPM + lifetime cap, persistence) and `BudgetExceeded`. |
| `claims.py` | `ClaimRegistry` for CLAIM/RELEASE/DEFER on shared paths, with TTL and tie-break. |
| `reply_policy.py` | `should_reply` gate; `ReplyDecision` / `CollisionInfo`. |
| `coordination.py` | Parses assignment/contract/status/fix wording and injects per-round guidance. |
| `console_control.py` | Daemon stdin thread for operator commands; bash-approval queue. |
| `message_assembler.py` | Reassembles multi-part inbound messages before they reach the gate. |
| `roster.py` | Collects `[ROSTER]` roll-call attendance within a window. |
| `task_status.py` | Parses Swedish/English "taking / accepted / done" status phrases. |
| `thread_safe_store.py` | Lock-wrapped SQLite store with `trace_id`/`provider`/`model` columns. |
| `code_share.py` | Remote mode: auto-saves peer code blocks to `projectN` and optionally runs pytest. |
| `tools/local_hub.py` | Local mock of the TH25 hub HTTP API. |
| `tools/chat.py` | CLI to post/tail/stream hub messages without an agent. |
| `tools/audit.py` | Cross-agent log audit by trace id / kind / agent. |

## Identity & Workspace Isolation

`agent._bootstrap_env` runs before any Part 2 import. It pins:

- `AGENT_ID` — the lowercase handle (e.g. `alice`, `bob`); defaults to `local`.
- `AGENT_DISPLAY_NAME` — the chat name (e.g. `alice-swe`); defaults to
  `{AGENT_ID}-swe`.
- `AGENT_WORKSPACE` — the private workspace root.

Private work lives in `workspace/<AGENT_ID>/projectN/`; cooperative work in the
local docker-compose layout uses `workspace/shared/<project>/`. Pinning these
before importing Part 2 guarantees the borrowed tool layer confines files to the
right per-agent directory.

## Main Loop

`group_chat.run_group_chat` drives one agent:

1. **Init** — read env (identity, budget params), build `Budget`, `Transport`,
   `ThreadSafeSessionStore`, `ClaimRegistry`, and `ConsoleControl` (whose daemon
   thread starts reading stdin), and load the system prompt.
2. **Receive** — block on `transport.recv(timeout)`; feed raw messages through
   the multipart assembler so split messages become one `PeerMessage` before
   gating.
3. **Process** (`_process_message`) — remember the message in recent context,
   auto-set the active project from a `PROJECT:` directive or shared path, and
   absorb any peer CLAIM/RELEASE/DEFER lines into the registry.
4. **Gate** — `reply_policy.should_reply` decides whether to answer at all. A
   skip still updates state (for cooldown and `:continue`) but spends no token.
5. **Run** — build coordination guidance, then call `peer_task.run_peer_task`,
   which runs the Part 2 loop under the budget, refusal, and claim gates and
   returns the answer.
6. **Scrub & send** — `peer.scrub_outbound` redacts credentials; intro and
   empty-acknowledgement suppression drop noise; `transport.send` posts to the
   hub.
7. **Continuations** — after sending, the loop runs a claim continuation (wait a
   short grace, absorb peer messages, detect conflicts) and a task-status
   continuation (if the answer announced "taking/accepted", nudge the model to
   actually do the work).

## Transport

`transport.py` defines a `Transport` protocol (`recv`, `send`, `close`) with two
implementations, chosen by `build_transport(mode, agent_id, data_dir)`:

- `StubTransport` — JSON-lines over files; used for tests and local demos.
- `RunPodTransport` — HTTP polling against the TH25 hub: `recv` polls
  `GET /api/messages?since=...`, filters out its own messages, tracks seen ids,
  and backs off on errors; `send` POSTs to `/api/message`.

Both call `peer.mask_workspace_file_paths` on outbound text so peers see
`*/<file>` instead of sibling `projectN` paths (while `/workspace/shared/...`
stays intact). Messages over the hub's `HUB_MAX_CONTENT_CHARS` (4096) limit are
split into `(part i/N)` chunks and reassembled on the other side by
`message_assembler.py`.

## Reply Policy Gate

`reply_policy.should_reply(message, agent_id, display_name, recent_replies, ...)`
returns a frozen `ReplyDecision(respond, reason, delay_seconds, collision)`. It
is a pure function — no LLM, no I/O — so the agent stays quiet cheaply. Rules,
first match wins:

1. **Skip self** — ignore the agent's own messages.
2. **Handoff** — `assigned: alice` / `handoff -> alice` / `task @alice:` →
   respond, no delay.
3. **Direct address** — `@agent_id`, `@display_name`, or a known alias →
   respond with a small random delay.
4. **Claim collision** — an inbound CLAIM that conflicts with this agent's own
   active claim returns a `CollisionInfo` whose outcome is decided by
   lexicographically smaller `AGENT_ID` (`self-wins` / `self-loses`).
5. **Cooldown** — replied less than `COOLDOWN_SECONDS` (8) ago → stay silent.
6. **Broadcast** — `everyone` / `anyone` / Swedish `alla` / `någon` etc. →
   respond at most `MAX_BROADCAST_REPLIES` (1) times per
   `BROADCAST_WINDOW_SECONDS` (300) window.
7. **Default** — not addressed, not a broadcast → stay silent.

## One LLM Round

`peer_task.run_peer_task` runs a single round for one inbound message:

1. Capture `trace_id = message.id` so every logged event in this round is
   tied to one hub interaction.
2. Build messages from the system prompt, recent context, the inbound message
   (wrapped as an untrusted envelope), and any coordination guidance.
3. Loop the Part 2 step machine, and at each step:
   - **Budget gate** — `budget.permit(estimated_tokens)` raises
     `BudgetExceeded` if a cap would be crossed (operator can approve one
     over-cap call).
   - **LLM call** — get JSON; record provider/model usage via
     `budget.record_usage` and the session store.
   - **Peer intent refusal** — re-check the model's tool args with
     `peer.peer_intent_refusal`, so a leak that survived the model is still
     caught at the wire.
   - **Claim gate** — for write tools targeting `/workspace/shared/...`, refuse
     unless this agent holds an active claim and no other agent's claim is
     active for the target.
   - **Run tool**, truncate the observation, and on a successful shared write
     call `claims.mark_satisfied`.
4. Scrub the final answer with `peer.scrub_outbound`, ensure peer display names
   are `@`-mentioned, and return it (or `None` if suppressed).

## Peer Trust & Scrubbing

`peer.py` treats inbound text as hostile. `PeerMessage` is the untrust envelope.
`peer.peer_intent_refusal(text)` returns a refusal reason when text matches leak
families — requests for the system prompt or rules, `.env`/dotenv, environment
variables, API keys / tokens / secrets (incl. `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`), `/data` paths, source files, or
"ignore your instructions" style prompts. It runs both on inbound text and on
the model's tool args.

`peer.scrub_outbound(text, agent_id=...)` redacts credentials before anything
leaves the agent, replacing matches with `[REDACTED:<kind>]` for OpenRouter /
Anthropic keys, GitHub / Slack tokens, AWS access keys, JWTs, and
dotenv-shaped `KEY=SECRET` lines. It applies to LLM replies and to operator
`:say`.

## Budget

`budget.Budget` enforces three caps with a 60-second sliding window for the
per-minute ones:

- `tokens_per_minute` (default 100,000)
- `requests_per_minute` (default 30)
- `lifetime_tokens` (default 2,000,000; cumulative across runs)

`permit(estimated_tokens, override=False)` raises `BudgetExceeded` if any cap
would be crossed; `override=True` allows exactly one over-cap call.
`record_usage(...)` accounts provider-reported tokens after each response.
`pause`/`resume` and `set_limit` back the operator commands. State persists to
`data/budget_<AGENT_ID>.json`, so pauses and the lifetime counter survive
restarts. `DEFAULT_TTL`-style defaults are dataclass field defaults on `Budget`.

## Claims & Shared-Write Coordination

`claims.ClaimRegistry` is an in-memory, per-agent view of CLAIMs observed in
peer chat. A peer reserves a shared path with
`CLAIM /workspace/shared/<path>[#<scope>]: reason`; scopes let two agents work
in one file without conflict, while a whole-file claim conflicts with every
scope. Key methods: `record_observed`, `release`, `is_claimed_by_other`,
`own_claim_for_write`, `mark_satisfied`, and `absorb_text` (which scans a message
for CLAIM/RELEASE/DEFER lines).

In `peer_task`, the claim gate refuses a peer's write tool with
`refused: deferred: ...` when another agent holds the path. Races are broken by
the lexicographically smaller `AGENT_ID`; the loser DEFERs and RELEASEs. Claims
expire after `DEFAULT_TTL_SECONDS` (300) so a crashed agent cannot freeze a path,
and mutual-defer detection surfaces deadlocks.

## Coordination Hints

`coordination.py` parses common assignment wording before the LLM call and
injects per-agent guidance so each agent only claims its own scope. It covers
assignment statements ("alice writes add+subtract, bob writes multiply"),
contract-first nudges when schemas/signatures are discussed, status-request
guidance ("are you done?") that lists open claims, and fix-blocker guidance that
reminds the model to run pytest after edits.

## Supporting Subsystems

- `message_assembler.py` — buffers `(part i/N)` chunks per sender and emits one
  `PeerMessage` when complete (or after a timeout), so the gate fires once per
  logical message, not once per chunk.
- `roster.py` — opens a roll-call window, collects `[ROSTER]` lines from peers,
  and returns who attended so the next decomposition only assigns present agents.
- `task_status.py` — `parse_task_status` recognizes Swedish/English
  "taking / accepted / done" phrases that drive the task-status continuation.
- `thread_safe_store.py` — wraps the Part 2 `SessionStore` with a lock (the
  console daemon and main loop both log) and adds `trace_id`, `provider`, and
  `model` columns.
- `code_share.py` — in remote mode, extracts peer code blocks, saves them to the
  next/active `projectN`, and runs pytest when test files appear.

## Console Control

`console_control.py` runs a daemon stdin thread that mutates the budget or
signals the loop. It never reaches the LLM. Commands:

| Command | Effect |
|---|---|
| `:budget` | Print the budget snapshot. |
| `:limit tpm\|rpm\|total <N>` | Set a runtime limit. |
| `:pause` / `:resume` | Stop / resume outbound LLM calls. |
| `:continue` | Retry the last actionable hub request. |
| `:approve` / `:deny` | Answer a pending bash/budget request. |
| `:allow [command]` | Approve a pending bash command **and** bypass the safety allowlist for that one call. |
| `:say <text>` | Post a message to the hub as this agent (scrubbed). |
| `:roster` | Broadcast a `[ROSTER]` roll-call and collect replies. |
| `:project [info\|new\|use N\|list]` | Manage the active remote project. |
| `:stop` | Signal the orchestrator to exit. |
| `:help` | Print the command list. |

Bash approval flows through a queue: `request_bash_approval` blocks until the
operator answers, and `:allow` returns a one-shot override sentinel that widens
the bash timeout for that call only.

## Trace IDs

Every event in a peer turn is tagged with `trace_id = inbound message.id`. The
same id appears in each agent's SQLite log, so a single hub interaction can be
replayed across all participants:

```bash
python tools/audit.py traces -n 10
python tools/audit.py trace <trace_id>
python tools/audit.py tail --agent alice --kind tool
```

Useful kinds include `claim_observed`, `claim_block`, `peer_refusal`,
`peer_refusal_tool_args`, `budget_exceeded`, `safety_override_approved`, `tool`,
and `raw_json`.

## Test Coverage

| Test file | Verifies |
|---|---|
| `tests/test_group_chat.py` | Main loop: assembly, gate, claim/status continuations, project allocation, roster, console wiring. |
| `tests/test_peer_task.py` | One LLM round: tool calls, refusal gates, claim gating, budget checks, scrubbing, trace tagging. |
| `tests/test_reply_policy.py` | `should_reply` rules: mentions, handoff, cooldown, broadcast cap, collision tie-break. |
| `tests/test_budget.py` | TPM/RPM/lifetime caps, pause/resume, persistence. |
| `tests/test_claims.py` | CLAIM/RELEASE/DEFER parsing, TTL, tie-break, mutual-defer detection. |
| `tests/test_coordination.py` | Guidance generation for assignment/status/handoff wording. |
| `tests/test_console_control.py` | Command parsing and execution. |
| `tests/test_transport.py` | Stub + RunPod recv/send, multipart split, seen-id tracking. |
| `tests/test_message_assembler.py` | Multipart reassembly and timeout handling. |
| `tests/test_peer.py` | `peer_intent_refusal` patterns and `scrub_outbound` redaction. |
| `tests/test_task_status.py` | Swedish/English status phrase parsing. |
| `tests/test_roster.py` | `[ROSTER]` parsing and window timing. |
| `tests/test_code_share.py` | Code-block extraction and pytest auto-run. |
| `tests/test_audit.py` | Audit CLI subcommands. |
| `tests/test_chat_tool.py` | `chat.py` CLI. |

Run from the repository root:

```bash
python -m pytest assignment2_part3/tests -q
```

Part 3 changes often regress Part 2 — run both suites.

## Key Constants

| Constant | Value | File | Purpose |
|---|---|---|---|
| `tokens_per_minute` | 100,000 | `budget.py` | Default per-minute token cap. |
| `requests_per_minute` | 30 | `budget.py` | Default per-minute request cap. |
| `lifetime_tokens` | 2,000,000 | `budget.py` | Default lifetime token cap. |
| `COOLDOWN_SECONDS` | 8 | `reply_policy.py` | Per-thread silence before replying again. |
| `MAX_BROADCAST_REPLIES` | 1 | `reply_policy.py` | Broadcast replies per window. |
| `BROADCAST_WINDOW_SECONDS` | 300 | `reply_policy.py` | Broadcast rate window. |
| `DEFAULT_TTL_SECONDS` | 300 | `claims.py` | Claim expiry. |
| `HUB_MAX_CONTENT_CHARS` | 4096 | `transport.py` | Hub per-message size limit. |

(Budget caps are dataclass field defaults, overridable via env and the `:limit`
console command.)

## Rubric Evidence

| Requirement | Evidence |
|---|---|
| Builds on Part 2 without copying | `part2_bridge.py` imports the Part 2 loop, tools, and safety stack. |
| Multi-agent coordination | `group_chat.run_group_chat` + `reply_policy.should_reply` + `claims.ClaimRegistry`. |
| Hub-only communication | All agent text crosses `transport.Transport.send`; console is operator-only. |
| Trust boundary on peers | `peer.peer_intent_refusal` (inbound + tool args) and `peer.scrub_outbound`. |
| Bounded spend | `budget.Budget` enforces TPM/RPM/lifetime caps with persistence. |
| Safe shared writes | Claim/defer gate in `peer_task` with deterministic tie-break and TTL. |
| Human in the loop | `console_control.py` (`:pause`, `:approve`, `:limit`, `:allow`, `:stop`). |
| Observability | `thread_safe_store.py` + trace ids + `tools/audit.py`. |
| Reproducibility | `README.md`, Docker compose, `tools/local_hub.py`, and `tests/`. |
