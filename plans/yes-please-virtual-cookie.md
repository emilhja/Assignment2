# Make agents run pytest before declaring "done"

## Context

In multi-agent demos, agents stop after one CLAIM → write cycle and leave testing
to the operator. Recent transcript:

- alice wrote `/workspace/shared/calculator.py` + tests and replied
  *"Ready to run the tests"* without calling `run_tests`.
- bob wrote tests against undefined functions, hit a red run, and reported "done"
  with `Tests: ran and failed. Blockers: functions multiply and divide are not defined.`

In both cases the operator had to type "everyone are you done" / "run the tests"
to push the agents forward. The system prompt already says reports should
include "tests run" (`config/system_prompt.txt:48`) and bans claiming success
without a `run_tests` observation (`:85`), but there is no enforcement when an
agent *writes* a shared test file and then emits a `final` answer without
ever running pytest in the same turn.

Goal: close the gap with two complementary layers so the operator does not
have to nudge.

- **Layer A — prompt norm**: tell the model to call `run_tests` itself after a
  shared-test-file write. Free and covers the happy path.
- **Layer B — runtime fallback**: when Layer A drifts, the runtime injects ONE
  reprompt forcing `run_tests` on the just-written test file, then accepts
  whatever comes back. Deterministic safety net.

Out of scope: the separate "operator says `run the tests` and both agents
DEFER" failure mode — that one is reply-policy/coordination shaped, not
covered here.

## Layer A — system prompt norm

**File:** `assignment2_part3/config/system_prompt.txt`

