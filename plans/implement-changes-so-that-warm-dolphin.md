# Plan: cap LLM generations and add request timeout

## Context

Part 3 agents hang indefinitely when a local llama.cpp server enters a runaway-generation
state. Investigation from `data/{alice,bob}.sqlite3` and `GET /slots` on the local server
confirmed:

- Bob's slot was still `is_processing: True` with `n_decoded: 20540` and `has_next_token: True`
  while alice's slot had already finished and gone idle.
- The model is loaded with `reasoning_format=deepseek` and wants to begin output with
  `<think>\n`, but our request sends `response_format={"type":"json_object"}` which
  enforces a JSON grammar that rejects `<`. The model can satisfy neither constraint, so
  it samples grammar-compatible filler tokens forever.
- `assignment2_part2/llm_client.py:305-313` (`_create_completion`) builds its OpenAI-SDK
  kwargs with no `max_tokens` cap and no per-request `timeout`. With `stream=False`, the
  Python client blocks on the HTTP response, never sees the runaway, never errors out.
- `assignment2_part3/group_chat.py:208-221` already catches `RuntimeError` from
  `complete_chat` and logs it as an `llm_failure` event plus a `[llm!]` stderr line — so a
  timeout-induced error will surface cleanly *if* the HTTP call can be made to raise.

Goal: bound each LLM call in two ways so a runaway either truncates (and the parser_guidance
loop recovers) or surfaces as a logged failure within a bounded wall-clock time. Minimal,
surgical change. No model swap, no server-side reconfiguration.

## Approach

Add `max_tokens` and `timeout` as per-request kwargs to the single
`client.chat.completions.create(...)` call in `_create_completion`. Source both from new
env vars with safe defaults, using the existing env-helper pattern.

### Why per-request, not per-client

`_client_for_provider` (`assignment2_part2/llm_client.py:137-142`) is shared and creates
the client with `max_retries=0` but no timeout. Setting timeout per-request keeps the
change localized to one function, and the OpenAI SDK accepts it identically.

### Why these defaults

- **`LLM_MAX_TOKENS=2048`** — long legitimate replies in `data/alice.sqlite3` (CLAIM +
  prose + file content + RELEASE) are ~200 tokens. 2048 leaves 10x headroom while
  truncating a 20k runaway well before it can block a slot.
- **`LLM_REQUEST_TIMEOUT_SECONDS=120`** — bob's first turn took ~3s, alice's longest tool
  round took ~16s. 120s is conservative for slow local hardware while bounding a hang at 2
  minutes.

Both env vars use 0 to mean "no override / SDK default" so users can disable the cap if
needed (matches the `LLM_RATE_LIMIT_MAX_WAIT_SECONDS=0` convention at
`assignment2_part2/llm_client.py:39-43`).

## Files to modify

### 1. `assignment2_part2/llm_client.py`

- Add a small `_env_positive_int(name, default)` helper next to `_env_nonnegative_float`
  at line 29 (returns int ≥ 0, falls back to default on parse error).
- Add two module-level getters parallel to `_rate_limit_total_wait_seconds` at line 39:
  - `_max_tokens()` — reads `LLM_MAX_TOKENS`, default `2048`.
  - `_request_timeout_seconds()` — reads `LLM_REQUEST_TIMEOUT_SECONDS`, default `120.0`,
    reusing `_env_nonnegative_float`.
- Modify `_create_completion` at line 305 to add the two kwargs:
  ```python
  def _create_completion(client, model, messages, *, use_json_mode):
      kwargs = {"model": model, "messages": list(messages)}
      max_tokens = _max_tokens()
      if max_tokens > 0:
          kwargs["max_tokens"] = max_tokens
      timeout = _request_timeout_seconds()
      if timeout > 0:
          kwargs["timeout"] = timeout
      if use_json_mode:
          kwargs["response_format"] = JSON_RESPONSE_FORMAT
      return client.chat.completions.create(**kwargs)
  ```
- No other call sites of `client.chat.completions.create` exist (confirmed in exploration),
  so this is the only edit point.

### 2. `assignment2_part2/tests/test_llm_client.py`

Two tests compare `completions.calls` to an exact dict:

- `test_json_mode_is_sent_on_first_request` (lines 62-74) — update the expected dict at
  lines 68-73 to include `"max_tokens": 2048` and `"timeout": 120.0`.
- `test_local_provider_does_not_require_api_key` (lines 77-92) — same update at lines
  86-92.

Other tests in the file already use key-specific assertions (e.g. `calls[0]["response_format"]`)
and continue to pass without changes.

Add one new test:
- `test_max_tokens_and_timeout_are_capped_per_request` — set `LLM_MAX_TOKENS=4096` and
  `LLM_REQUEST_TIMEOUT_SECONDS=42`, verify the kwargs are forwarded.
- `test_max_tokens_zero_means_no_cap` — set `LLM_MAX_TOKENS=0`, verify `"max_tokens"` is
  absent from the call (preserves SDK default behaviour).

### 3. `assignment2_part3/docker-compose.yml`

Thread the new env vars into both agent services so they're discoverable and overridable
from `.env`:

- Add to `agent-alice` `environment:` (around line 28):
  ```yaml
  LLM_MAX_TOKENS: ${LLM_MAX_TOKENS:-2048}
  LLM_REQUEST_TIMEOUT_SECONDS: ${LLM_REQUEST_TIMEOUT_SECONDS:-120}
  ```
- Same two lines added to `agent-bob` `environment:` (around line 62).

No changes to the `local-hub` service (it doesn't call the LLM).

## Out of scope (deliberate)

- Server-side reasoning/JSON-mode conflict — fixing the llama.cpp launch flags or the
  model choice is a separate decision the user explicitly deferred.
- Per-agent provider routing (alice=local, bob=cloud) — already supported via
  `LLM_PROVIDER_ORDER` and tracked separately.
- Streaming. Adding streaming would let us see runaway generations live, but it's a
  larger refactor of `complete_chat` and the test suite. The timeout achieves the same
  failure-surfacing goal with much less churn.

## Verification

End-to-end:
1. `cd assignment2_part3 && docker compose up --build agent-alice agent-bob local-hub` —
   confirm both containers start without env-var errors.
2. From a separate shell:
   `python tools/chat.py --url http://localhost:8090 live --as emil-user` and send
   `@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: alice writes
   add+subtract, bob writes multiply+divide. Use the CLAIM/RELEASE protocol`.
3. While running, hit `GET http://localhost:8080/slots` and confirm `n_decoded` stays well
   under 2048 on both slots. Previously slot 0 climbed past 20000.
4. If the model hangs at the grammar level, expect within ~120s to see a `[llm!] failed`
   line on stderr from `docker logs -f assignment2_part3-agent-bob-1` (or alice) and an
   `llm_failure` event in `data/{alice,bob}.sqlite3`. The agent stays alive and accepts the
   next message — no orphaned slots.

Unit:
- `cd assignment2_part2 && python -m pytest tests/test_llm_client.py -v` — both updated
  exact-match tests plus the two new tests pass.
- `cd assignment2_part3 && python -m pytest tests/test_peer_task.py -v` — unchanged; these
  pass `chat_fn=` directly and don't go through `_create_completion`.

Manual smoke:
- `LLM_MAX_TOKENS=0 docker compose up agent-alice` — confirm no cap is sent (use llama.cpp
  server logs to verify `n_predict=-1` is still respected).
