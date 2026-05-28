# Plan — fix local-mode default + replay-wake latency

## Context

After the operator typed `:project new`, the chat session in `assignment2_part3/` had three observable problems:

1. **"Both ignored the shared folder"** — emil-user wrote `build a calculator in /workspace/shared/calculator.py`. Both alice and bob wrote to their *private* `/workspace/<agent>/project5/` instead. Root cause: `docker-compose.yml` defaults `AGENT_MODE: runpod` for `agent-alice` (line 25) and `agent-bob` (line 106), even though those containers are the *local* profile and bind-mount `./workspace:/workspace`. In runpod mode, `_remote_workspace_guidance` (`group_chat.py:138-173`) explicitly instructs the model **not** to use `/workspace/shared/`. The mode label says "remote" but the actual filesystem is shared — a config bug. (`agent-remote`, line 68, correctly hardcodes `AGENT_MODE: runpod`; that one stays.)
2. **"Bob needed `continue` to start"** — perceived delay between `:project new` and the parked-inbound replay. The main loop only drains `pending_replay` on its next iteration after `transport.recv(timeout=1.0)` returns (`group_chat.py:1217-1229`), so the worst case is ~1s of latency. That's small but visible, and worth eliminating since the operator already paid for an LLM call by hitting `:project new`.
3. **Alice "lost" with many `ls` calls** — pure model-prompting issue, not a runtime bug. Out of scope for this plan (would require changes to `_remote_workspace_guidance` / `_local_workspace_guidance`, and even then is unreliable).

Intended outcome: with the compose default flipped to `local`, alice and bob co-write `/workspace/shared/<project>/calculator.py` and the CLAIM/RELEASE/DEFER protocol fires as designed. With the replay wake-up event, work begins as soon as the operator types `:project new`, with no perceptible gap.

## Changes

### 1. Flip docker-compose default to `AGENT_MODE: local` for alice/bob

**File:** `assignment2_part3/docker-compose.yml`

- Line 25: `AGENT_MODE: ${AGENT_MODE:-runpod}` → `AGENT_MODE: ${AGENT_MODE:-local}` (agent-alice)
- Line 106: `AGENT_MODE: ${AGENT_MODE:-runpod}` → `AGENT_MODE: ${AGENT_MODE:-local}` (agent-bob)
- Line 68 (`agent-remote`): **unchanged** — stays hardcoded `runpod`.

**Why this works today, no other code changes needed:**

- `group_chat.py:130-135` (`_project_root`): `mode == "runpod"` returns private workspace; **else** (any other value, including `"local"`) returns `SHARED_WORKSPACE` env var as root with `is_shared=True`.
- `group_chat.py:566-567`: `runpod = mode == "runpod"` — every downstream gate uses this boolean, so `"local"` flips them all correctly.
- `group_chat.py:943-966`: in shared mode, the inbound's `/workspace/shared/<name>/` path or `PROJECT: <name>` directive auto-sets the active project on first inbound — so the operator may not even need to type `:project new` for the calculator-style task.
- The `process_shared_code` / CLAIM gate / `_peer_claim_blocking_save` paths (`group_chat.py:920, 1022-1049`) all activate via `project_state.is_shared`, which is set to `True` when `_project_root` returns `is_shared=True`.
- `_local_workspace_guidance` (`group_chat.py:176-onwards`) is the per-round system-prompt addendum that tells the model to write to `/workspace/shared/<project>/<file>` and to use the CLAIM/RELEASE/DEFER protocol. This is dispatched whenever `is_shared=True`.

**Note on `/workspace/shared/calculator.py` (flat file, no project subfolder):**
`coordination.project_name_from_shared_path` returns `None` for a flat shared file (`test_coordination.py:618`). So a message naming `/workspace/shared/calculator.py` will *not* auto-allocate a project name; the operator still types `:project new`, OR includes `PROJECT: <name>`, OR names a subdir path like `/workspace/shared/calc/calculator.py`. That's an acceptable shape — the wake-up event below makes `:project new` instant.

### 2. Wake the main loop immediately after `:project new` / `:project use`

**File:** `assignment2_part3/group_chat.py`

Add a `threading.Event` that the project handler sets and the main loop watches with a much shorter `recv` timeout when there are parked messages.

Concretely:

- Near line 652 (where `pending_replay_lock` is declared) add:
  ```python
  project_changed_event = threading.Event()
  ```
