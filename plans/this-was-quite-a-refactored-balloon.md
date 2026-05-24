# Polish run #2: close the two gaps the calculator session exposed

## Context

User reran the multi-agent calculator build (alice owns add/subtract, bob owns multiply/divide, "write pytest next to it"). Outcome on disk now:

- `workspace/shared/calculator.py` has all four functions ✅
- `workspace/shared/test_calculator.py` has add/subtract/multiply/divide tests + edge cases ✅

So the run did finish — the staged changes from `i-think-this-went-serene-manatee.md` (release-without-write reprompt, pending-tests reprompt, raised reprompt budget to 2, status-request guidance, higher TPM/total) all earned their keep on the *recovery* path.

But: alice **stalled silently** on the very first continuation, and the operator had to ping twice ("are you done?" and "have you implemented?") to get her unstuck. Two distinct gaps caused that.

## Findings from the trace (session 2026-05-24T18:24–18:33, msgs 1 / 11 / 13)

### Gap A — pending-write detector misses the "I need to re-read…" stall

`assignment2_part3/peer_task.py:323-347` `_looks_like_pending_shared_write` requires **both** a pending marker (`"i need to"`, `"i'll"`, …) **and** a write verb (`create | write | implement | add | update | edit`).

Alice's first continuation final at 18:25:33 was:

> `"I need to re-read /workspace/shared/calculator.py"`

That has the prefix and the pending marker, but `re-read`/`read` are not write verbs → detector returns False → the answer falls through as a benign final, gets scrubbed and sent. The recursive `_continue_claims` (`group_chat.py:478-522`) sees no CLAIM in the answer, the recursive call returns, and the continuation dies. **No `claim_continuation_pending_write_reprompt`, no `claim_continuation_giveup`, no audit event** — alice just goes quiet.

Three minutes later the operator pings and the recovery path kicks in.

### Gap B — `_stale_claim_guidance` and `status_request_guidance` collide

`group_chat.py:343-356` appends both guidance strings to `runtime_guidance` in order. When the operator asks "are you done?" while alice still has an unsatisfied CLAIM (the leftover from Gap A), alice receives:

1. `status_request_guidance` → "reply in this shape: `Done: … Tests: … Blockers: …`"
2. `_stale_claim_guidance` → "either complete the write … **or post `RELEASE <target>`** to give it up. Do not re-post the same CLAIM."

Alice took the simpler RELEASE option (18:28:39, msg 11). That's not what the operator asked for — they wanted a status reply. The two guidance strings are individually correct but collectively push toward the wrong answer.

### Gap C (audit-only, low priority) — silent continuation death

When `_continue_claims` exits because the answer carried no new CLAIM and no tool was called, there is no audit event recording "continuation ended without progress". That's what made Gap A invisible until I cross-referenced timestamps. A single `claim_continuation_ended_without_progress` log line at the existing return paths in `_continue_claims` would have made the stall self-explanatory.

## Recommended fix (small, three-part)

### Part A — broaden the pending-write detector

In `peer_task.py:323-347` `_looks_like_pending_shared_write`, treat *any* prose final on a shared-claim continuation as a stall when there is no successful write yet. The current write-verb gate exists to avoid false positives on legitimate finals like DEFER/RELEASE, but those branches are already handled *before* this check (`peer_task.py:736-756` RELEASE branch, plus the existing DEFER paths). So the safer rewrite is:

