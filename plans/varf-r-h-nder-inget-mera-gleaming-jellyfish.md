# Nudge agents about stale, unsatisfied active claims

## Context

Two prior fixes landed:

- `plans/please-check-and-fix-curious-badger.md` — `reply_policy._strip_protocol_lines` stops `DEFER`/`RELEASE` mentions from triggering replies. (`reply_policy.py:87-99, 201`)
- `plans/slightly-better-but-still-declarative-trinket.md` — `peer_task` clears `saw_failed_shared_write` after a later successful write so truthful success messages survive. (`peer_task.py:540-548`)

Both worked, but the calculator scenario still stalls. Plan 2 already flagged the next bug at the bottom under "Known follow-up":

> Bob posted `CLAIM /workspace/shared/test_calculator.py#test-cases` (event 1446) but never called `create_file` on his next turn — he emits `DEFER` instead. `group_chat.py`'s `claim_continuation` injection only fires when the agent's most recent reply contains a fresh CLAIM line, not when the agent has an older active claim with no write yet.

So: agent posts `CLAIM …`, then either defers, says "I will create …", or runs out of tool steps. The claim sits in the registry (TTL 300s) but the file never gets written. On the next inbound message the runtime offers no reminder, the model has forgotten its open obligation, and "inget händer mera sen."

User confirmed this is the symptom they observed.

### Why the existing continuation logic misses it

`group_chat._continue_claims` (`group_chat.py:410-413`) parses CLAIM targets from `answer` — the just-sent reply text. If the agent's current reply has no CLAIM line, the function returns immediately, even when `claims.active_claims_for(agent_id)` still holds an unsatisfied claim from a prior turn.

There is also no signal anywhere that distinguishes "claim posted but never written" from "claim posted and successfully fulfilled." Both look identical to the registry until the TTL expires.

## Recommended approach

Three small, additive changes — no new modules, no abstractions.

1. Teach `ClaimRegistry` to remember when a claim was satisfied by a successful shared write.
2. Have `peer_task` mark the claim satisfied after a successful `create_file` / `edit_section` / `replace_text` observation on `/workspace/shared/<path>`.
3. Have `group_chat` inject a runtime-guidance line on each subsequent task run that lists the agent's unsatisfied active claims, telling the model to either write or RELEASE them now.

This keeps the fix scoped to data the runtime already tracks. No filesystem inspection, no per-turn timers, no new event loop.

### Change 1 — `claims.py`: track satisfaction

**File:** `assignment2_part3/claims.py`

- Add `satisfied_at: float | None = None` to the `Claim` dataclass (line 48). Because `Claim` is `frozen=True`, satisfaction tracking goes in a sibling dict on `ClaimRegistry`, not on `Claim` itself. Use a `{_claim_key(path, scope): satisfied_at_float}` map keyed the same way `_claims` is.
- Add `ClaimRegistry.mark_satisfied(self, claimant: str, path: str) -> bool`:
  - Resolves the agent's own active claim matching `path` (use the same logic as `own_claim_for_write` at lines 185-191 — a write to `/workspace/shared/foo` satisfies any active self-claim with `path == "/workspace/shared/foo"`, regardless of scope).
  - Records the satisfaction timestamp under the matching claim key.
  - Returns True if it marked something.
- Add `ClaimRegistry.unsatisfied_claims_for(self, claimant: str) -> list[Claim]`:
  - Iterates `active_claims_for(claimant)`.
  - Returns only those whose key is absent from the satisfaction map.
- Adjust `release()` and TTL expiry paths so the satisfaction-map entry is cleared when the claim entry is removed (otherwise re-claims after release would look pre-satisfied).

### Change 2 — `peer_task.py`: mark satisfaction on successful writes

**File:** `assignment2_part3/peer_task.py`

Inside `run_peer_task`, the tool-call branch already inspects shared-path writes at lines 535-548 (the `saw_failed_shared_write` block). Extend the success path:

```python
if (
    parsed.tool in CLAIM_GATED_TOOLS
    and isinstance(parsed.args.get("path"), str)
    and parsed.args["path"].startswith(SHARED_PATH_PREFIX)
):
    if _looks_like_failed_write(observation):
        saw_failed_shared_write = True
    else:
        saw_failed_shared_write = False
        if claims is not None and self_id:
            claims.mark_satisfied(self_id, parsed.args["path"])  # NEW
```

Rationale: this is the same place where the previous fix decides the write actually succeeded, so it is the right gate to flip the satisfaction bit. No new heuristic.

### Change 3 — `group_chat.py`: inject stale-claim guidance on every task run

**File:** `assignment2_part3/group_chat.py`

Add a small helper near the other guidance helpers (above `_run_task_for_message` at line 267):

```python
def _stale_claim_guidance(active_claims: list[Claim]) -> str | None:
    if not active_claims:
        return None
    targets = ", ".join(claim.target for claim in active_claims)
    return (
        "You have unsatisfied active CLAIM(s) from a previous turn: "
        f"{targets}. On this turn either complete the write with "
        "create_file/edit_section/replace_text for each, or post "
        "`RELEASE <target>` to give it up. Do not re-post the same CLAIM."
    )
```

In `_run_task_for_message`, after the existing `handoff_guidance` block (around line 295) and before the `run_peer_task` call:

```python
stale = claims.unsatisfied_claims_for(agent_id)
guidance = _stale_claim_guidance(stale)
if guidance:
    runtime_guidance.append(guidance)
```

