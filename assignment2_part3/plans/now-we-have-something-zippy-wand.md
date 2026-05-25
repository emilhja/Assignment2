# Make agents self-verify with pytest before declaring Done

## Context

In the latest live calculator session both agents wrote shared files but neither ran the tests, then both reported `Tests: not run` even after the operator broadcast `everyone are you done?`. The operator should not have to ask, and "Done" should not be reported while the tests sitting next to the code are still unverified.

Part 3 inherits Part 2's `run_tests` tool through `part2_bridge.py` (registered at `tools.py:511`), but `peer_task.py` was deliberately stripped of Part 2's post-edit auto-pytest hook (see comment block at `peer_task.py:1-13`). The system prompt at `config/system_prompt.txt:85` even instructs the model to *say* "tests were not run" when a `run_tests` observation is missing — which is honest but maps directly to the bad outcome we saw.

`coordination.py` already injects a pytest-sidecar nudge for the impl write (`_pytest_sidecar_guidance`, lines 150–174) and a structured status format (`status_request_guidance`, lines 370–414). What is missing is **runtime enforcement**: the claim-continuation loop in `peer_task.py:run_peer_task` accepts a final answer as soon as a shared write succeeds, even when the original request asked for pytest coverage and the test file was never executed.

Goal: when the inbound peer message asks for pytest coverage and the agent completes a shared write during a claim continuation, refuse the final answer until either (a) a successful `run_tests` observation exists in this turn, or (b) the configured reprompt cap is hit.

## Design

### 1. `peer_task.py` — enforce `run_tests` before final answer in claim continuations

Mirror the existing `saw_successful_shared_write` machinery for tests, then add a new reprompt branch alongside `_looks_like_pending_test_work` (currently at lines 688–714).

- **New module-level helper** `_run_tests_path_for_target(target: str | None) -> str | None`: returns the bare `/workspace/shared/test_<stem>.py` path (no `#scope` suffix), reusing `split_claim_target` and the same stem/`test_` logic already in `_test_target_for_claim` (lines 301–312). Used to suggest the exact `run_tests` arg.
- **New detector** `_looks_like_done_without_tests(answer: str) -> bool`: true when the answer contains `"Done:"` or `"tests:"` together with either `"not run"`, `"ran no tests"`, or `"have not run"` (case-insensitive). Returns false if the answer also contains `"ran and passed"`, `"ran and failed"`, `"DEFER"`, or `"RELEASE"` (those are legitimate continuation exits).
- **New per-turn flag** `saw_successful_test_run = False`, initialized next to `saw_successful_shared_write` (line 512). Set to `True` when `parsed.tool == "run_tests"` and the observation does not start with `"Tool error:"`, `"Command exited with code 124"` (timeout), or `"Blocked by safety check:"`. Note: a failing pytest still counts as "ran" — exit code ≠ 0 is information, not a missing run.
- **New reprompt branch** in the `parsed.kind == "final"` path, *after* the existing `_looks_like_pending_test_work` block, gated by `_is_claim_continuation(message) and _pytest_was_requested(message.text) and saw_successful_shared_write and not saw_successful_test_run`. If the final answer matches `_looks_like_done_without_tests`, call `_continuation_reprompt_or_stop("claim_continuation_pytest_required_reprompt", guidance, fallback)`.

  Guidance text:
  > "A successful shared-file write happened in this continuation and the original request asked for pytest coverage, but no `run_tests` observation exists yet in this round. Do not report Done with `Tests: not run`. Call `run_tests` now with `{\"path\": \"<test_path>\"}` and only emit the final answer after the observation. Report the pytest result honestly: `Tests: ran and passed` on green, or `Tests: ran and failed` followed by the first failure line on red."

  Fallback (used after `MAX_CONTINUATION_REPROMPTS_PER_REASON` retries):
  > "I had to stop because I kept reporting Done without running the requested pytest verification."

  `<test_path>` comes from `_run_tests_path_for_target(_claim_continuation_target(message))`, falling back to `_latest_shared_test_path(recent_context)` (already exported from `coordination.py` lines 275–290) and finally to a generic "the shared test file" string.

This reuses the existing `_continuation_reprompt_or_stop` rate-limiter so the loop can't run forever, and it logs a `claim_continuation_giveup` event for `tools/audit.py` to surface stalls.

### 2. `coordination.py` — proactive verification instruction

