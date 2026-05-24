# Calculator session post-mortem: pytest miss + missing completion signal

## Context

User ran a multi-agent calculator build (alice owns add/subtract, bob owns multiply/divide, "Write pytest tests next to it"). Outcome on disk:

- `workspace/shared/calculator.py` only contains bob's `multiply` + `divide`.
- `workspace/shared/archive/` has an older copy with all four functions and a `test_calculator.py` (unittest, not pytest) — leftover from an earlier session.
- Operator had to ping "who writes pytest?" and "are you done?" and never got a definitive answer.

User wants (a) a log-based diagnosis of why pytest never got written, and (b) a recommendation on whether/how to add an "All done, pytest passed" confirmation step.

## Issue 1: why pytest was never written

The pytest guidance machinery is wired up correctly but never reached. Three layers conspired:

### 1a. Pytest sidecar guidance does fire on the initial coordinator message

`coordination._pytest_sidecar_guidance` (`assignment2_part3/coordination.py:128`) is called from `assignment_guidance` (`coordination.py:141`) and DOES fire on the original prompt — the path regex matches `/workspace/shared/calculator.py.`, both `owns` patterns match, and `"pytest"` is present. So each agent's first inbound message contains: *"Pytest coverage was requested next to the shared file. After completing the implementation write, use a separate CLAIM for /workspace/shared/test_calculator.py#<scope>-tests…"*.

This guidance is conditional on **first completing the implementation write**. That precondition was never satisfied.

### 1b. Agents stalled in the "pending write" reprompt loop before reaching the test phase

From the audit logs, both alice and bob hit `claim_continuation_giveup` with message *"I had to stop because I kept describing the write instead of using a write tool."* This message is produced by `peer_task._continuation_reprompt_or_stop` at the `claim_continuation_pending_write_reprompt` branch (`peer_task.py:689`).

Flow that triggered the giveup:
1. Agent posts `CLAIM /workspace/shared/calculator.py#add-subtract: ...`
2. `group_chat._continue_claims` (`group_chat.py:465`) synthesizes a runtime continuation message: *"Continue the active shared-file claim … Use tools now; do not post another CLAIM."*
3. Model responds with prose like *"I will now write the add and subtract functions"* — caught by `_looks_like_pending_shared_write` (`peer_task.py:312`).
4. Runtime reprompts once. Model repeats the pattern. `MAX_CONTINUATION_REPROMPTS_PER_REASON = 1` (`peer_task.py:38`) → giveup.

The pytest claim-after-implementation guidance at `peer_task.py:660` (`claim_continuation_pending_tests_reprompt`) is gated on `saw_successful_shared_write`. Alice never had a successful shared write, so she never even reached the test-claim nudge. Bob did complete `multiply`/`divide`, but his test-claim attempt fell into a parser-rejected `edit_section` + a `budget_exceeded` event in the same minute window, blocking the retry.

### 1c. Budget throttling amplified the failure

Both agents hit ≥8 `budget_exceeded` events across the session (TPM cap). Bob's one successful test-write attempt landed in a minute where the TPM bucket was already full, so the retry was suppressed.

### Root cause summary

- **Proximate**: the single-reprompt budget (`MAX_CONTINUATION_REPROMPTS_PER_REASON = 1`) is too tight for "describe instead of call" — a very common LLM failure mode. One nudge isn't enough.
- **Contributing**: pytest test-writing is a *secondary* claim that requires the *primary* (implementation) write to succeed first. When the primary write fails, the entire test-writing branch is silently skipped — no observable "tests were skipped because impl failed" event.
- **Contributing**: TPM budget exceedance has no graceful "wait and retry" path; it just hard-stops the round.

## Issue 2: no "all done" confirmation signal

There is no completion protocol. After an agent's final answer, the runtime sends the text to the hub and the loop continues. There is no:

- Structured `{"type": "completion", "scope": "...", "tests_passed": true}` event.
- Auto-run pytest after a shared write (Part 2 has this in `agent.py:310-337`, Part 3 deliberately removed it — see `peer_task.py:1-5`).
- Operator-side aggregator that prints "all assigned scopes complete + tests green".

The system prompt (`config/system_prompt.txt:48`) tells agents to "publish a short summary: files changed, tests run, any blockers" — but enforcement relies entirely on the model. When the model gives up or hits budget, no summary is sent and the operator has to poll.

## Recommended fix (minimal, two-part)

### Part A — make implementation writes more robust (fixes pytest miss)

