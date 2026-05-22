# Plan: close the parser-rejection blind spot in the shared-write lie corrector

## Context

In a recent live session (audit traces around `2026-05-22T22:05`), `alice-swe` posted
"Successfully created /workspace/shared/calculator.py with the add() and subtract()
functions" — but her `create_file` tool call was **never executed**. The file on disk was
written entirely by `bob-swe`'s `#multiply-division` claim and just happened to include
add/subtract in its content.

Audit replay of alice's round shows the failure mode:

1. `22:05:11` raw `CLAIM …` line (no JSON envelope) → `parse_response` rejects ("not a JSON object") → `parser_guidance` injected
2. `22:05:24` `{"type":"tool_call","tool":"create_file",…}` → `parse_response` also rejects (likely escaping in `content`) → `parser_guidance` injected again (no `tool create_file` observation follows)
3. `22:05:26` `{"type":"final","answer":"Successfully created …"}` → passes through unmodified

The lie-corrector at `assignment2_part3/peer_task.py:419` only rewrites the final answer
when `saw_failed_shared_write` is `True`. That flag is set on two paths:

- `peer_task.py:454` — `_maybe_shared_write_refusal` returned a `block_reason`
- `peer_task.py:467` — a CLAIM_GATED tool ran but its observation matched `_looks_like_failed_write`

The third failure mode — `parse_response` rejecting a tool_call before it ever ran —
is not covered. So a model that emits a malformed shared-write tool_call and then
fabricates success in a later `final` round walks straight past the corrector.

Intended outcome: fabricated success-claims on /workspace/shared after a parser-rejected
write attempt get rewritten by the same corrector that already handles gate refusals and
failed observations.

## Fix

### 1. Set `saw_failed_shared_write` on parser-rejected shared-write attempts

File: `assignment2_part3/peer_task.py`

In the `else` branch at lines 478–483 (where unparseable responses log
`parser_guidance`), inspect the raw response. If it referenced
`/workspace/shared/` together with any token from `CLAIM_GATED_TOOLS`
(`create_file`, `edit_section`, `replace_text`), set
`saw_failed_shared_write = True` before logging guidance.

This is a content sniff on the **raw model output**, not on parsed args (we cannot
parse them — that's why we're here). Cheap heuristic, matches the same surface area
the corrector at line 419 already guards (`_looks_like_write_success_claim` also
sniffs for `/workspace/shared/` + verbs). The two heuristics are symmetric:
"attempted-but-malformed write" ↔ "claimed-success write".

Shape of the change (illustrative, not final code):

```python
# end of the parsed-kind dispatch in run_peer_task
if (
    "/workspace/shared/" in raw_response
    and any(tool in raw_response for tool in CLAIM_GATED_TOOLS)
):
    saw_failed_shared_write = True
guidance = (
    "Your previous response was invalid. Respond with exactly one JSON object and no prose. "
    f"Parser error: {parsed.error}"
)
_log("system", "parser_guidance", guidance)
messages.append({"role": "user", "content": guidance})
```

Reuses existing constants (`CLAIM_GATED_TOOLS`, `SHARED_PATH_PREFIX`), no new helpers.

### 2. Strengthen the system prompt on protocol-line wrapping

File: `assignment2_part3/config/system_prompt.txt`

The model repeatedly emits `CLAIM …` and `RELEASE …` as raw lines instead of
wrapping them in the required `{"type":"final","answer":"…"}` envelope (every alice
and bob session in the audit shows 1–2 parser_guidance rounds before they comply).
This wastes budget and is the root cause that exposed bug #1.

In the P3.9 section (around lines 53–69), add one explicit example showing the wrap:

> Protocol lines (CLAIM, RELEASE, DEFER) are still final answers and MUST be wrapped:
> `{"type":"final","answer":"CLAIM /workspace/shared/foo.py#scope: <reason>"}`. Do not
> emit a bare `CLAIM …` line — it will be rejected by the parser.

Keep this short — one line plus the example. Do not restructure the surrounding
rules.

### 3. Regression test

File: `assignment2_part3/tests/test_peer_task.py`

Mirror the existing `test_blocked_shared_write_success_claim_is_corrected`
(line 218) but feed `chat_fn` two responses:

1. A raw, non-JSON `CLAIM /workspace/shared/calculator.py#add-subtract: …` string
   (or a malformed `{"type":"tool_call",…}` for `create_file` on
   `/workspace/shared/calculator.py`) — either triggers parser_guidance.
2. A valid `{"type":"final","answer":"Successfully created /workspace/shared/calculator.py …"}`.

Assertions (same as existing test):

- `"could not complete" in answer.lower()`
- `not (shared / "calculator.py").exists()`
- `"peer_reply_corrected" in event kinds`

Plus one new assertion specific to this path:

- `"parser_guidance" in event kinds` (confirms we're exercising the new branch, not the existing gate-refusal one)

## Files touched

- `assignment2_part3/peer_task.py` — ~5 lines added in the parser-rejection branch
- `assignment2_part3/config/system_prompt.txt` — one sentence + one example near P3.9
- `assignment2_part3/tests/test_peer_task.py` — one new test, ~30 lines, copy-shaped from `test_blocked_shared_write_success_claim_is_corrected`

## Out of scope (explicitly not changing)

- **`parse_response` auto-wrapping CLAIM/RELEASE/DEFER lines.** Tempting but a
  semantic change to the parser contract; risks accepting accidental protocol
  utterances in prose. Better handled by tightening the prompt (#2).
- **Bob writing alice's scope content.** The gate at `peer_task.py:215–237` only
  checks the agent's own claim target vs. peer claims; it does not introspect
  `args["content"]` for cross-scope text. Out of scope here — would need a real
  semantic check, not a string sniff.
- **`successful_shared_write_paths` set redesign.** Replacing the negative
  `saw_failed_shared_write` flag with a positive evidence set would be cleaner,
  but it's a wider refactor. Defer until a second leak motivates it.

## Verification

From `assignment2_part3/`:

1. `python -m pytest tests/test_peer_task.py -k "success_claim_is_corrected or parser_rejected" -v`
   — new test passes, existing one still passes.
2. `python -m pytest tests/ -q` — full part3 suite stays green.
3. Replay the live scenario: launch alice + bob via existing docker-compose / runpod
   harness, send the coordinator prompt
   `@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: alice writes add+subtract, bob writes multiply + division`,
   and confirm via `python tools/audit.py tail --agent alice` that any
   parser-rejected create_file followed by a `final` claiming success is rewritten
   to the "could not complete" string and tagged with `peer_reply_corrected`.
4. `python tools/chat.py tail --since 0` should no longer surface a "Successfully
   created" line from alice unless a `tool create_file` observation precedes it in
   her audit DB.
