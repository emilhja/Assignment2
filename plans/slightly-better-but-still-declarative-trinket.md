# Stop the runtime from rewriting successful writes as failures

## Context

A prior fix (`plans/please-check-and-fix-curious-badger.md`) made `reply_policy._strip_protocol_lines` ignore `@mentions` inside `DEFER`/`RELEASE` lines, plus added a system-prompt bullet telling the model not to reply DEFER-on-DEFER. That fix worked for the peer-to-peer ping-pong: in the latest session SQL (`assignment2_part3/data/{alice,bob}.sqlite3`) every `reply_decision` against a peer DEFER correctly returns `respond=False reason=not addressed; not a broadcast`.

But the loop still appears, just one level up: now both agents emit a fresh `DEFER` whenever the **user** sends `@<id> continue`. The user reports "slightly better but still not complete".

Tracing alice's SQL events 1505–1532 reveals the actual root cause is not in the reply gate, the system prompt, or `claims.py`. It is in `peer_task.py:441-447`: **the runtime silently rewrites alice's truthful success message into a false "I could not complete the shared-file write" reply, which then poisons her own `recent_context` on every subsequent turn and makes her defer.**

### Evidence — alice's full trace for the user's "everyone continue" message (msg=9)

| Event | Action | Observation |
|---|---|---|
| 1511 | alice posts `CLAIM /workspace/shared/calculator.py#add-subtract` | OK |
| 1515 | observes bob's `CLAIM …#multiply-divide` | OK |
| 1517 | runtime injects `claim_continuation` nudge | OK |
| 1518 | alice calls `create_file(/workspace/shared/calculator.py, overwrite=false)` | — |
| 1519 | **`claim_block`**: "scoped claim … cannot recreate existing shared file" (bob's `create_file` won the race and wrote the whole file). This sets `saw_failed_shared_write = True` at `peer_task.py:476`. | blocked |
| 1521 | alice calls `read_file` → sees bob's content | OK |
| 1523 | alice calls `edit_section` → observation `"Edited one section in /workspace/shared/calculator.py."` (verified, full row read) | **SUCCESS** |
| 1524 | alice final answer: *"The file /workspace/shared/calculator.py has been updated to include the add and subtract functions in addition to the existing multiply and divide functions. Next, I will create pytest tests for these functions."* | truthful |
| 1526 | **`peer_reply_corrected`**: runtime overwrites her answer to *"I could not complete the shared-file write. The latest tool observation reported a block/refusal, so no successful update to /workspace/shared should be assumed."* | **LIE** |
| 1531 | next turn (`@alice-swe try again`): alice → `DEFER to @bob-swe` |  |

The user's next message — "everyone i see that calculator is ready" — independently confirms the file IS written. The runtime's "could not complete" override was wrong on the wire.

### Why the override fires falsely

`saw_failed_shared_write` is set to `True` in two places (`peer_task.py:476` and `peer_task.py:489`) but is never cleared. After it flips on, the conditional at `peer_task.py:441` triggers on any final answer that mentions `/workspace/shared/` together with a success verb (`created`, `added`, `updated`, `wrote`, `implemented` — see `_looks_like_write_success_claim`, lines 265-270). That conditional then substitutes the false confession from lines 442-446.

The flag is **per-turn local** (initialized at line 355), so it doesn't leak across messages — but within one turn a recovery sequence of `create_file (blocked) → read_file → edit_section (succeeded)` still hits the substitution, because the success-write step doesn't clear the flag.

## Recommended approach

One surgical change in `peer_task.py` to reset the flag when a later shared write actually succeeds. No new abstractions; this just makes the existing flag mean what its name promises.

### Change — `peer_task.py`: clear `saw_failed_shared_write` on a successful CLAIM-gated write

**File:** `assignment2_part3/peer_task.py`

Replace the block at lines 483–489:

```python
if (
    parsed.tool in CLAIM_GATED_TOOLS
    and isinstance(parsed.args.get("path"), str)
    and parsed.args["path"].startswith(SHARED_PATH_PREFIX)
    and _looks_like_failed_write(observation)
):
    saw_failed_shared_write = True
```

with:

```python
if (
    parsed.tool in CLAIM_GATED_TOOLS
    and isinstance(parsed.args.get("path"), str)
    and parsed.args["path"].startswith(SHARED_PATH_PREFIX)
):
    if _looks_like_failed_write(observation):
        saw_failed_shared_write = True
    else:
        # A subsequent successful shared write supersedes an earlier
        # failure in this turn. Without this, a recovery sequence
        # (create_file blocked → read_file → edit_section succeeded)
        # still triggers _looks_like_write_success_claim and the runtime
        # overwrites the model's truthful success report at line 441.
        saw_failed_shared_write = False
```

This is the minimum fix that restores wire-level honesty. After this change, alice's success message at event 1524 reaches the hub unmodified, the user no longer sees a false "I could not complete" reply, and the cascading DEFER loop on subsequent `@alice continue` prompts disappears.

### Files to leave alone

- `reply_policy.py` — the previous plan's strip-DEFER fix is correct; do not revert.
- `config/system_prompt.txt` — the P3.9 bullet about DEFER-on-DEFER is correct.
- `claims.py`, `group_chat.py` — claim conflict, tie-break, and continuation logic are working as designed for the racing-CLAIM case. The bug is downstream.

## Tests to add

`assignment2_part3/tests/test_peer_task.py` — add a regression covering the recovery sequence:

1. **`test_failed_shared_write_flag_clears_on_subsequent_success`**: simulate one peer-task run where the first `create_file` on `/workspace/shared/x.py` returns "Edit blocked: path already exists" (so the flag flips on), then an `edit_section` on the same path returns "Edited one section in …" (the flag should flip back off), then the model's final answer says "I updated /workspace/shared/x.py". The returned scrubbed answer MUST equal the model's text — not the "I could not complete" rewrite.

2. **`test_failed_shared_write_flag_persists_when_no_recovery`**: simulate `create_file` blocked, no further successful shared write, then the model claims "I created /workspace/shared/x.py". The rewrite at lines 441-447 SHOULD still fire (the existing protective behavior). This locks in that the fix only loosens the override for genuine recoveries.

Run with:
```bash
cd assignment2_part3 && python -m pytest tests/test_peer_task.py -v
```

## Verification

1. **Unit tests** (new + existing):
   ```bash
   cd assignment2_part3
   python -m pytest tests/test_peer_task.py tests/test_reply_policy.py -v
   ```

2. **DB replay sanity** — re-inspect alice's most recent session DB after the fix is in:
   ```bash
   python -c "import sqlite3;c=sqlite3.connect('data/alice.sqlite3').cursor();c.execute(\"select id,kind,substr(content,1,120) from events where kind='peer_reply_corrected' order by id desc limit 5\");[print(r) for r in c.fetchall()]"
   ```
   After re-running the calculator scenario there should be zero new `peer_reply_corrected` events for turns whose last tool observation was a successful edit/replace.

3. **Live replay**:
   ```bash
   cd assignment2_part3
   docker compose up -d
   python tools/chat.py live --as emil-user
   ```
   Then send the original prompt:
   ```
   @bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py.
   Agree on function signatures in chat first (one message each), then split:
   alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM with
   the function names in the scope (e.g. #add-subtract). Write pytest tests
   next to it.
   ```
   Expected:
   - Each agent posts a scoped CLAIM, then writes.
   - When one agent loses the `create_file` race, they recover with `read_file` + `edit_section` and report success truthfully — no "I could not complete" override in chat.
   - On `@<id> continue` follow-ups, the addressed agent continues real work (e.g. `create_file` on `test_calculator.py`) instead of emitting `DEFER`.

## Known follow-up (not in this plan)

Bob's behavior in the same session is a different, milder bug: he posted `CLAIM /workspace/shared/test_calculator.py#test-cases` (event 1446) but never called `create_file` for it on his next turn — he emits `DEFER` instead. `group_chat.py`'s `claim_continuation` injection only fires when the agent's most recent reply contains a fresh CLAIM line, not when the agent has an older active claim with no write yet. A follow-up plan should make `group_chat.py` also nudge agents about stale open claims on their next turn. Out of scope for this fix because it's behavioral, not a runtime falsehood.
