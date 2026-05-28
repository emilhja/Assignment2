# Remote-mode: auto-save peer-shared code blocks without prior `PROJECT:` directive

## Context

In Part 3 remote (RunPod) mode, the runtime already extracts fenced code blocks from inbound peer messages and writes them to disk via `code_share.process_shared_code` (called from `assignment2_part3/group_chat.py:1017`). However, the save is gated on `project_state.active is not None` (group_chat.py:1002). In remote mode the active project is **only** auto-allocated when the inbound message carries an explicit `PROJECT: <name>` directive (group_chat.py:950-974) or when the operator runs `:project new` in the agent console.

The observed gap: when a peer (e.g., `sonia-agent`) opens with a fenced ```python block but no `PROJECT:` directive, the code is dropped on the floor. The operator console only sees the `[project?] no active project` advisory; nothing lands on disk for `read_file` later.

Intended outcome: in remote mode, the first peer message that contains at least one markdown code fence should auto-allocate the next `workspace/<AGENT_ID>/projectN/`, make it active, and save the extracted blocks there. Subsequent inbound code lands in the same active project (sticky), matching existing `_ProjectState` semantics.

## Design (confirmed)

- **Trigger**: any inbound message with at least one fenced code block triggers allocation. No allowlist; no @-mention requirement. Matches the sonia-agent example.
- **Stickiness**: once allocated, the projectN stays active until the operator switches (`:project use N` / `:project new`) or a later `PROJECT: <name>` directive switches it. Subsequent code lands in the same project.

## Changes

### 1. `assignment2_part3/group_chat.py` — `_process_message`

Extend the remote auto-allocate guard at lines 950-974. Today it allocates only on `parse_project_directive`. Add a second trigger: fenced code blocks.

Logical structure inside the existing `if runpod and project_state.active is None and project_state.root is not None:` block:

```
directive_name = parse_project_directive(message.text or "")
allocate_reason = None
if directive_name:
    allocate_reason = ("directive", directive_name)
elif extract_code_blocks(message.text or ""):
    allocate_reason = ("code_share", message.sender_id)

if allocate_reason is not None:
    new_dir = next_project_dir(project_state.root)
    if new_dir is not None:
        project_state.active = new_dir
        kind, detail = allocate_reason
        _log(
            store,
            "project_auto_allocated" if kind == "directive" else "project_auto_allocated_from_code",
            f"name={new_dir.name} reason={kind} {('directive_name=' + detail) if kind == 'directive' else ('sender=' + detail)} msg_id={message.id}",
        )
        print(
            colors.dim(
                f"[project] auto-allocated active={new_dir.name} "
                + (f"from PROJECT: {detail}" if kind == "directive" else f"from peer-shared code by {detail}")
            ),
            flush=True,
        )
```

The existing `if runpod and project_state.active is None:` advisory branch (line 982) then becomes unreachable for messages that contain code (because we just set `active`), so it correctly only fires for plain-text, no-directive inbound — unchanged contract.

The downstream `process_shared_code(...)` call at line 1017 then runs naturally because `project_state.active` is now set, saves the blocks under `workspace/<AGENT_ID>/projectN/`, runs auto-pytest (per the existing `auto_pytest=True` for remote), and appends `code_guidance` for the LLM turn.

### 2. `assignment2_part3/tests/test_group_chat.py`

Add one new test, mirroring the existing `project_auto_allocated` tests (find them with grep for `project_auto_allocated` in this file):

- `test_remote_inbound_with_code_blocks_auto_allocates_project`: build a `PeerMessage` whose text contains a ```python fenced block but no `PROJECT:` directive. Drive `_process_message` (or the public entry point used by the existing remote-allocate tests). Assert:
  - `project1/` directory was created under the per-agent workspace tmpdir.
  - The block content was written to `project1/<filename>` (use `# file: foo.py` directive inside the fence to make the filename deterministic).
  - A `project_auto_allocated_from_code` event was logged with `sender=<peer>`.
  - A second inbound with a second fenced block lands in the same `project1/` (stickiness).

Keep deterministic: no real LLM/network — reuse whatever stub harness the existing remote tests use.

### Reuse points

- `code_share.extract_code_blocks` — already imported and used at `group_chat._peer_claim_blocking_save` (line 898).
- `code_share.next_project_dir` — already used at line 957.
- `_log` (SQLite event logger) — used throughout `_process_message`.
- `colors.dim` — used for the existing `[project]` advisory line.

### Files to modify

- `assignment2_part3/group_chat.py` — `_process_message`, lines 944-995 only.
- `assignment2_part3/tests/test_group_chat.py` — one new test.

No changes needed in: `code_share.py`, `coordination.py`, `claims.py`, `transport.py`, `peer.py`, `peer_task.py`, system prompt, Dockerfile, docker-compose.yml.

## Verification

1. `python -m pytest assignment2_part3/tests/test_group_chat.py -q` — new test passes, existing tests still pass.
2. `python -m pytest assignment2_part3 -q` — full Part 3 suite green (sticky semantics, project_auto_allocated logging contract, etc.).
3. `python -m pytest assignment2_part2 -q` — confirm Part 2 unaffected (per repo convention).
4. Manual smoke against the local hub mock:
   - Run `tools/local_hub.py` and one agent with `AGENT_MODE=runpod`-equivalent config.
   - From `tools/chat.py`, send a message containing a fenced ```python block and no `PROJECT:` directive.
   - Confirm in the agent console: `[project] auto-allocated active=project1 from peer-shared code by <sender>`.
   - Confirm on disk: `workspace/<AGENT_ID>/project1/<filename>` exists with the expected content.
   - Confirm via audit: `python tools/audit.py tail --agent <id> --kind project_auto_allocated_from_code` shows the event.
   - Send a second fenced block in a later message → file lands in the same `project1/`, no new project allocated.
