# Remove no-active-project inbound skip in runpod mode

## Context

In runpod mode, `assignment2_part3/group_chat.py:1018-1027` parks every inbound
peer message and returns silently when no active project is set. The operator
sees only `[skip] no active project ...` (and the per-startup `[project?]`
banner), the agent never calls the LLM, and the hub broadcast goes unanswered.

The user observed this in production against the live RunPod hub:
`emil-hjaertfors-agent` received seven broadcasts in a row, skipped every one,
and never showed `[hub<-]` echoes or any conversational reply. The two other
agents on the hub (`lullo-swe-agent`, `igor-petersson-agent`) replied because
they already had an active project.

Desired behavior: the agent should converse normally with the hub regardless
of project state — only the **file-write tools** should refuse while no
project is active, with a one-line refusal that tells the operator what to
type. Visibility of inbound hub messages (`[hub<-]` echo lines) should be
preserved or restored.

Note: the user's terminal pasted a `[skip] no active project — :project new
or :project use N to choose` line that does not exist in the current
working tree. That text is from an older built image; rebuild is required
after the change (`docker compose --profile remote build --no-cache
agent-remote`).

## Goals

1. Runpod inbound messages flow through the normal pipeline (`should_reply` →
   `run_peer_task` → `transport.send`) even with no active project.
2. File-write tools (`create_file`, `append_text`, `edit_section`,
   `replace_text`, `rename_file`) refuse with a clear "no active project"
   observation, similar in shape to the existing claim refusal.
3. The model is told, via runtime guidance, that it may converse but must not
   call write tools yet — and how the operator can unblock it.
4. The `pending_replay` / `_park_for_replay` plumbing is removed; no message
   is ever parked.
5. Tests are updated to match the new behavior.

## Files to modify

- `assignment2_part3/group_chat.py` — drop the skip/park branch, add runtime
  guidance for the no-project case, thread `project_active` into
  `run_peer_task`, delete the replay plumbing, reword the startup/`[project?]`
  banner.
- `assignment2_part3/peer_task.py` — add a new `_maybe_no_project_refusal`
  guard alongside the existing `_maybe_shared_write_refusal`; accept a new
  `project_active: bool = True` kwarg in `run_peer_task` and gate writes on
  it.
- `assignment2_part3/tests/test_group_chat.py` — replace the four tests
  documented below with tests for the new behavior.

No changes to `transport.py` are needed — `RunPodTransport.recv` already
echoes `[hub<-]` per message at `transport.py:300-306`. If the operator
isn't seeing those, the cause is a stale docker image, not a code bug.

## Changes in detail

### 1. `group_chat.py` — remove the skip-and-park branch

Delete `group_chat.py:1018-1027`:

```python
if runpod and project_state.active is None:
    _absorb_inbound_claims(message)
    _log(store, "reply_decision",
         f"respond=False reason=no_active_project msg_id={message.id} sender={message.sender_id}")
    _print_no_project_prompt(message)
    _park_for_replay(message)
    return
```

Replace with a one-line stdout notice (so the operator still sees *why* the
agent can't write yet) plus a log line, and fall through:

```python
if runpod and project_state.active is None:
    _log(store, "no_active_project_advisory",
         f"msg_id={message.id} sender={message.sender_id}")
    print(
        f"{colors.ts()} {colors.dim('[project?] no active project — replying without writes; type :project new or :project use N to enable file tools')}",
        flush=True,
    )
```

`_absorb_inbound_claims(message)` is still called later in the function for
the `should_reply=False` path; for the `True` path, `_process_message` already
absorbs claims before/after sending. No change there.

### 2. `group_chat.py` — runtime guidance for no-project case

In `_run_task_for_message` (the `if project_state.active is not None:` block
at lines 825-835), add an `elif runpod:` branch:

```python
if project_state.active is not None:
    if code_guidance:
        runtime_guidance.append(code_guidance)
    if runpod:
        runtime_guidance.append(
            _remote_workspace_guidance(agent_id, project_state.active.name)
        )
    else:
        runtime_guidance.append(
            _local_workspace_guidance(agent_id, project_state.active)
        )
elif runpod:
    runtime_guidance.append(_no_project_conversation_guidance())
```

Add the helper at module level:

```python
def _no_project_conversation_guidance() -> str:
    return (
        "Remote hub mode, but no active project is allocated yet. You may "
        "converse, plan, ask clarifying questions, and read existing files. "
        "Do NOT call create_file, append_text, edit_section, replace_text, "
        "or rename_file in this round — the runtime will refuse them. If "
        "the task requires writing files, tell the operator (in chat) to "
        "type `:project new` in the agent console, or to include "
        "`PROJECT: <name>` in their next broadcast. Truthful-completion "
        "rule still applies: do not claim 'done', 'created', or 'wrote' "
        "for work that has not been performed."
    )
```

### 3. `group_chat.py` — thread `project_active` into `run_peer_task`

In `_run_task_for_message`, add to the `run_peer_task(...)` call:

```python
return run_peer_task(
    ...
    project_active=(project_state.active is not None),
    ...
)
```

### 4. `peer_task.py` — refuse write tools when no project

Add a helper near `_maybe_shared_write_refusal` (around line 286):

```python
NO_PROJECT_REFUSAL = (
    "refused: no active project — file-write tools are disabled. "
    "Operator: type `:project new` to allocate one, or include "
    "`PROJECT: <name>` in the next broadcast."
)

