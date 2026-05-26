# Catch prose-only "I need to" stalls outside claim-continuation

## Context

In the 2026-05-25 calculator session, alice-swe wrote `add`/`subtract` and her tests to `/workspace/shared/`, then deferred to bob and reported `Done: ... Tests: not run`. When the operator nudged her with `@alice-swe run the tests and verify implementation`, she replied prose-only:

> *"I need to re-read /workspace/shared/test_calculator.py to verify the current implementation before running the tests."*

No tool call, no `run_tests`. The reply was accepted as a `final`, the turn ended, and every subsequent operator nudge produced the same prose. Bob in the same session actually invoked `run_tests` and reported `Tests: ran and passed.`

`peer_task.py` already has a detector for exactly this pattern — `_looks_like_pending_shared_write` (peer_task.py:367–407) — and a reprompt branch at peer_task.py:789–830. The branch is gated on `_is_claim_continuation(message)`, so it only fires when the inbound is a *runtime-generated* claim continuation. Alice's inbound was a normal operator message, so the detector never fired.

The fix is to broaden the trigger: when the inbound is a non-continuation message that explicitly requests an action (e.g. "run the tests", "verify", "run pytest"), and the LLM's `final` answer is a prose stall naming a shared-workspace path without any tool call, reprompt the agent. Cap reuses the existing `MAX_CONTINUATION_REPROMPTS_PER_REASON` (= 2) so we don't loop forever and the existing `_continuation_reprompt_or_stop` closure stays the single chokepoint for reprompt budgeting.

This is intentionally narrow: it only fires when (a) the operator/peer asked for action, (b) the agent's final is prose-only, (c) the prose contains a deferral marker (`I need to`, `I'll`, `going to`, etc.) plus a `/workspace/shared/` path. It does not affect Done-with-real-pytest-result finals, RELEASE/DEFER protocol lines, or pure tool-call rounds.

## Files to modify

- `assignment2_part3/peer_task.py` — add helper + new reprompt branch
- `assignment2_part3/tests/test_peer_task.py` — add three unit tests

## Implementation

### 1. Add `_action_was_requested` helper (peer_task.py, near line 294 next to `_pytest_was_requested`)

```python
_ACTION_REQUEST_RE = re.compile(
    r"(?i)\b("
    r"run\s+(?:the\s+)?(?:tests?|pytest)"
    r"|verify(?:\s+(?:the\s+)?(?:tests?|implementation|code))?"
    r"|execute"
    r"|run\s+pytest"
    r"|go\s+ahead"
    r"|please\s+(?:run|verify|test|execute)"
    r")\b"
)


def _action_was_requested(text: str) -> bool:
    """True if inbound text contains an imperative action verb the agent should act on.

    Stricter than `_pytest_was_requested`: we only reprompt when the operator/peer
    actually told the agent to do something, not when tests are merely discussed.
    """

    return bool(_ACTION_REQUEST_RE.search(text or ""))
```

### 2. Add new reprompt branch in the `final` handler

Insert after the existing `_looks_like_pending_shared_write` branch (peer_task.py:830) and before the RELEASE branch (peer_task.py:831). The branch:

```python
if (
    not _is_claim_continuation(message)
    and _action_was_requested(message.text)
    and _looks_like_pending_shared_write(answer)
):
    guidance = (
        "The user asked you to take action (e.g. run pytest or read a file) and your "
        'reply was prose only. {"type":"final",...} replies describing what you "will" '
        'or "need to" do are invalid here — make the tool call now. For verification, '
        'call {"type":"tool_call","tool":"run_tests","args":{"path":"/workspace/shared/test_<file>.py"}}. '
        "For inspection, use read_file first. Only send a final answer after a tool observation."
    )
    stopped = _continuation_reprompt_or_stop(
        "user_action_prose_stall_reprompt",
        guidance,
        "I had to stop because I kept describing what I would do instead of calling a tool.",
    )
    if stopped is not None:
        return stopped
    continue
```

Reuses `_continuation_reprompt_or_stop` so reprompt count is bounded by `MAX_CONTINUATION_REPROMPTS_PER_REASON` and giveup is logged identically to the claim-continuation branches.

### 3. Tests (assignment2_part3/tests/test_peer_task.py)

Add three tests modeled on existing `chat_fn`-stubbed tests in this file:

- `test_user_action_request_with_prose_stall_reprompts`: inbound text `"@alice-swe run the tests and verify"`. Stub `chat_fn` to return `{"type":"final","answer":"I need to re-read /workspace/shared/test_calculator.py to verify."}` on every step. Expect at least 2 reprompts then fallback message containing `"describing what I would do"`. Assert `chat_fn` was called ≥ 3 times.

- `test_user_action_request_followed_by_tool_call_no_reprompt`: inbound `"run tests"`. Stub returns `run_tests` tool_call on step 1, then `{"type":"final","answer":"Tests: ran and passed."}` on step 2. Expect single final, no reprompt, `chat_fn` called exactly 2 times.

- `test_user_no_action_request_prose_passes_through`: inbound `"thanks alice"`. Stub returns the same `"I need to re-read /workspace/shared/test_calculator.py."` prose. Expect immediate return (no reprompt fired) since `_action_was_requested` is False. `chat_fn` called exactly 1 time.

## Verification

```bash
# unit (fast)
python -m pytest assignment2_part3/tests/test_peer_task.py -q

# part 3 + part 2 regression (Part 3 changes often regress Part 2)
python -m pytest assignment2_part3 -q
python -m pytest assignment2_part2 -q
```

End-to-end (optional, only if user wants a live repro):
1. `cd assignment2_part3 && docker compose build agent-alice agent-bob && docker compose up -d`
2. Replay: `python tools/chat.py say --as emil-user "@bob-swe @alice-swe build a calculator..."`
3. After alice posts `Done: ... Tests: not run`, send `@alice-swe run the tests and verify`.
4. Expected: alice now either invokes `run_tests` (success path) or, on persistent prose stall, emits the new fallback line after 2 reprompts — instead of looping on "I need to re-read".

## Out of scope

- The `DEFER to @bob-swe` mystery from 06:50: alice's claim was a second `test_calculator.py#add-subtract-tests` after she had already written it. Not addressed here — would be a separate "don't re-claim a satisfied scope" change in `claims.py` / `peer_task.py`.
- The Part 2 auto-pytest runner: it only runs the *internal* test suite after `create_file`/`edit_section`/`replace_text`. It does not auto-run user-written tests in `workspace/shared/`. Out of scope; agents must invoke `run_tests` themselves, and this fix makes sure they do when asked.