- Pass `project_changed_event` into `_build_project_handler` and `.set()` it after each successful `project_state.active = ...` assignment (lines 302, 319, 326 in `_build_project_handler`).
- In the main loop (`group_chat.py:1210-1229`), replace `transport.recv(timeout=1.0)` with a conditional timeout: if `project_changed_event.is_set()`, clear it and skip directly to the drain; otherwise use a short timeout (e.g., 0.1s) when parked messages are present, else the existing 1.0s.

**Why this is safe vs. the alternative of calling `_drain_pending_replay()` from the console thread:** `_process_message` runs `_run_task_for_message` which blocks on an LLM call. Running that on the console daemon would block stdin and break further `:approve` / `:pause` / `:stop` commands. The Event-based wake-up keeps all message processing on the main thread (consistent with the existing `stop_event` pattern at `group_chat.py:1211` and `console_control.py:82`), only the *signal* crosses threads.

### 3. Tests

**Existing runpod-pinned tests must keep passing.** They all set `AGENT_MODE=runpod` explicitly via `monkeypatch.setenv` (e.g. `test_group_chat.py:555, 592, 1395, 1446`), so the compose default flip does not affect them.

**New tests:**

- `test_group_chat.py` — a test that with `AGENT_MODE=local` and `SHARED_WORKSPACE` set, an inbound naming `/workspace/shared/calc/x.py` auto-allocates `calc` as the active project and processes the inbound (no skip/park).
- `test_group_chat.py` — a test that simulates the wake-up event: after the project handler sets active, the main-loop drain happens within a small bounded time even when `transport.recv` is configured with a long timeout. Use the existing fake-transport patterns (`StubTransport`/test fixtures).

### 4. Out of scope (deliberate non-changes)

- **Alice's degenerate `Hej` and `ls` loop** — model-side, not runtime. The local-mode flip indirectly helps (different system prompt, clearer "write to /workspace/shared/<project>" directive), but no code change here.
- **Auto-allocate project on flat shared paths** — could change `coordination.project_name_from_shared_path` to fall back to a sentinel like `default`, but that conflicts with multi-project tests and changes broader semantics. Leave the operator's `:project new` as the canonical opt-in; the wake-up event makes it instant.
- **`agent-remote` mode** — stays `runpod`, correct.

## Critical files

- `assignment2_part3/docker-compose.yml` — change two lines (25, 106).
- `assignment2_part3/group_chat.py` — add `project_changed_event`, thread it into `_build_project_handler`, set in the three `active = ...` branches, watch in main loop.
- `assignment2_part3/console_control.py` — no change required; the event is mutated from inside `_build_project_handler` which is already called from the console thread via `project_handler`.
- `assignment2_part3/tests/test_group_chat.py` — add two new tests; existing tests untouched.

## Verification

1. **Unit tests pass:**
   - `python -m pytest assignment2_part3/tests -q` — both new and existing tests.
   - `python -m pytest assignment2_part1 -q` and `python -m pytest assignment2_part2 -q` — Part 3 changes shouldn't touch these, but rerun per `CLAUDE.md` discipline.
2. **Rebuild & exercise the four-terminal demo:**
   ```bash
   cd assignment2_part3
   docker compose build agent-alice agent-bob
   docker compose up -d
   docker compose logs -f                          # T1
   docker attach assignment2_part3-agent-alice-1   # T2
   docker attach assignment2_part3-agent-bob-1     # T3
   python tools/chat.py live --as emil-user        # T4
   ```
3. **Repeat the failing scenario:**
   - In T4, send: `@bob-swe @alice-swe build a calculator in /workspace/shared/calc/calculator.py. First agree on signatures add/subtract/multiply/divide. Each emit a CLAIM with scope #add-subtract or #multiply-divide. Write pytest in the same folder.`
   - In T2 and T3, type `:project new`.
   - **Expected:** parked inbound is drained within <100ms of `:project new` (no perceptible delay); both agents emit a CLAIM, then write under `/workspace/shared/<project>/`, then RELEASE; tests run and pass; final files are visible on the host at `assignment2_part3/workspace/shared/<project>/`.
4. **Audit:**
   ```bash
   python tools/audit.py traces -n 5
   python tools/audit.py trace <trace_id>
   ```
   - Confirm `claim_observed`, `claim_block` (if any), and successful `tool` events for `/workspace/shared/...`.
5. **Remote-profile regression check:** `docker compose --profile remote up agent-remote` still boots with `AGENT_MODE: runpod` and refuses to touch `/workspace/shared/` per `_remote_workspace_guidance`. (Don't actually connect to a real RunPod hub — just confirm the env is set and the runtime starts.)
