# Unstick the alice/bob stall + harden the claim runtime

## Context

The user is running the Part 3 multi-agent calculator demo and the agents
stall the same way they did in previous sessions. From the transcript:

```
[15:25] <bob-swe>   CLAIM   /workspace/shared/calculator.py#multiply-divide
[15:25] <alice-swe> CLAIM   /workspace/shared/calculator.py#add-subtract
[15:25] <alice-swe> RELEASE /workspace/shared/calculator.py#add-subtract
[15:25] <bob-swe>   RELEASE /workspace/shared/calculator.py#multiply-divide
emil-user> everyone continue
[15:27] <alice-swe> "I need to create the add and subtract functions ... as previously claimed."
[15:27] <bob-swe>   DEFER to @alice-swe
emil-user> @alice-swe you seem to have done your part
[15:28] <alice-swe> (same sentence as before)
```

No tool call ever runs and no file appears in `/workspace/shared/`.

### Root cause (verified against code)

1. **CLAIM/RELEASE happen in the same logical turn pair without a write
   in between.** Each agent posts `CLAIM ...` as its final answer.
   `group_chat._continue_claims` (group_chat.py:434) synthesizes a
   continuation message (`_claim_continuation_message` at line 124) and
   re-runs `run_peer_task`. The model — confused that the user *also*
   asked for "agreement on signatures" first, plus the absence of any
   peer-side acknowledgment — answers the continuation with `RELEASE`,
   which is a perfectly valid final answer per the parser.
2. **`claims.absorb_text` (claims.py:236–260) honors RELEASE
   unconditionally.** The claim is deleted from the registry the moment
   the RELEASE line is scrubbed/sent. There is no "satisfied?" check on
   release — the `_satisfied` map exists (`mark_satisfied`, line 207)
   but `release()` never reads it.
3. **After RELEASE, `_stale_claim_guidance` (group_chat.py:175) has no
   active claims to nudge about.** When `emil-user> everyone continue`
   arrives, alice runs with zero runtime guidance about the abandoned
   work, defaults to restating intent, and never re-CLAIMs.
4. **Bob's DEFER is correct per the system prompt.** P3.9 line 59 says
   "If you observe another agent's CLAIM for the same path and same
   scope ... reply with DEFER". Alice's "I need to create ... as
   previously claimed" reads, to bob's model, as an active claim — even
   though the registry is empty. Bob defers and the deadlock locks in.

So the runtime is one-shot: if the continuation doesn't produce a write,
nothing in the system pulls the agent back to finish.

### Goal of this change

- Right now: get alice and bob to actually produce
  `/workspace/shared/calculator.py` + tests in the current session.
- Durable: make a premature RELEASE recoverable (or impossible) so this
  failure mode does not recur.

---

## Part A — Unstick the current session (operator actions, no code)

Run these from the four-terminal layout already documented in
`CLAUDE.md` ("Part 3 multi-agent local hub"). No restart needed unless
Part B is done first.

1. **Confirm the registry is empty and identify what was missed.** From
   any terminal:

   ```bash
   cd assignment2_part3
   python tools/audit.py tail --agent alice --kind claim_observed -n 20
   python tools/audit.py tail --agent alice --kind claim_continuation_pending_write_reprompt -n 20
   python tools/audit.py tail --agent bob   --kind claim_observed -n 20
   ```

   You should see alice's CLAIM, then RELEASE, then nothing — no
   `tool` event for `create_file` and no `mark_satisfied`-style entry.
   That confirms the diagnosis.

