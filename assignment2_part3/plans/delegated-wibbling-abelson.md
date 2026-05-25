# Why bob-swe failed the calculator demo

## Context

In the 4-terminal demo at 05:45–05:46 on 2026-05-25, bob-swe's final reply to the operator was the runtime fallback **"I could not complete this within my step budget. Please rephrase or split the task."** — even though he successfully wrote `multiply`/`divide` to `/workspace/shared/calculator.py`. This document explains exactly which steps were burned and why, plus the smallest set of fixes that would let bob finish the round (impl + tests) in future runs.

## Diagnosis: bob hit MAX_STEPS=8 mid-turn

The fallback string lives at `peer_task.py:869` and is emitted when the inner ReAct loop at `peer_task.py:564` (`for step in range(1, max_steps + 1):`) exits without a clean `final`. `MAX_CLAIM_CONTINUATION_STEPS = 8` (peer_task.py:38) is the cap for the post-CLAIM continuation turn. It is **not** configurable via env var — hardcoded constant.

### Step-by-step burn (trace `1:claim-continuation:/workspace/shared/calculator.py#multiply-divide`)

| # | LLM output | Outcome | Cost |
|---|------------|---------|------|
| 1 | `final: RELEASE …#multiply-divide` | `claim_release_without_write_reprompt` (RELEASE before any successful write) | wasted |
| 2 | `final: "I need to implement … before I can release"` | `claim_continuation_pending_write_reprompt` (talking, not doing) | wasted |
| 3 | `tool_call read_file /workspace/shared/calculator.py` | "Edit blocked: file does not exist" — alice hadn't created it yet | wasted (race) |
| 4 | `tool_call create_file …` | `claim_block` from `_maybe_shared_write_refusal` (peer_task.py:221–256): alice's `add/subtract` create landed first; bob's scoped claim can't recreate the existing shared file | wasted (race) |
| 5 | `final: "I cannot create … I will read first"` | another `claim_continuation_pending_write_reprompt` (talking again) | wasted |
| 6 | `tool_call read_file` | success | productive |
| 7 | `tool_call append_text` (multiply + divide) | success — file written ✓ | productive |
| 8 | `final: "The functions … have been implemented and appended"` | `claim_continuation_pending_tests_reprompt` — runtime asks for tests too | productive but late |
| — | (no budget left) | loop exits → fallback emitted | — |

So **3 of 8 steps were burned on vacuous "talking" finals** (#1, #2, #5) and **2 more on a race with alice** (#3, #4). Only 3 productive steps remained — enough for the implementation, not for tests.

### Why each wasted step happened

- **#1 premature RELEASE** — the model's claim-continuation handling defaulted to closing the claim without doing the work. Caught by the runtime, but cost a step.
- **#2, #5 narrating instead of acting** — the model emitted `{"type":"final", ...}` describing its next action instead of issuing the next `tool_call`. The runtime correctly re-prompted, but each re-prompt consumes a step.
- **#3, #4 race with alice** — both agents got the broadcast simultaneously. Alice's `create_file` landed in the ~2 s window between bob's CLAIM and bob's first write attempt. The `claim_block` error (peer_task.py:246–248) does hint to use `edit_section`/`replace_text`, but bob still had to spend a step on the failed `create_file` and another reading the now-existing file before he could `append_text`.

## Recommended fixes (smallest-first)

These are options, not a committed plan — pick zero or more. None of them is required to ship; the demo "worked" in the sense that the multiply/divide code did land.

### Option A — Tighten the system prompt / claim-continuation reprompt (no code-path change to budget)

In `peer_task.py` where the `claim_continuation_pending_write_reprompt` text is built, add an explicit "**Do not reply with `{\"type\":\"final\",…}`. Only `{\"type\":\"tool_call\",…}` is valid until the write succeeds.**" This directly attacks the #2/#5 failure mode. Cheap, model-side fix; no budget change. Most likely to eliminate the failure on its own.

### Option B — Raise `MAX_CLAIM_CONTINUATION_STEPS` from 8 → 12

`peer_task.py:38`. Gives headroom for one race + a couple of stalls + impl + tests. Costs more tokens when an agent is genuinely stuck, but bounded by `Budget` (TPM/RPM/lifetime in `budget.py`) so it can't run away. Cheapest mechanical fix.

### Option C — Don't charge a step for runtime-caught vacuous finals

In the inner loop at `peer_task.py:564`, when the assistant emits a `final` during claim-continuation and the runtime injects `claim_continuation_pending_write_reprompt`, treat that as a free retry (don't increment `step`) up to a small bounded number (e.g. 2). Higher complexity; risk of pathological loops if a model truly cannot recover.

### Option D — Have the coordinator hint mention `append_text` for the second-arriving writer

`coordination.py`'s runtime guidance currently tells each agent their scope but doesn't pre-warn about the create vs append race. Adding "If `/workspace/shared/<file>` already exists when you try to write, use `append_text` or `edit_section` instead of `create_file`" would prevent step #4's wasted call. Targeted but narrow.

## Critical files (read-only — for reference)

- `assignment2_part3/peer_task.py` — `MAX_CLAIM_CONTINUATION_STEPS` (line 38), inner loop (line 564), fallback emit (line 869), `_maybe_shared_write_refusal` (lines 221–256)
- `assignment2_part3/coordination.py` — runtime guidance injection
- `assignment2_part3/claims.py` — claim registry (for context only; not implicated in the failure)

## Verification (if any fix is applied)

1. `python -m pytest assignment2_part3/tests -q` — ensure no regressions in claim/peer-task tests.
2. Re-run the 4-terminal calculator demo from `demo.md:13-14`:
   - `docker compose down && docker compose build agent && docker compose up -d`
   - Send the same operator broadcast
   - Confirm both agents land impl + tests in their respective turns and neither emits the "step budget" fallback
3. `python tools/audit.py trace <new_trace_id>` — confirm bob's step count for the continuation turn stays under the cap.

## No change to apply yet

This is a diagnostic-only plan. The user asked "why did bob-swe fail" — answered above. Awaiting direction on whether to apply A/B/C/D (or none).
