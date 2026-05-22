# Log provider + model per LLM call in Part 3

## Context

Part 3's `events` SQLite table (`assignment2_part3/thread_safe_store.py:28-36`)
captures `created_at, role, kind, content, trace_id` but never records which
LLM produced each assistant turn. `complete_chat`
(`assignment2_part2/llm_client.py:381-432`) resolves provider + model
internally and returns only `response.choices[0].message.content`, so the
information is dropped before it reaches the store.

Part 3 is the part where this matters most: each peer agent can run on a
different provider/model (Alice on Groq, Bob on OpenAI, etc.), and the
interesting behaviors to study — claim/defer tie-breaks, mention discipline,
refusal robustness, scrubber hits — vary by model. Recording provider + model
per `raw_json` event lets us slice runs post-hoc by `trace_id × model` and
attribute behavior to the model that produced it.

Out of scope: extending Part 1 and Part 2 logging. Part 3 is the
multi-agent surface where this comparison matters; Part 1/2 are
single-agent and would force a much larger test rewrite for low return.

## Recommended approach

Add a sibling function `complete_chat_with_metadata` in Part 2's
`llm_client.py` that returns `(content, provider, model)`. Refactor the
existing `complete_chat` into a thin wrapper that calls it and discards the
metadata, so Part 1, Part 2, and their tests are untouched. Part 3's
`peer_task.py` imports the new function, late-binds it, and threads
provider/model through to the store. Tests that monkey-patch with
string-returning fakes keep working via a small normalization shim.

Schema: add two new nullable columns (`provider`, `model`) to `events`,
following the same idempotent `ALTER TABLE … OperationalError` pattern
already used for `trace_id` on `thread_safe_store.py:38-42`.

## Files to modify

### 1. `assignment2_part2/llm_client.py`

- Add a new top-level function:
  ```python
  def complete_chat_with_metadata(messages) -> tuple[str, str, str]:
      ...
  ```
  Body is the current `complete_chat` body (lines 381-432). On the successful
  return path (line 428-429), return `(content or "", provider_name, model)`
  instead of just `content or ""`. On the `_failed_generation_json` recovery
  paths (lines 402-404, 419-421), also return `(recovered, provider_name, model)` —
  the model we *attempted* is still the right attribution.
- Reduce `complete_chat` to:
  ```python
  def complete_chat(messages):
      content, _provider, _model = complete_chat_with_metadata(messages)
      return content
  ```
  This preserves the existing return contract for every other caller in the repo
  (Part 1 `agent.py:67`, Part 2 `agent.py:274`, plus ~25 test stubs).

### 2. `assignment2_part3/thread_safe_store.py`

- Extend the `CREATE TABLE` statement (lines 28-36) to include
  `provider TEXT` and `model TEXT` (both nullable — most events have no
  associated LLM call).
- Add two idempotent `ALTER TABLE` migrations after the existing `trace_id`
  one (lines 39-42), wrapped in the same `try/except sqlite3.OperationalError: pass`
  pattern:
  ```python
  try:
      self.connection.execute("ALTER TABLE events ADD COLUMN provider TEXT")
  except sqlite3.OperationalError:
      pass
  try:
      self.connection.execute("ALTER TABLE events ADD COLUMN model TEXT")
  except sqlite3.OperationalError:
      pass
  ```
- Extend `record()` signature (line 46) to accept `provider: Optional[str] = None,
  model: Optional[str] = None` and include them in the INSERT (line 50-51).
  Keep them at the end of the kwargs list so existing positional callers don't
  break.

### 3. `assignment2_part3/peer_task.py`

- Replace the import (line 24) with:
  ```python
  from llm_client import complete_chat_with_metadata
  ```
- Update the late-binding default (lines 276-277): bind `chat_fn` to
  `complete_chat_with_metadata` when not provided.
