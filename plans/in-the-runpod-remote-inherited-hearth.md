# Remote-hub workspace: active `projectN/`, console control, write redirect

## Context

The first iteration of this plan (already implemented and merged on the working tree) added `code_share.py` and a runpod-only hook in `group_chat.py` that allocated a **fresh `projectN/` per peer message** containing code fences, then ran pytest if any `test_*.py` landed.

Two problems surfaced once the remote agent was actually run:

1. **Per-message project allocation is too granular.** A 3-message coding handoff splits into 3 directories that lose their relationship to each other. The natural unit is "this connection" or "this session", not "this single chat message".
2. **The agent still wrote its own collaborative output to `/workspace/shared/`.** That is exactly what `config/system_prompt.txt` (P3.8 + P3.3 + P3.9) tells it to do, and that behavior is correct for the local docker-compose 2-agent demo. But on the TH25 remote hub there is no shared filesystem with other people's bots — `/workspace/shared/` is local to this container, peers cannot see it, and the CLAIM/DEFER protocol guarding it serves no purpose.

This iteration replaces the per-message allocation with a single **active project** concept owned by `run_group_chat`, exposes operator commands (`:project new` / `:project use N` / `:project list` / `:project`) so the operator can switch the active project after `docker attach`, and injects a runtime-guidance line on every runpod-mode turn telling the LLM to write to `/workspace/<AGENT_ID>/<active>/...` instead of `/workspace/shared/` and to suppress CLAIM/DEFER/RELEASE protocol lines.

The system prompt stays one file. Per-mode behavior remains a runtime-guidance concern, consistent with how `coordination.py` already overlays per-message context on top of a single prompt.

## Design summary

1. **`code_share.process_shared_code` no longer allocates.** It takes an `active_project: Path` and writes into it. `code_share.next_project_dir` (cap-aware allocator, already implemented) is now called by `group_chat` and the `:project new` handler, not by `process_shared_code`.
2. **`code_share.most_recent_project_dir(workspace)`** (new) returns the highest-numbered existing `projectN/` or `None`. Used at startup to pick the initial active project.
3. **`group_chat.run_group_chat` owns active-project state** for the lifetime of the loop. On startup (runpod mode only): pick most-recent existing `projectN/`, or allocate `project1` if none. Print a banner. Pass a `project_handler` callback to `ConsoleControl`.
4. **Always-on runtime guidance (runpod mode)** describing the active project + the write-redirect + the CLAIM/DEFER suppression. This runs every turn, in addition to (and after) the existing coordination guidance and the existing `process_shared_code` per-turn guidance.
5. **`ConsoleControl`** gets a new optional `project_handler` constructor arg and a `:project` command dispatch. When the handler is `None` (local mode), `:project` prints `[project not enabled in this mode]`.

The runtime guidance is the override mechanism, not enforcement. The Part 2 path resolver still permits `/workspace/shared/` writes from the agent — if the LLM ignores the guidance and writes there anyway, the file lands locally and peers won't see it. That's a follow-up if needed; flagged at the bottom.

## Files to modify

### `assignment2_part3/code_share.py`

Changes:

- Add `most_recent_project_dir(agent_workspace: Path) -> Path | None`. Reuse the `_existing_project_indices` helper that already exists; return `agent_workspace / f"project{max(indices)}"` or `None`.
- Change `process_shared_code` signature to:
  ```
  process_shared_code(message_text: str, agent_id: str, active_project: Path) -> str | None
  ```
  Drop the `agent_workspace` + internal `next_project_dir(...)` call. Save into `active_project` directly. Build the guidance string using `agent_id` and `active_project.name`. The "cap reached" branch is removed from this function — capping is now the `:project new` handler's concern.
- Keep `next_project_dir`, `save_code_blocks`, `maybe_run_pytest`, `extract_code_blocks` as-is.

### `assignment2_part3/group_chat.py`

Changes (concentrated in `run_group_chat` and `_run_task_for_message`):

1. **Reorder** so `runpod = mode == "runpod"` is computed before `ConsoleControl` is constructed (currently it lands later, at line 305).

2. **Add small mutable state holder** at module top:
   ```python
   @dataclass
   class _ProjectState:
       active: Path | None = None
   ```
   (Mutable so the console-thread handler and the main loop see the same object. Python attribute assignment under the GIL is atomic enough for this use.)