Extend `_pytest_sidecar_guidance` (lines 150–174) so the second branch (the one that already covers peer-collision/import-order risk) also appends:

> "After the test-file write succeeds, call `run_tests` with `{\"path\": \"<test_path>\"}` in the same continuation before sending any final answer. Your Done line must report `Tests: ran and passed` or `Tests: ran and failed` with the first failure summary — `Tests: not run` is not acceptable when pytest coverage was requested."

Also append a shorter version of the same instruction to the single-agent base branch (no peer collision) so an agent that owns both impl and tests sees it too.

Update `status_request_guidance` (line 393–397): when `test_path` is available AND the recent context contains a successful `run_tests` event for that path, drop the "call run_tests first" hint. (Optional polish — non-blocking.)

### 3. Tests — `assignment2_part3/tests/test_peer_task.py`

Add four new cases that drive `run_peer_task` with a scripted `chat_fn` (same pattern used elsewhere in the file):

1. **Pytest reprompt fires after impl write without verification.** Continuation message asks for pytest, scripted model writes the file successfully then returns `{"type":"final","answer":"Done: ... Tests: not run. Blockers: none."}`. Assert the reprompt is appended to messages and the loop calls the model again.
2. **No reprompt when `run_tests` already ran.** Same setup but the script inserts a `run_tests` tool_call (with a green observation) before the Done final. Assert the final answer is returned as-is.
3. **No reprompt when pytest was not requested.** Inbound continuation text omits the word "pytest"/"test". Assert the Done-without-tests answer passes straight through.
4. **Giveup after the reprompt cap.** Script returns `Done: ... Tests: not run` three times. Assert the function returns the configured fallback string and logs `claim_continuation_giveup`.

`run_tests` observations in the scripted tools observation can be simple stubs since `_run_tool_with_approval` is monkey-patchable via the existing test harness.

## Critical files

- `assignment2_part3/peer_task.py` — new detector, flag, and reprompt branch around lines 512, 297–312, and 688–714.
- `assignment2_part3/coordination.py` — `_pytest_sidecar_guidance` extension at lines 150–174 (and optional `status_request_guidance` tweak).
- `assignment2_part3/tests/test_peer_task.py` — four new cases.

## Reuse (do not duplicate)

- `_pytest_was_requested` (peer_task.py:294-298) — already gates pytest-related logic.
- `_test_target_for_claim` (peer_task.py:301-312) — derive the new `_run_tests_path_for_target` from this.
- `_continuation_reprompt_or_stop` (peer_task.py:515-540) — the rate-limited reprompt mechanism with built-in giveup logging.
- `_latest_shared_test_path` (coordination.py:275-290) — fallback test-path resolver from recent context.
- `MAX_CONTINUATION_REPROMPTS_PER_REASON = 2` (peer_task.py:42) — reuse the same cap; don't introduce a new constant.

## Verification

1. **Unit tests**: `python -m pytest assignment2_part3/tests -q`. The four new test cases plus the existing suite must pass. Run Part 2 too (`python -m pytest assignment2_part2 -q`) since Part 3 changes occasionally regress the bridge.
2. **Replay the calculator session live**:
   ```
   cd assignment2_part3
   docker compose build agent
   docker compose up -d
   python tools/chat.py say --as emil-user "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. First, each state agreement on signatures: add(a, b), subtract(a, b), multiply(a, b), divide(a, b). Then split work: alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM with the function names in the scope, e.g. #add-subtract and #multiply-divide. Write pytest tests next to it."
   python tools/chat.py live --as emil-user
   ```
   Expected: each agent ends its claim continuation with a Done line that says `Tests: ran and passed` (or `Tests: ran and failed` with a failure line) **without the operator asking**. Operator should not need to broadcast "are you done?".
3. **Audit replay**: `python tools/audit.py traces -n 5` then `python tools/audit.py trace <id> | grep -E "pytest|run_tests|claim_continuation"` — confirm a `tool: run_tests` event lands between the successful shared write and the final `peer_reply_raw`, and the new `claim_continuation_pytest_required_reprompt` event appears in any session where the model needed nudging.

## Open assumption (flag if wrong)

When tests fail (e.g. bob's tests are red because alice's impl isn't merged yet), the agent is allowed to send `Done: ... Tests: ran and failed. Blockers: <first failure>` and exit the continuation — the runtime does not force green tests, only that pytest *was run*. If you'd prefer agents to keep retrying until green (with a separate cap), say so and I'll add that branch.
