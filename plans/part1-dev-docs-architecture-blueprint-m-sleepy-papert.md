# Architecture Blueprints for Part 2 and Part 3

## Context

`assignment2_part1/dev_docs/architecture_blueprint.md` is a clean, code-faithful
design document for the Part 1 ReAct CLI agent. It follows a fixed structure:
Purpose → Design Goals → Module Responsibilities table → the loop → Model
Protocol → subsystem sections (LLM Client, tool, safety) → Test Coverage →
Rubric Evidence table.

Parts 2 and 3 have no equivalent. Part 2 (`assignment2_part2/dev_docs/` holds
only demo/plan/README notes) and Part 3 (`assignment2_part3/dev_docs/` holds
only `starting_prompts_agent_chat.md`) lack a single document that explains the
whole system the way Part 1's blueprint does.

**Goal:** Write two new `architecture_blueprint.md` files — one per part — that
mirror Part 1's structure and tone but are *extended* to cover the substantially
larger surface area (Part 2: structured-JSON tool agent; Part 3: multi-agent hub
layer on top of Part 2). The output is documentation only — no code changes.

## Conventions to match (from Part 1's blueprint)

- Markdown, ~80-col prose wrapping, fenced `text` blocks for flow diagrams and
  protocol shapes.
- A **Module Responsibilities** table (`| Module | Responsibility |`).
- Numbered loop walkthrough.
- A closing **Rubric Evidence** table (`| Requirement | Evidence |`).
- **Reference modules and function names, not line numbers** (Part 1 does this;
  line numbers rot and several gathered numbers are approximate). Cite files and
  functions like `parser.parse_response`, `safety.safety_check`.
- Keep claims faithful to the code already explored; do not invent behavior.

## File 1: `assignment2_part2/dev_docs/architecture_blueprint.md`

Sections:

1. **Purpose** — structured-JSON tool agent; contrast with Part 1's text
   protocol. One-JSON-object-per-turn, an agent loop, a safety allowlist+blocklist,
   four editing tools, SQLite session log, auto-run-tests-after-write.
2. **Design Goals** — JSON contract over freeform text; default-deny bash;
   workspace confinement; deterministic tests; observability via SQLite.
3. **Module Responsibilities** table — `agent.py`, `parser.py`, `safety.py`,
   `tools.py`, `session_store.py`, `llm_client.py`, `runtime_helpers.py`,
   `colors.py`, `config/system_prompt.txt`.
4. **Agent Loop** (numbered, from `agent.run_task`): intent refusal →
   build messages → for each step up to `MAX_STEPS` (8): `complete_chat` →
   `parse_response` → final vs tool_call → `_run_tool_call` → on successful
   write `_run_post_edit_tests` → append observation → repeat. Note context
   trimming (`MAX_CONTEXT_TURNS`, `MAX_CONTEXT_CHARS`).
5. **JSON Protocol** — the two shapes (`{"type":"final","answer":...}` and
   `{"type":"tool_call","tool":...,"args":{...},"reason":...}`) and what
   `parser.parse_response` rejects (non-JSON, non-object, missing fields,
   unknown tool, field conflicts).
6. **Tools** — table of all registered tools with args; spotlight the four core
   ones (`bash`, `create_file`, `edit_section`, `replace_text`) plus
   `read_file`/`append_text`/`rename_file`/`run_tests`. Note `ToolSpec` fields
   (`mutates_workspace`, `requires_approval`, `success_prefixes`), 4000-char
   output cap, 10s bash timeout.
7. **Safety Stack** — four layers: `intent_refusal`, allowlist tokens,
   blocklist regexes, path-argument checks; plus `confirm_command` y/N.
8. **Auto-run Tests After Write** — `runtime_helpers.workspace_mutation_tools`
   + `tool_succeeded`; `_run_post_edit_tests` runs `pytest assignment2_part2 -q`
   (120s) after any successful mutating tool; test failure finalizes.
9. **LLM Client** — providers (groq / openrouter / local), `LLM_PROVIDER_ORDER`,
   `response_format=json_object` with fallback, rate-limit retry/backoff.
10. **Session Store** — `events(id, created_at, role, kind, content)` schema and
    what gets recorded each round.
11. **Test Coverage** — per-file summary (`test_parser`, `test_safety`,
    `test_tools`, `test_agent`, `test_llm_client`, `test_session_store`).
12. **Key Constants** table — `MAX_STEPS`, `MAX_OUTPUT_CHARS`,
    `COMMAND_TIMEOUT_SECONDS`, `POST_EDIT_TEST_TIMEOUT_SECONDS`, token/timeout
    defaults.
13. **Rubric Evidence** table.

## File 2: `assignment2_part3/dev_docs/architecture_blueprint.md`

Sections:

1. **Purpose** — multi-agent layer that *imports Part 2* via `part2_bridge.py`
   (only file that knows where Part 2 lives); adds Transport, Budget,
   ReplyPolicy, ClaimRegistry, ConsoleControl. Per-agent process; hub-only
   inter-agent text.