3. **Inside `run_group_chat`, before constructing `ConsoleControl`:**
   ```python
   project_state = _ProjectState()
   project_handler = None
   if runpod:
       agent_workspace = Path(os.environ["AGENT_WORKSPACE"])
       initial = most_recent_project_dir(agent_workspace) or next_project_dir(agent_workspace)
       project_state.active = initial
       # banner via print(colors.dim(...)) — match the existing "[part3] ... listening via ..." line style
       project_handler = _build_project_handler(project_state, agent_workspace, store_logger=...)
   ```
   The `_build_project_handler` returns a `Callable[[str, list[str]], str]` that:
   - `"info"` (default when no args) → `f"active={state.active.name}"` or `"active=<none>"`.
   - `"new"` → `nxt = next_project_dir(workspace)`; if `None`, return cap error; else update state and return `f"active=projectN (new)"`.
   - `"use"` + `N` → check `agent_workspace / f"project{N}"` exists; if not, return error; else update state. Refuse non-integer / out-of-range input.
   - `"list"` → enumerate existing projectN dirs in numerical order, mark the active one with `*`.
   - Unknown action → return usage string.

4. **Pass `project_handler` to `ConsoleControl(...)`** in the `if console is None` branch. The default `None` (used by tests / local mode) preserves existing behavior.

5. **In `_run_task_for_message`, after the existing coordination guidance calls and after the `process_shared_code` call:**
   - Refactor the existing runpod block to call `process_shared_code(message.text, agent_id, project_state.active)` instead of the old signature.
   - Append the new always-on runtime guidance:
     ```python
     if runpod and project_state.active is not None:
         runtime_guidance.append(_remote_workspace_guidance(agent_id, project_state.active.name))
     ```

6. **`_remote_workspace_guidance(agent_id, project_name)`** returns text along the lines of:
   ```
   Remote hub mode (no shared filesystem). Your active project is /workspace/<AGENT_ID>/<project>/.
   Write every file you create or edit under /workspace/<AGENT_ID>/<project>/<filename>. Do NOT
   write to /workspace/shared/ on this hub — peers cannot see it and there is no point in claiming
   it. Do NOT emit CLAIM, RELEASE, or DEFER protocol lines on this hub; the system prompt's P3.9
   protocol applies only to the local docker-compose demo. The operator can switch active project
   at any time with :project new or :project use N.
   ```
   Pure function in `group_chat.py` (or a small helper in `code_share.py` — either is fine; placing it next to its only caller in `group_chat.py` keeps `code_share.py` filesystem-only).

### `assignment2_part3/console_control.py`

Changes:

- Add `project_handler: Optional[Callable[[str, list[str]], str]] = None` to `__init__`.
- Add `:project` dispatch in `_handle`:
  ```python
  elif cmd == "project":
      self._cmd_project(args)
  ```
- New `_cmd_project(self, args)`:
  ```python
  if self.project_handler is None:
      self._print("[project not enabled in this mode]")
      return
  action = args[0].lower() if args else "info"
  rest = args[1:]
  try:
      result = self.project_handler(action, rest)
  except Exception as exc:
      result = f"[project error] {exc}"
  self._print(result)
  ```
- Update `HELP_TEXT` to include the new commands.

### `assignment2_part3/tests/test_code_share.py`

Refactor: replace the orchestrator tests for the old signature with three new ones:

- `test_most_recent_project_dir_picks_highest_existing` — workspace with `project1`, `project3`, `project2` → returns `project3`.
- `test_most_recent_project_dir_returns_none_when_empty` — empty workspace → `None`.
- Update `test_process_shared_code_end_to_end_no_tests` and `test_process_shared_code_end_to_end_with_tests` to pass an `active_project` arg explicitly (caller-allocated).
- Remove `test_process_shared_code_at_cap_returns_noticing_string` — cap handling moved to the `:project new` handler.

All `extract_*`, `next_project_dir`, `save_*`, `maybe_run_pytest` tests are unchanged.

### `assignment2_part3/tests/test_console_control.py`

Add tests that mirror the existing `_start_console` pattern:

- `test_project_command_without_handler_prints_disabled` — no handler → `:project new` echoes `[project not enabled in this mode]`.
- `test_project_command_invokes_handler` — handler is a stub that records calls; `:project use 5` → stub called with `("use", ["5"])`, return value is printed.
- `test_project_info_default_action` — `:project` with no args → handler called with `("info", [])`.

### `assignment2_part3/tests/test_group_chat.py`