2. **Push alice with one operator chat message that bypasses the broken
   claim flow.** From the `tools/chat.py live` terminal:

   ```
   @alice-swe You are clear to proceed. Re-post your CLAIM
   `CLAIM /workspace/shared/calculator.py#add-subtract: implement add and subtract`
   and on the next runtime continuation immediately call create_file with
   path /workspace/shared/calculator.py and content containing
   def add(a, b): return a + b
   def subtract(a, b): return a - b
   Do not RELEASE until after a successful create_file observation.
   ```

   The wording matters: the system prompt forbids re-CLAIM only "after
   deferring" (P3.9 line 71). Alice DID NOT defer, she released, so
   re-CLAIM is allowed. Spell that out so her model doesn't self-censor.

3. **Then do the same for bob** (separately, so the broadcast rate
   limiter doesn't dampen the second message):

   ```
   @bob-swe Same instructions for your half: re-CLAIM
   /workspace/shared/calculator.py#multiply-divide and call create_file
   or edit_section for multiply/divide on the continuation. Then write
   /workspace/shared/test_calculator.py with pytest tests for all four
   functions and run run_tests on it.
   ```

4. **If alice still loops on "I need to create..."**, use her console
   directly (terminal T2, attached to `agent-alice`):

   ```
   :say CLAIM /workspace/shared/calculator.py#add-subtract: implement add and subtract per emil-user
   ```

   This posts the CLAIM as alice. The next message reaching her — even
   "ok" from the operator — will trigger `_continue_claims` because the
   registry now has her active claim.

5. **Last resort: restart with a single-step task.** Stop and restart
   both agents, then send one message at a time:

   ```bash
   docker compose restart agent-alice agent-bob
   ```

   Then in chat: `@alice-swe create /workspace/shared/calculator.py
   with def add(a,b) and def subtract(a,b). Post your CLAIM, then on
   the continuation call create_file.` (and the equivalent for bob
   separately). Splitting "agree signatures" from "claim" from "write"
   into one message each is what makes the planner happy.

---

## Part B — Durable fix in the multi-agent runtime

The minimum-surface-area fix that closes the loophole.

### B1. Reject "RELEASE without write" inside the claim-continuation loop

**File:** `assignment2_part3/peer_task.py`
**Where:** the `if parsed.kind == "final":` block at lines 493–542,
right next to the existing CLAIM-reprompt (line 495) and pending-write
reprompt (line 505).

Add a third reprompt branch:

```python
if (
    _is_claim_continuation(message)
    and RELEASE_PATTERN.search(answer)
    and not saw_successful_shared_write   # see B2
):
    guidance = (
        "You posted RELEASE but the runtime has no successful "
        "create_file/edit_section/replace_text observation for "
        "/workspace/shared in this round. RELEASE without a write "
        "abandons the claim. Either call the write tool now to "
        "complete the work, or send a final answer that explicitly "
        "explains why you cannot proceed (do not say 'released' as "
        "the reason)."
    )
    _log("system", "claim_release_without_write_reprompt", guidance)
    messages.append({"role": "user", "content": guidance})
    continue