1. **Raise `MAX_CONTINUATION_REPROMPTS_PER_REASON` from 1 → 2** in `peer_task.py:38`. Two nudges is still bounded but gives the model a real chance to course-correct after "describe instead of call".
2. **Add a stronger continuation reprompt for the pending-write case**: when `_looks_like_pending_shared_write` triggers, append a concrete example of the JSON tool call the model should have emitted (e.g. *"Emit exactly: `{\"type\":\"tool_call\",\"tool\":\"create_file\",\"args\":{\"path\":\"/workspace/shared/calculator.py\", ...}}`"*). Models recover faster from a concrete template than a paraphrased rule. Edit `peer_task.py:681-688`.
3. **Log a `pytest_skipped_due_to_impl_failure` event** when the implementation write fails on a turn that had pytest guidance — so the operator can see in `audit.py` that test work was dropped, not just impl work. Edit `peer_task.py` around the `claim_continuation_giveup` path.

### Part B — add a lightweight completion signal (answers "shouldn't I get confirmation?")

Two options, in order of effort:

**Option B1 (small, recommended):** Treat operator messages matching `/\b(are you done|status|finished\?|done\?)\b/i` as a special "status request". When matched, inject a runtime guidance string that tells the agent: *"The operator is asking for completion status. Respond with exactly: 'Done: <scope> implemented at <path>. Tests: <ran/not run>. Blockers: <none/...>'. If you have not run tests yet, call `run_tests` on the test file first."* This costs one regex + one guidance string in `coordination.py`, no protocol changes.

**Option B2 (larger, can defer):** Introduce a structured `STATUS` protocol line analogous to `CLAIM`/`RELEASE`. Agents post `STATUS <target>: done|in-progress|blocked tests=passed|failed|not-run` after each scope completion. `tools/audit.py` and `tools/chat.py` can render a per-scope status board so the operator sees who's done without asking. Adds a new pattern in `claims.py`, a new event kind, and a small renderer in `tools/chat.py live`.

Given user said "this went quite well" — recommend **B1 only** for now. B2 is a candidate for a follow-up plan if the status-board ergonomic becomes worth the protocol surface.

## Files to modify

| Path | What changes |
|------|--------------|
| `assignment2_part3/peer_task.py` | Raise `MAX_CONTINUATION_REPROMPTS_PER_REASON` to 2; strengthen pending-write reprompt with concrete JSON example; log `pytest_skipped_due_to_impl_failure` when applicable. |
| `assignment2_part3/coordination.py` | Add `STATUS_REQUEST_PATTERN` + a new `status_request_guidance(text, ...)` returning the "respond with Done: …" template. |
| `assignment2_part3/group_chat.py` | Call `status_request_guidance` from `_run_task_for_message` (alongside the other guidance hooks at lines 320-345). |
| `assignment2_part3/tests/test_peer_task.py` | New test: pending-write reprompt now allows 2 nudges before giveup. |
| `assignment2_part3/tests/test_coordination.py` | New test: `status_request_guidance` returns expected template for "are you done?" / "status?" / "done yet?". |

## Verification

End-to-end (Docker hub, the layout in CLAUDE.md):

1. Rebuild: `cd assignment2_part3 && docker compose build agent`.
2. Wipe shared state: remove `workspace/shared/calculator.py` + `test_calculator.py` (keep archive).
3. Bring up `docker compose up -d` and attach both agents.
4. From a fourth terminal, replay the original prompt (`tools/chat.py say --as emil-user "…"`).
5. Expected: both agents post CLAIM, then write implementation, then post a *second* CLAIM for the test file scope, then write tests.
6. Send `tools/chat.py say --as emil-user "@alice-swe @bob-swe status?"` — expected: each replies with the `Done: ... Tests: ... Blockers: ...` template.
7. Audit: `python tools/audit.py traces -n 10` should show `claim_continuation_pending_write_reprompt` at most twice per agent before tools fire.

Unit tests (deterministic, no real provider):

- `python -m pytest assignment2_part3/tests/test_peer_task.py -q`
- `python -m pytest assignment2_part3/tests/test_coordination.py -q`
- `python -m pytest assignment2_part2 -q` (regression — Part 2 must still pass).

## What I deliberately did not include

- Auto-running pytest after every shared write (the Part 2 hook). Part 3 removed it on purpose because shared writes don't necessarily target tests the writer cares about. Reintroducing it would re-create the noise the removal was meant to fix.
- A full status board UI in `tools/chat.py live`. Belongs in a separate plan if B1 isn't enough.
- Lowering budget caps or changing the TPM accounting. Budget exceedance was a contributing factor, not the root cause, and tweaking it has session-wide blast radius.