Add one new bullet to the Claim/defer protocol block (between the existing
RELEASE-without-write rule on line 67 and the "only report ... after a
successful ... observation" rule on line 68). Text:

> After a successful create_file/append_text/edit_section/replace_text
> observation for a shared test file under `/workspace/shared/` (any
> `test_<name>.py` or `<name>_test.py`), your next response must be a
> `run_tests` tool_call on that exact path — not a final answer. Only after a
> `run_tests` observation in this round may you emit a final answer; that
> final answer must report `Tests: ran and passed` or `Tests: ran and failed`
> with the exit code, not `Tests: not run` or `Ready to run`.

This sits next to existing rules that talk about post-write obligations, so
the model picks it up without re-reading unrelated sections.

## Layer B — runtime fallback nudge

**File:** `assignment2_part3/peer_task.py`

Reuse the existing nudge machinery (`_continuation_reprompt_or_stop`,
`peer_task.py:515-540`) — same shape as the four existing branches
(`peer_task.py:650-773`), bounded by
`MAX_CONTINUATION_REPROMPTS_PER_REASON` (one reprompt per turn is enough
since `run_tests` is a one-shot tool call).

### B.1 Per-turn tracking

Inside `run_peer_task`, alongside `saw_successful_shared_write` /
`saw_failed_shared_write` (`peer_task.py:511-513`), add two new locals:

- `written_test_paths: set[str]` — populated when a shared write succeeds and
  the `path` arg matches a test-file pattern.
- `ran_test_paths: set[str]` — populated when `run_tests` returns an
  observation that does not start with the failure markers handled by
  `_looks_like_failed_write` (`peer_task.py:259`).

Both are written in the existing tool-dispatch block at
`peer_task.py:818-844`, right after `_log("tool", ...)`:

- For `parsed.tool in CLAIM_GATED_TOOLS` with a `/workspace/shared/...` path
  that matches the test-file regex *and* the observation is **not** a failed
  write → `written_test_paths.add(path)`.
- For `parsed.tool == "run_tests"` with a `/workspace/shared/...` path and a
  non-blocked observation → `ran_test_paths.add(path)`.

### B.2 Test-path detection helper

Add a small module-private helper at the top of `peer_task.py` (next to
`_test_target_for_claim`, `peer_task.py:301`):

```python
def _is_shared_test_path(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith(SHARED_PATH_PREFIX):
        return False
    if not path.endswith(".py"):
        return False
    _dir, _sep, filename = path.rpartition("/")
    stem = filename[:-3]
    return stem.startswith("test_") or stem.endswith("_test")
```

Mirrors the rule already encoded in `coordination.SHARED_TEST_PATH_PATTERN`
(`coordination.py:31`) and `coordination._test_path_for_source`
(`coordination.py:140-147`). A local copy is cleaner than importing — keeps
`peer_task.py` self-contained for this check, matches the style of the
existing `_PYTEST_REQUEST_RE` / `_pytest_was_requested` helpers
(`peer_task.py:294-298`).

### B.3 Nudge branch

In the `parsed.kind == "final"` block (`peer_task.py:648`), insert a new
branch **before** the generic final-answer return at line 774, and **after**
the existing RELEASE-without-write branch at lines 753-773 (so test-running
takes priority over RELEASE):

```python
pending_test_runs = written_test_paths - ran_test_paths
if pending_test_runs:
    pending_path = sorted(pending_test_runs)[0]
    guidance = (
        "You wrote a shared test file in this turn but never called "
        f"run_tests on it. Emit exactly this tool_call now and wait for the "
        "observation before any final answer: "
        '{"type":"tool_call","tool":"run_tests","args":{"path":"'
        f'{pending_path}"' "}}. "
        "After the observation, your final answer must report "
        "`Tests: ran and passed` or `Tests: ran and failed` with the exit "
        "code — not `Tests: not run` or `Ready to run`."
    )
    stopped = _continuation_reprompt_or_stop(
        "shared_test_write_without_run_reprompt",
        guidance,
        "I had to stop because I wrote tests but never ran them.",
    )
    if stopped is not None:
        return stopped
    continue
```

Notes:

- Fires for both claim-continuation and regular turns. All shared writes
  *currently* require a claim continuation (the `CLAIM_GATED_TOOLS` block at
  `peer_task.py:810-816` rejects shared writes without an active claim), so
  in practice this branch only triggers inside continuations — but not
  gating on `_is_claim_continuation(message)` keeps it correct if that ever
  loosens.
- `BudgetExceeded` is already handled at `peer_task.py:566-597` and exits
  before we reach the `final` block, so no extra guard needed.
- One nudge per turn (`MAX_CONTINUATION_REPROMPTS_PER_REASON = 2` lets two,
  but the second nudge would be on the same key after the model ignored the
  first — acceptable upper bound; matches existing branches' tolerance).
- Cleared by a successful `run_tests`: if the model nudged-into-compliance
  calls `run_tests`, `ran_test_paths` picks up the path and
  `pending_test_runs` becomes empty on the next `final`.

## Tests

**File:** `assignment2_part3/tests/test_peer_task.py`

Add one new test, mirroring
`test_successful_write_can_reprompt_to_claim_pending_tests`
(test_peer_task.py:1189-1245) for setup style and
`test_pending_write_reprompt_tolerates_two_nudges_before_giveup`
(test_peer_task.py:1606-1674) for the max-retries shape:

- `test_shared_test_write_without_run_tests_is_reprompted_then_runs`:
  - Stub `chat_fn` to return, in order:
    1. `tool_call append_text` to `/workspace/shared/test_calc.py` (succeeds).
    2. `final` with "Ready to run the tests." (should trigger nudge).
    3. `tool_call run_tests` on `/workspace/shared/test_calc.py` (succeeds,
       green).
    4. `final` with "Tests: ran and passed.".
  - Assert `shared_test_write_without_run_reprompt` appears in
    `_events(store)` exactly once.
  - Assert the returned reply is the final from step 4, not the stalled one
    from step 2.

Optional second test (nice-to-have, not blocker): writing to
`/workspace/shared/calculator.py` (non-test path) does NOT trigger the
nudge, to lock in the test-path-only behavior.

## Files touched

1. `assignment2_part3/config/system_prompt.txt` — one new bullet between
   lines 67-68.
2. `assignment2_part3/peer_task.py`:
   - New `_is_shared_test_path` helper near line 301.
   - Two new locals in `run_peer_task` near line 513.
   - Two tracking-set updates in the tool-dispatch block near line 836.
   - New nudge branch in the `final` block between lines 773 and 774.
3. `assignment2_part3/tests/test_peer_task.py` — one new test
   (optionally two).

## Verification

```bash
# 1. Unit suite (Part 3) — new test passes, existing reprompt tests stay green.
python -m pytest assignment2_part3/tests -q

# 2. Part 2 regression suite (Part 3 changes have regressed Part 2 before).
python -m pytest assignment2_part2 -q

# 3. End-to-end smoke in the 4-terminal docker setup.
cd assignment2_part3
docker compose build agent          # picks up peer_task.py + system_prompt.txt
docker compose up -d
python tools/chat.py say --as emil-user \
  "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. \
   alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM with \
   the function names in the scope. Write pytest tests next to it."
python tools/chat.py live --as emil-user
# Expect both agents to report `Tests: ran and passed` (or `ran and failed`
# with an exit code) WITHOUT the operator typing "are you done" or "run the
# tests".

# 4. Audit trace — confirm the new reprompt kind fires when the model drifts.
python tools/audit.py tail --agent alice --kind shared_test_write_without_run_reprompt
python tools/audit.py tail --agent bob   --kind shared_test_write_without_run_reprompt
```