2. **Design Goals** — hub-only communication, per-round leak refusal + outbound
   scrub, pure-function reply gate (no LLM cost), sliding-window budget,
   claim/defer for shared writes, trace IDs across agents, human-in-the-loop
   console.
3. **Module Responsibilities** table — `agent.py`, `part2_bridge.py`,
   `group_chat.py`, `peer.py`, `peer_task.py`, `transport.py`, `budget.py`,
   `claims.py`, `reply_policy.py`, `coordination.py`, `console_control.py`,
   `message_assembler.py`, `roster.py`, `task_status.py`, `thread_safe_store.py`,
   `code_share.py`, plus `tools/` (`local_hub.py`, `chat.py`, `audit.py`).
4. **Identity & Workspace Isolation** — `AGENT_ID` + `AGENT_DISPLAY_NAME`,
   `_bootstrap_env`, `workspace/<AGENT_ID>/projectN/` vs `workspace/shared/`.
5. **Main Loop** (`group_chat.run_group_chat`, numbered) — init → `recv` /
   message assembly → `_process_message` → `should_reply` gate → `run_peer_task`
   → `scrub_outbound` → `transport.send` → claim/status continuations; console
   daemon thread runs alongside.
6. **Transport** — `Transport` protocol, `StubTransport` vs `RunPodTransport`,
   `build_transport`, hub `mask_workspace_file_paths`, 4096-char split, polling
   + backoff.
7. **Reply Policy Gate** — `should_reply` ordered rules (skip self, handoff,
   direct mention, claim collision tie-break, cooldown, broadcast cap, default);
   `ReplyDecision`/`CollisionInfo`.
8. **One LLM Round** (`peer_task.run_peer_task`) — trace_id capture, budget
   gate, LLM call, parse, peer intent refusal on tool args, claim gate, project
   gate, run tool, mark satisfied, scrub answer, ensure @-mentions.
9. **Peer Trust & Scrubbing** (`peer.py`) — `PeerMessage` untrust envelope,
   `peer_intent_refusal` pattern families, `scrub_outbound` credential kinds
   (`[REDACTED:<kind>]`).
10. **Budget** (`budget.Budget`) — sliding 60s TPM/RPM + lifetime cap,
    `permit`/`record_usage`, `BudgetExceeded`, pause/resume, JSON persistence.
11. **Claims & Shared-Write Coordination** (`claims.ClaimRegistry`) —
    CLAIM/RELEASE/DEFER, scopes, conflict rule, lexicographic tie-break, 300s
    TTL, `_maybe_claim_block` gate in peer_task, mutual-defer detection.
12. **Coordination Hints** (`coordination.py`) — assignment/contract-first/
    status/fix-blocker guidance injected before the LLM round.
13. **Supporting subsystems** — `message_assembler` (multipart reassembly),
    `roster` (roll-call window), `task_status` (Swedish/English phrases),
    `thread_safe_store` (locked SQLite + trace_id/provider/model columns),
    `code_share` (remote auto-save + pytest).
14. **Console Control** — command list (`:budget`, `:limit`, `:pause`/`:resume`,
    `:approve`/`:deny`/`:allow`, `:continue`, `:say`, `:roster`, `:project`,
    `:stop`, `:help`); daemon stdin thread never reaches the LLM.
15. **Trace IDs** — `trace_id = message.id` tagged across every agent's SQLite
    log; `tools/audit.py trace <id>` replays one interaction.
16. **Test Coverage** — per-file summary across the ~17 test files.
17. **Key Constants** table — budget defaults, reply cooldown/broadcast window,
    claim TTL, peer_task step caps, transport limits, roster window.
18. **Rubric Evidence** table — multi-agent coordination, safety boundaries,
    budget enforcement, human-in-the-loop, observability.

## Critical files (read-only references; no edits)

- Pattern source: `assignment2_part1/dev_docs/architecture_blueprint.md`
- Part 2: `agent.py`, `parser.py`, `safety.py`, `tools.py`, `runtime_helpers.py`,
  `session_store.py`, `llm_client.py`, `config/system_prompt.txt`
- Part 3: `group_chat.py`, `peer_task.py`, `peer.py`, `reply_policy.py`,
  `budget.py`, `claims.py`, `transport.py`, `coordination.py`,
  `console_control.py`, `part2_bridge.py`, `tools/audit.py`

## Verification

- `markdownlint` is not configured here; verify by eye that both files render and
  the tables are well-formed.
- Spot-check 5–10 named symbols per file actually exist (e.g. `grep -n
  "def should_reply" assignment2_part3/reply_policy.py`, `def run_task`
  in part2 `agent.py`) so the docs stay code-faithful.
- Confirm both files live under each part's `dev_docs/` next to the Part 1 peer.
- No code or test changes; `git status` should show only the two new docs.