- Normalize the result at the call site (line 348) so that test fakes
  returning plain strings keep working:
  ```python
  result = chat_fn(messages)
  if isinstance(result, tuple):
      raw_response, provider, model = result
  else:
      raw_response, provider, model = result, None, None
  ```
- Extend the inner `_log` helper (lines 287-291) to accept optional
  `provider` and `model` and forward them to `store.record`. Use
  `inspect.signature(store.record).parameters` (same pattern already used for
  `trace_id` on line 285) to gracefully degrade if the store predates the
  schema change.
- Pass `provider=provider, model=model` only on the `raw_json` log at line 350.
  Leave the other `_log` calls (peer messages, tool observations, claim
  events, refusals, parser guidance) untouched — they have no associated LLM
  call.

### 4. `assignment2_part3/tests/` — verify, don't rewrite

- `test_peer_task.py` (lambda `chat_fn` returning strings): keeps working via
  the `isinstance(result, tuple)` shim in step 3. No code changes expected,
  but re-run to confirm.
- `test_group_chat.py` (`FakeChat` returning JSON strings, monkey-patches
  `peer_task.complete_chat`): the monkey-patch target name changes to
  `peer_task.complete_chat_with_metadata`. The fake's string return shape
  is still handled by the shim, so only the patch target name needs updating
  (`test_group_chat.py:50`).
- Add one new test in `tests/test_peer_task.py`: pass a `chat_fn` that returns
  a `(content, "groq", "llama-3.1-8b-instant")` tuple and assert the `events`
  row for `kind='raw_json'` has matching `provider` and `model` values. This
  is the single new test that proves the wire-up works end-to-end.

## Files NOT touched

- `assignment2_part1/**` — `complete_chat` contract unchanged.
- `assignment2_part2/agent.py`, `assignment2_part2/tests/**` — `complete_chat`
  contract unchanged; ~15 test stubs untouched.
- `assignment2_part2/session_store.py` — Part 2 keeps its current schema.
  Only Part 3's `ThreadSafeSessionStore` gets the new columns.

## Verification

1. **Unit tests:**
   - `cd assignment2_part3 && python -m pytest tests/test_peer_task.py tests/test_group_chat.py -x`
     → existing tests pass after only the monkey-patch target rename.
   - The new tuple-passing test in `test_peer_task.py` passes.
2. **Migration on an existing DB:** copy a pre-change `session_history.sqlite3`
   into a scratch dir, instantiate `ThreadSafeSessionStore(path=...)`,
   confirm no error and that `PRAGMA table_info(events)` now lists `provider`
   and `model`.
3. **End-to-end smoke** (manual, one short Part 3 group-chat run):
   - Run `python -m assignment2_part3.agent` with two peers on different
     providers (set `LLM_PROVIDER_ORDER=groq` for one, `=openai` for the other
     via per-agent `.env`).
   - After the run, query:
     ```sql
     SELECT trace_id, provider, model, COUNT(*)
     FROM events WHERE kind='raw_json'
     GROUP BY trace_id, provider, model;
     ```
   - Expect every `raw_json` row populated, with the two providers visible
     across different traces.
4. **Local provider sanity:** with `LLM_PROVIDER_ORDER=local`, confirm
   logged values are `provider='local'`, `model=$LOCAL_LLM_MODEL` (or
   `'local-model'` if unset). This is what the user meant by "for local
   maybe we'd just say local" — it falls out for free because the existing
   `PROVIDERS` config already labels the entry `local` and resolves its model
   from `LOCAL_LLM_MODEL`.

## Risk notes

- `complete_chat_with_metadata` and `complete_chat` share a body — the
  refactor must keep `complete_chat` as a one-line wrapper to avoid drift.
  No duplicate provider-iteration logic.
- The `_failed_generation_json` recovery path attributes the model to the
  provider that *raised* the recoverable error. This is the right call for
  attribution ("which model misbehaved?") but worth noting in case readers
  expect "model that returned the content."
- Nothing in this change requires a destructive migration; old DBs gain
  two `NULL` columns and keep working.