- Keep the existing marker list, **add** `"re-read"`, `"reread"`, `"look at"`, `"review"`, `"read"`.
- Drop the write-verb conjunction (any pending marker + the shared prefix is enough).
- Leave RELEASE/DEFER/repeat-CLAIM detection where it is (they short-circuit earlier in the same loop, so they won't fall through to this branch).

This is narrow enough that the existing test `test_claim_continuation_reprompts_declarative_missing_file_then_writes` and friends still pass; the new test asserts that "I need to re-read …" triggers the same reprompt path.

### Part B — make status guidance win over stale-claim guidance

In `group_chat.py:343-356`, when `status_request_guidance` returns a non-None string, **either** skip `_stale_claim_guidance` entirely **or** swap its body for a softer variant that says "include open claims in the `Blockers:` field; do not RELEASE just because status was asked." The cleanest implementation: compute `status_guidance` first, and if it's non-None, pass the unsatisfied claims into `status_request_guidance` so the template line gets concrete blocker text. Then skip the separate `_stale_claim_guidance` append for that turn.

Signature change to `coordination.status_request_guidance`: add `open_claim_targets: list[str] | None = None`. If provided, append a sentence telling the agent to list those targets in the Blockers field.

### Part C — audit visibility for silent continuation death

In `group_chat.py:478-522` `_continue_claims`, add one `_log(store, "claim_continuation_ended_without_progress", …)` at the point where the recursive call would return because `_claimed_targets(answer)` is empty AND the agent still holds the original claim AND no successful shared write happened for it. This is observability only — no behavior change.

(Tracking "no successful shared write" cleanly across the recursion needs the existing `claims.recently_released_unsatisfied_for` or a quick check against `claims.own_claim_for_write` at the recursion boundary. Both already exist.)

## Files to modify

| Path | What changes |
|------|--------------|
| `assignment2_part3/peer_task.py` | Broaden `_looks_like_pending_shared_write` per Part A. |
| `assignment2_part3/coordination.py` | Extend `status_request_guidance` to take `open_claim_targets` and weave them into the Blockers template line. |
| `assignment2_part3/group_chat.py` | When `status_request_guidance` returns non-None, suppress the separate `_stale_claim_guidance` append for that turn and pass the open claim targets into the status helper. Add `claim_continuation_ended_without_progress` audit log in `_continue_claims`. |
| `assignment2_part3/tests/test_peer_task.py` | New test: `_looks_like_pending_shared_write` triggers for "I need to re-read …" and the runtime reprompts then writes. Mirror `test_claim_continuation_reprompts_declarative_missing_file_then_writes` (`tests/test_peer_task.py:935`). |
| `assignment2_part3/tests/test_coordination.py` | New test: `status_request_guidance` with `open_claim_targets=["…#add-subtract"]` returns guidance that mentions the target in the Blockers sentence. |
| `assignment2_part3/tests/test_group_chat.py` *(if exists, else inline)* | New test: when both status request + stale claim are active, `_stale_claim_guidance` is suppressed and the status template carries the claim into Blockers. |

## Verification

End-to-end (Docker hub, same layout as CLAUDE.md):

1. `cd assignment2_part3 && docker compose build agent` then `docker compose up -d`.
2. Wipe `workspace/shared/calculator.py` and `test_calculator.py`.
3. Replay original prompt: `python tools/chat.py say --as emil-user "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. alice owns add/subtract, bob owns multiply/divide. Write pytest next to it."`
4. Expected: both agents post CLAIM → continuation → write impl → CLAIM tests → continuation → write tests **without any operator ping**.
5. If alice still drifts into "I need to re-read", expected audit event sequence: `claim_continuation_pending_write_reprompt` → tool_call read_file → tool_call append_text. No silent gaps.
6. Send `python tools/chat.py say --as emil-user "@alice-swe @bob-swe are you done?"`. Expected: each agent replies with the `Done: … Tests: … Blockers: …` template, *without* a stray RELEASE.

Unit tests (deterministic):

- `python -m pytest assignment2_part3/tests/test_peer_task.py -q`
- `python -m pytest assignment2_part3/tests/test_coordination.py -q`
- `python -m pytest assignment2_part3/tests/test_group_chat.py -q` *(if added)*
- `python -m pytest assignment2_part2 -q` — regression guard.

Audit replay:

- `python tools/audit.py traces -n 5` — confirm continuation traces have no silent gaps.
- `python tools/audit.py tail --agent alice --kind claim_continuation_ended_without_progress -n 5` — should be empty after the fix.

## What I deliberately did NOT include

- **A structured `STATUS` protocol line** (deferred Option B2 from the prior plan). The status-request guidance template is enough; a new protocol surface is overkill until the operator says they want a status board.
- **Auto-running pytest after every shared write.** Still the right call to keep this off — Part 2 has it, Part 3 deliberately doesn't (see `peer_task.py:1-13`).
- **Lowering the new TPM/total caps.** They were raised in this session and helped — leave alone.
- **Touching `_looks_like_pending_test_work`.** It worked correctly in this run (fired on msg 13's post-impl prose and got alice to CLAIM the test file).
- **A retry/backoff on `BudgetExceeded`.** No budget exceedances showed up in this trace; the higher caps absorbed the load. Revisit only if a future run hits the wall again.
