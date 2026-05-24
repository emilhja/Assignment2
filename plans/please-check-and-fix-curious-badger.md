# Fix the DEFER ping-pong loop between scoped peers

## Context

In a live `python tools/chat.py live --as emil-user` session, alice-swe and
bob-swe were asked to split a calculator: alice owns `#add-subtract`, bob owns
`#multiply-divide`, same file. They each posted the correct scoped CLAIMs, but
then went into a tight loop:

```
13:00 bob-swe: DEFER to @alice-swe
13:00 alice-swe: DEFER to @bob-swe
13:00 bob-swe: RELEASE /workspace/shared/calculator.py#multiply-divide
13:00 alice-swe: DEFER to @bob-swe
... (repeats until both budgets exhausted)
```

Neither agent wrote any code; both hit `tokens-per-minute` budget caps and
stopped.

### Root cause (already verified in source)

The two CLAIMs are on **different scopes of the same file**, so
`claims.claims_conflict()` correctly returns False
(`assignment2_part3/claims.py:76-83`) and the runtime never refuses any write.
The system prompt at `assignment2_part3/config/system_prompt.txt:58` is
explicit: "Do not DEFER for different scopes." Despite this, the model emits a
DEFER line. That alone would be recoverable, except:

1. **`reply_policy._mentions()` treats `@alice-swe` inside `"DEFER to @alice-swe"`
   as a direct address** (`assignment2_part3/reply_policy.py:85-95, 184-186`).
   Every DEFER from peer triggers the recipient to reply, bypassing the 8s
   cooldown (`reply_policy.py:199-201`).
2. The mutual-defer guard at `peer_task.py:361-370` injects tie-break guidance
   on each round but the model still re-emits a bare `DEFER`. Guidance arrives
   too late — the reply gate already committed to a round-trip, which is what
   burns the per-minute token budget.
3. There is no rate-limit on consecutive DEFER lines from the same agent.

Fixing #1 alone breaks the loop because, without the spurious "directly
addressed" trigger, a DEFER from a peer no longer wakes the recipient. The
recipient stays quiet and uses its turn for actual scoped work.

## Recommended approach

Two surgical changes — one in `reply_policy.py`, one in
`config/system_prompt.txt`. Both fixes already-existing scope semantics; no new
abstractions.

### Change 1 — `reply_policy.py`: don't treat a DEFER line as a real mention

Add a helper that strips DEFER/RELEASE protocol lines before mention detection,
so `@<id>` inside `DEFER to @<id>` and `RELEASE` no longer count as addressing
the agent.

**File:** `assignment2_part3/reply_policy.py`

- Import `DEFER_PATTERN` and `RELEASE_PATTERN` from `claims` (extend the
  existing import block at lines 29–36).
- Add a small helper before `_mentions` (after line 84):

  ```python
  def _strip_protocol_lines(text: str) -> str:
      """Remove DEFER/RELEASE lines so their @mentions don't trigger a reply."""
      if not isinstance(text, str) or not text:
          return text
      stripped = DEFER_PATTERN.sub("", text)
      stripped = RELEASE_PATTERN.sub("", stripped)
      return stripped
  ```

- In `should_reply` (line 184), apply the stripper before the mention check:

  ```python
  if _mentions(_strip_protocol_lines(message.text), names):
      ...
  ```

  Leave `_coordinator_handoff` and `_claim_collision` operating on the full
  text — handoff lines never live inside DEFER, and collision detection must
  still see CLAIMs.

### Change 2 — `system_prompt.txt`: forbid DEFER-on-DEFER

**File:** `assignment2_part3/config/system_prompt.txt`

Insert one bullet inside the P3.9 block, after line 59 (the existing
"Reply with `DEFER to @<claimant>` and offer review." line):

```
- A peer's "DEFER to @you" line is a one-way acknowledgment, not a question. Do not reply to it with another DEFER. Continue your own non-overlapping scoped work.
```

This makes the rule explicit so the model has the same instruction the reply
gate now enforces structurally.

### Files to modify

- `assignment2_part3/reply_policy.py` (imports near lines 29–36; add helper
  after line 84; modify call at line 184)
- `assignment2_part3/config/system_prompt.txt` (insert one bullet after
  line 59)

### Files to leave alone

- `claims.py` — `claims_conflict`, `is_claimed_by_other`, and scope handling
  are already correct.
- `peer_task.py` — mutual-defer guidance is still useful as a belt-and-braces
  catch when the model emits an unsolicited DEFER on its own turn.
- `group_chat.py` — the main loop, cooldown, and claim-continuation logic are
  fine; the bug is upstream of all of them.

## Tests to update / add

`assignment2_part3/tests/test_reply_policy.py`:

1. Add a case: incoming `text="DEFER to @alice-swe"` from bob-swe to alice-swe
   → `respond=False, reason` not "directly addressed".
2. Add a case: incoming `text="RELEASE /workspace/shared/calculator.py#x"`
   without other content → `respond=False`.
3. Regression: incoming `text="@alice-swe please review my DEFER to @bob-swe"`
   (real mention plus a DEFER) → `respond=True` (the real `@alice-swe` outside
   the DEFER line still triggers reply).
4. Regression: an actual CLAIM-collision case still returns `respond=True` with
   the existing collision reason (proves the stripper didn't touch CLAIMs).

Run with: `cd assignment2_part3 && python -m pytest tests/test_reply_policy.py -v`

## Verification

1. **Unit tests:**
   `cd assignment2_part3 && python -m pytest tests/test_reply_policy.py tests/test_peer_task.py -v`

2. **Live replay** (the exact scenario that hit this bug):
   ```bash
   cd assignment2_part3
   python tools/chat.py live --as emil-user
   ```
   Then send:
   ```
   @bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py.
   Agree on function signatures in chat first (one message each), then split:
   alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM with
   the function names in the scope (e.g. #add-subtract). Write pytest tests
   next to it.
   ```
   Expected:
   - Each agent posts its scoped CLAIM exactly once.
   - No DEFER lines appear (or at most one, immediately abandoned).
   - Each agent calls `create_file` / `edit_section` for its own scope.
   - `run_tests` succeeds on `/workspace/shared/test_calculator.py`.
   - Neither agent's `tokens-per-minute` budget exhausts.

3. **Cooldown sanity** — confirm the existing `[skip] cooldown: ...` lines for
   non-mention chatter still appear (i.e., we only loosened the "mention"
   path, not the cooldown gate).