```

Import `RELEASE_PATTERN` from `claims` (already imported alongside
`CLAIM_PATTERN`).

### B2. Track per-round successful shared write

Same file. There is already a `saw_failed_shared_write` flag (line 386,
flipped where shared writes are observed). Add a sibling
`saw_successful_shared_write = False` initialized at the same site, and
set it `True` in the tool_observation branch when a `create_file` /
`edit_section` / `replace_text` targeting `/workspace/shared/...` returns
without a `refused:` / `Edit blocked:` / `Tool error:` prefix. The
detection logic should mirror the existing `_looks_like_write_success_claim`
helper (peer_task.py:265) but read the observation, not the answer.

This new flag is the precondition for B1's reprompt and also lets us
upgrade `mark_satisfied` calls (`claims.mark_satisfied`, currently
unused in the live path) to fire here.

### B3. Drop "released without write" guidance on the next inbound turn

**File:** `assignment2_part3/group_chat.py`
**Where:** `_stale_claim_guidance` (line 175). Right now it only fires
when `claims.unsatisfied_claims_for(agent_id)` returns something. We
need a parallel "recently released-without-write" nudge so alice's next
turn after `emil-user> everyone continue` actually gets a kick.

Add to `ClaimRegistry` (claims.py):

- New field `_released_unsatisfied: dict[str, tuple[Claim, float]]` keyed
  by `_claim_key`.
- In `release()` (line 134), if the released claim's key is NOT in
  `self._satisfied`, store the released claim in `_released_unsatisfied`
  with current timestamp.
- New `recently_released_unsatisfied_for(claimant, window=120.0)`
  returning the list (with expiry).

Then extend `_stale_claim_guidance` (group_chat.py:175) to also accept
that list and produce a message like:

> "You previously CLAIMed `<target>` and then RELEASEd without a
> successful write. Re-CLAIM and complete the write, or explain in chat
> why you abandoned the work."

Wire it into `_run_task_for_message` (group_chat.py:317) right after
the existing `_stale_claim_guidance` call.

### B4. Tighten the system prompt to forbid premature RELEASE

**File:** `assignment2_part3/config/system_prompt.txt`
**Where:** line 65 already says "When your scoped work is finished,
post RELEASE …". Add one explicit sentence at line 65–66:

> "Do not post RELEASE in the same exchange as your CLAIM unless a
> successful create_file/edit_section/replace_text observation for that
> path has already been returned. RELEASE without a successful write
> tool observation will be rejected by the runtime and you will be
> reprompted."

This makes the runtime check in B1 line up with the model's stated
contract instead of being a surprise.

### B5. Tests

**Files:** `assignment2_part3/tests/test_peer_task.py`,
`assignment2_part3/tests/test_claims.py`,
`assignment2_part3/tests/test_group_chat.py`

Add:

- `test_peer_task.py` — new test that drives `run_peer_task` with a
  fake `chat_fn` that returns first a CLAIM (final), then on the
  continuation returns a RELEASE (final). Assert the runtime injects the
  release-without-write reprompt and bumps the step count, instead of
  treating RELEASE as the final answer.
- `test_claims.py` — assert `release()` populates
  `_released_unsatisfied` when not satisfied, and does NOT populate it
  when `mark_satisfied` has been called for the same key. Assert the
  window expiry.
- `test_group_chat.py` — assert `_stale_claim_guidance` returns a
  released-without-write nudge when only the recently-released list is
  non-empty.

All must be deterministic (no live LLM). Follow the existing pattern in
`test_peer_task.py` of providing `chat_fn` as a function returning
canned JSON strings.

---

## Critical files

| File | Why |
|------|-----|
| `assignment2_part3/peer_task.py` (lines 380–542) | Per-round loop; needs the new RELEASE-without-write reprompt branch and the `saw_successful_shared_write` flag |
| `assignment2_part3/claims.py` (lines 134–222) | `release()` + new `_released_unsatisfied` tracking |
| `assignment2_part3/group_chat.py` (lines 124–193, 288–333, 434–474) | `_claim_continuation_message`, `_stale_claim_guidance`, `_run_task_for_message`, `_continue_claims` — the whole continuation path |
| `assignment2_part3/config/system_prompt.txt` (lines 53–66) | P3.9 claim/defer wording — add the explicit "no premature RELEASE" rule |
| `assignment2_part3/tests/test_peer_task.py`, `test_claims.py`, `test_group_chat.py` | New tests for the three regression cases |

## Reuse opportunities

- `RELEASE_PATTERN`, `CLAIM_PATTERN`, `split_claim_target` already exist
  in `claims.py` — import them, don't rewrite.
- `_looks_like_write_success_claim` in `peer_task.py:265` is a good
  template for the new tool-observation success detector (B2).
- `mark_satisfied` / `unsatisfied_claims_for` already exist in
  `ClaimRegistry` but are not currently called from the live runtime —
  this fix is partly "actually wire up the satisfaction tracking that
  was already designed."

## Verification

1. **Unit suites stay green:**
   ```bash
   python -m pytest assignment2_part2 -q
   python -m pytest assignment2_part3/tests -q
   ```
   Part 3 changes routinely regress Part 2 — both suites must pass.

2. **End-to-end repro with the local hub:**
   ```bash
   cd assignment2_part3
   docker compose build agent          # picks up code changes
   docker compose up -d
   ```
   Open the four-terminal layout. In chat, paste exactly the user's
   original message:

   > `@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. First, each state agreement on signatures: add(a, b), subtract(a, b), multiply(a, b), divide(a, b). Then split work: alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM with the function names in the scope, e.g. #add-subtract and #multiply-divide. Write pytest tests next to it.`

   Success criteria:
   - `workspace/shared/calculator.py` exists with all four functions.
   - `workspace/shared/test_calculator.py` exists and `run_tests`
     passed for it (visible in `audit.py tail --kind tool`).
   - Audit shows `claim_release_without_write_reprompt` events firing
     when an agent tries the broken pattern, and the agent recovers
     within the same turn.

3. **Audit replay for the trace:**
   ```bash
   python tools/audit.py traces -n 5
   python tools/audit.py trace <id-of-first-build-message>
   ```
   Expected: CLAIM → tool(create_file) → RELEASE, in that order, for
   each agent. No bare CLAIM→RELEASE pair without a tool in between.

4. **Smoke the operator console.** After the demo, confirm `:budget`,
   `:pause`, `:resume`, `:say` still work and that the release-tracking
   state in `data/budget_<agent>.json` is unchanged (B3 adds in-memory
   state only — no schema change).