This piggybacks on the existing `runtime_guidance` list that `peer_task` already wraps as authoritative runtime messages (`peer_task.py:402-406, 162-174`), so no new injection plumbing.

#### Why this is safe

- `unsatisfied_claims_for` returns nothing once the agent successfully writes (Change 2 flips the flag).
- TTL expiry (300s default) removes the claim entirely, so a forgotten claim self-clears.
- The guidance instructs the model to RELEASE as an alternative, so a no-longer-relevant claim has an explicit exit.
- It does NOT re-trigger `_continue_claims`, so the runtime does not spawn extra rounds — it only enriches the round the agent was going to run anyway.

### Files to modify

- `assignment2_part3/claims.py` — add satisfaction map and two methods
- `assignment2_part3/peer_task.py` — call `mark_satisfied` on a successful shared write (one line in the existing block at 540-548)
- `assignment2_part3/group_chat.py` — new helper + 4-line injection in `_run_task_for_message`

### Files to leave alone

- `reply_policy.py` — the DEFER-strip fix is correct; cooldown semantics still apply.
- `config/system_prompt.txt` — the rule already says "When your scoped work is finished, post RELEASE …" (line 65). No new prompt change needed; the runtime guidance is per-turn and more direct than a prompt.
- `coordination.py` — assignment/handoff guidance is unrelated to the stale-claim path.
- `group_chat._continue_claims` — keep its current "only continue freshly-CLAIMed targets" behavior. Mixing in stale claims there would create a continuation loop; the per-turn guidance approach avoids that.

## Tests to add

### `assignment2_part3/tests/test_claims.py`

(Create if missing; otherwise extend.)

1. `test_mark_satisfied_then_unsatisfied_claims_for_excludes_it`:
   - Register a claim for `alice` on `/workspace/shared/calc.py#add`.
   - `unsatisfied_claims_for("alice")` returns it.
   - Call `mark_satisfied("alice", "/workspace/shared/calc.py")` (note: write path can be the base, scope is implicit).
   - `unsatisfied_claims_for("alice")` returns empty list.

2. `test_mark_satisfied_only_affects_matching_claimant`:
   - Two claims on different paths by alice and bob.
   - `mark_satisfied("alice", …)` does not affect bob's claim.

3. `test_release_clears_satisfaction_map`:
   - Mark satisfied, then `release(...)`, then re-`record_observed(...)` the same target.
   - Fresh claim must appear in `unsatisfied_claims_for(...)` again.

4. `test_ttl_expiry_clears_satisfaction_map`:
   - With a fake clock, register + satisfy + advance past TTL + re-register.
   - Fresh claim must appear in `unsatisfied_claims_for(...)`.

### `assignment2_part3/tests/test_peer_task.py`

5. `test_successful_shared_write_marks_claim_satisfied`:
   - Pre-register a self-claim for the agent.
   - Drive `run_peer_task` with a stubbed chat that returns a `create_file` on `/workspace/shared/foo.py` and then a final answer.
   - After the call, `claims.unsatisfied_claims_for(agent_id)` is empty.

6. `test_failed_shared_write_does_not_mark_satisfied`:
   - Same setup but stub the write to return `"Edit blocked: …"`.
   - The claim remains in `unsatisfied_claims_for(...)`.

### `assignment2_part3/tests/test_group_chat.py`

(Optional but high-value end-to-end coverage.)

7. `test_stale_claim_guidance_injected_on_next_turn`:
   - Build a fake transport, a real `ClaimRegistry`, and a stub `run_peer_task` replacement that records the `runtime_guidance` it received.
   - Inject a self-claim into the registry without marking it satisfied.
   - Feed a peer message that the agent will respond to (e.g., a direct mention).
   - Assert the captured `runtime_guidance` contains the stale-claim string and the target path.

Run with:
```bash
cd assignment2_part3 && python -m pytest tests/test_claims.py tests/test_peer_task.py tests/test_group_chat.py -v
```

## Verification

1. **Unit tests** — all the cases above pass; existing `test_reply_policy.py`, `test_peer_task.py`, `test_coordination.py` still pass:
   ```bash
   cd assignment2_part3
   python -m pytest -v
   ```

2. **SQL replay sanity** — after a live run, no `claim_continuation` events should appear for stale claims (the existing continuation path is unchanged), but the new `runtime_guidance_injection` events from `peer_task.py:402-406` should mention the stale targets when bob is re-addressed:
   ```bash
   python -c "import sqlite3; c=sqlite3.connect('data/bob.sqlite3').cursor(); c.execute(\"select id,kind,substr(content,1,140) from events where kind='runtime_guidance_injection' order by id desc limit 5\"); [print(r) for r in c.fetchall()]"
   ```

3. **Live replay** — the exact calculator scenario from both prior plans:
   ```bash
   cd assignment2_part3
   python tools/chat.py live --as emil-user
   ```
   Send:
   ```
   @bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py.
   Agree on function signatures in chat first (one message each), then split:
   alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM with
   the function names in the scope (e.g. #add-subtract). Write pytest tests
   next to it.
   ```
   Expected after the fix:
   - Both agents post scoped CLAIMs and successfully write their scope (existing behavior).
   - If one agent's first write attempt stalls (defers, "I will…", step exhaustion), the very next message addressed to that agent triggers a runtime-guidance line listing the unsatisfied claim; the model then writes or releases on that turn.
   - No infinite continuation loops; the satisfaction bit flips on the first successful write and the guidance disappears.