def _maybe_no_project_refusal(tool: str, project_active: bool) -> str | None:
    if project_active:
        return None
    if tool not in CLAIM_GATED_TOOLS:
        return None
    return NO_PROJECT_REFUSAL
```

In `run_peer_task`, add the kwarg with default `True` (so existing tests and
local-mode callers are untouched). Before the `_run_tool_with_approval(...)`
dispatch at `peer_task.py:1274`, add:

```python
refusal = _maybe_no_project_refusal(parsed.tool, project_active)
if refusal is not None:
    observation = refusal
    # match the existing refusal logging shape used by _maybe_shared_write_refusal
    ...
else:
    observation = _run_tool_with_approval(parsed.tool, parsed.args, console)
```

Use the same logging kind as the existing claim refusal (`peer_refusal_tool_args`
or whichever is canonical at that callsite) with a distinct reason field, so
`tools/audit.py tail --kind peer_refusal_tool_args` keeps surfacing both.

### 5. `group_chat.py` — drop the replay plumbing

Delete:
- `MAX_PARKED_INBOUND_REPLAY` (line 70)
- `pending_replay`, `pending_replay_lock` init (lines 664-665)
- `_park_for_replay` (lines 873-892) and `_drain_pending_replay` (lines 899-904)
- The replay drain in the main loop (lines 1229-1232)
- The shorter-recv-timeout branch tied to parked inbound (lines 1244-1249) —
  revert to a constant `recv_timeout = 1.0`.

Keep `project_changed_event` only if `_build_project_handler`'s
`on_active_changed` callback still has a reason to fire. With parking gone,
the event no longer drives anything; delete it too (lines 600, 610) and
simplify `_build_project_handler` to drop the `on_active_changed` parameter.

### 6. `group_chat.py` — reword the banner

In `_render_no_project_prompt` (lines 387-423), the line
`"in the next broadcast to auto-start."` and surrounding text should drop the
"Inbound messages are skipped until you choose." framing (which exists in
some built image the user is running). Replace with something like:

```
[project?] no active project — replying to chat is enabled, but file-write
tools are refused until you choose one.
[project?] existing: project1, project2, ...
[project?] type `:project new` to allocate a fresh project, `:project use <N>`
to reconnect, or include `PROJECT: <name>` in the next broadcast to auto-start.
```

### 7. Tests to replace in `tests/test_group_chat.py`

These four tests assert behavior we are removing:

- `test_runpod_with_existing_projects_defers_and_skips_until_chosen` (line 541)
- `test_runpod_skip_parks_inbound_without_active_project` (line 1379)
- `test_project_activation_drains_parked_inbound_and_replays` (line 1416)
- `test_replay_queue_caps_at_four_drops_oldest` (line 1472)

Replace with three new tests:

1. `test_runpod_no_project_still_calls_llm_and_replies` — fake hub delivers
   one inbound; assert `ctx["fake_chat"].calls >= 1`, assert
   `transport.sent` contains a reply, no `inbound_parked_no_project` event.
2. `test_runpod_no_project_refuses_write_tools` — model emits a
   `create_file` tool call; assert observation is `NO_PROJECT_REFUSAL`,
   assert no file was written, assert the refusal was logged as a
   `peer_refusal_tool_args` (or chosen kind) row.
3. `test_runpod_no_project_passes_conversation_guidance` — assert the
   string from `_no_project_conversation_guidance()` is present in the
   prompt sent to `complete_chat` for runpod+no-project. (Cheap proxy:
   inspect `ctx["fake_chat"].seen_prompts[-1]` for the marker phrase.)

Leave the test file's other ~160 tests untouched.

## Verification

```bash
# 1. Unit tests
python -m pytest assignment2_part3/tests -q

# 2. Part 2 regression (Part 3 changes often regress Part 2)
python -m pytest assignment2_part2 -q

# 3. Rebuild the remote image so the change reaches the container
cd assignment2_part3
docker compose --profile remote build --no-cache agent-remote

# 4. Live smoke against the RunPod hub
docker compose --profile remote up -d agent-remote
docker compose --profile remote logs -f agent-remote        # T1
docker attach assignment2_part3-agent-remote-1              # T2
# T3 — from host:
python tools/chat.py say --as emil-user "@emil-hjaertfors-agent ping, no project yet"
# Expect: agent replies in T1/T3 within ~5s, no [skip] line, no write attempt.

python tools/chat.py say --as emil-user "@emil-hjaertfors-agent please create foo.py with print('hi')"
# Expect: agent replies with a one-line "no active project, ask :project new" message
# (either via runtime guidance shaping the LLM, or via NO_PROJECT_REFUSAL surfaced
# back through the model on the next round). No file written.

# In T2:
:project new
# T3:
python tools/chat.py say --as emil-user "@emil-hjaertfors-agent now please create foo.py"
# Expect: agent calls create_file, observation succeeds, file appears under
# workspace/emil_hjaertfors_bot/projectN/foo.py.
```

## Out of scope

- No change to local-hub (`docker-compose --profile local`) behavior — it
  already runs without the runpod-only gate.
- No change to budget, reply-policy, claim, or scrubber logic.
- No change to `audit.py` or other tools.