Add (if there's a clean injection point — `test_group_chat.py` already exists; verify whether it tests `run_group_chat` directly or via subcomponents). At minimum:

- `test_remote_workspace_guidance_text_mentions_active_project` — direct unit test on the helper that builds the guidance string.

End-to-end interplay (`_run_task_for_message` mutating state from `:project`) is hard to unit-test without spinning up the loop; rely on the manual verification steps below for that path.

## Files NOT to modify (and why)

- `config/system_prompt.txt` — runtime guidance carries the per-mode override. A hard fork into `system_prompt_local.txt` / `system_prompt_remote.txt` is a fallback if the LLM does not respect the runtime guidance in practice; not needed for v1.
- `peer_task.py` — `runtime_guidance` plumbing (lines 663–667) already handles arbitrary guidance strings.
- `coordination.py` — content-pattern-triggered guidance; the active-project override is unconditional in runpod mode, not pattern-matched, so it belongs in `group_chat.py`, not here.
- Part 2 path resolver (`tools.py`) — would have to change to *enforce* the redirect; treated as out of scope.

## Critical files

| Concern | File | Notes |
|---|---|---|
| Active-project state + banner + handler | `assignment2_part3/group_chat.py` | `_ProjectState`, `_build_project_handler`, `_remote_workspace_guidance`, reorder `runpod` computation |
| Per-call code save (signature change) | `assignment2_part3/code_share.py` | `process_shared_code(msg, agent_id, active_project)`, new `most_recent_project_dir` |
| Operator commands | `assignment2_part3/console_control.py` | `project_handler` ctor arg, `:project` dispatch, HELP_TEXT |
| Reused | `assignment2_part3/console_control.py` | existing `_handle`, `_print`, `_loop` patterns |
| Reused | `assignment2_part3/peer_task.py` 663–667 | runtime_guidance injection (unchanged) |
| Test patterns | `assignment2_part3/tests/test_console_control.py` 18–25 | `_start_console` helper to copy |

## Open follow-ups (not part of v1)

- **Hard enforcement of the write redirect.** If the LLM ignores the runtime guidance and writes to `/workspace/shared/` anyway, that file lands locally and silently. Enforcement would mean wiring an extra check into `_run_tool_call` / the Part 2 tool dispatch to refuse `/workspace/shared/...` paths in runpod mode. Defer until we see empirical evidence that runtime guidance is not enough.
- **Per-mode prompt files.** If runtime guidance gets too long or the LLM doesn't comply, split into `config/system_prompt_local.txt` + `config/system_prompt_remote.txt`. Higher maintenance cost; only worth it if needed.
- **Backlog-on-first-join** (carried over from the first iteration). Still relevant: on a fresh `emil_hjertfors_bot` join, the agent replays every prior hub message, each one going through `process_shared_code`. With the new active-project model, the spam is bounded (one shared dir, not N), but the LLM still pays per-turn cost. Two-line transport-side fix described in the previous plan version still applies.

## Verification

```bash
# Unit (deterministic, fast)
python -m pytest assignment2_part3/tests/test_code_share.py -q
python -m pytest assignment2_part3/tests/test_console_control.py -q

# Full Part 3 + Part 2 regression
python -m pytest assignment2_part3/tests -q
python -m pytest assignment2_part2 -q
```

Manual remote end-to-end:

1. Confirm `.env` has `AGENT_MODE=runpod`, `RUNPOD_CHAT_URL=...`, `AGENT_ID=emil_hjertfors_bot` (or the exact spelling you want on the hub — earlier exploration showed `emil_hjaertfors_bot`; pick one).
2. `docker compose --profile remote up -d --build agent-remote`.
3. `docker compose --profile remote logs -f agent-remote` → expect a banner like `[project] active=project1 — :project new for fresh, :project use N to switch`.
4. `docker attach <container>` and type `:project` → prints `active=project1`. Then `:project new` → prints `active=project2 (new)`; filesystem check: `assignment2_part3/workspace/emil_hjertfors_bot/project2/` exists.
5. Have a peer ask the bot to write a file. Expect: the bot writes to `/workspace/emil_hjertfors_bot/project2/...` (visible on host under `assignment2_part3/workspace/emil_hjertfors_bot/project2/`), **not** `/workspace/shared/`. Expect: no `CLAIM` / `RELEASE` / `DEFER` lines in its replies.
6. `:project use 1` → switches active back. Next peer write lands in `project1/`.
7. `:project list` → enumerates existing projects, marks active with `*`.
