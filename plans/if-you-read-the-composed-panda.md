# Plan: Test the "address all agents" path + surface skip reasons

## Context

The user sent a broadcast (`"all agents, please share your status"`).
Both Docker agents logged `[hub<-] seq=17 emil-user: ...`, but neither
replied. The broadcast feature is working as designed — but the operator
has no way to see *why* the agents stayed quiet. The skip reason is
written only to each agent's SQLite session log (via
`group_chat._log(store, "reply_decision", ...)` at `group_chat.py:117-121`),
never to stdout. From the operator's seat this looks like a bug.

Root cause of the silence: `reply_policy.should_reply`
(`reply_policy.py:117-119`) checks the per-thread cooldown **before** the
broadcast check, so a recent direct reply silently disables broadcast
participation for `REPLY_COOLDOWN_SECONDS` (default 30 s).

Goal: make broadcast behavior **debuggable** and **regression-proof**:
1. Add a `[skip] reason=...` stdout line in `group_chat.run_group_chat`
   when a message is dropped by the reply gate. Gated to
   `AGENT_MODE=runpod` so the existing stub-mode tests stay quiet.
2. Lock in the broadcast behavior with new pytest coverage — every
   broadcast keyword, multi-mention, cooldown-vs-broadcast precedence,
   and an end-to-end broadcast → reply path.

User-confirmed scope (from this session's AskUserQuestion):
- **Visibility:** add `[skip]` stdout line.
- **Gating:** runpod mode only (no test churn for stub).
- **Behavior:** no change to `reply_policy` defaults or check order.

## Files to change

### 1. `assignment2_part3/group_chat.py` — print skip reasons in runpod mode

Current code (`group_chat.py:116-123`):

```python
decision = should_reply(message, agent_id, display_name, recent_replies)
_log(store, "reply_decision", f"...")
if not decision.respond:
    continue
```

The `runpod` local is already computed (added in the previous task — see
`group_chat.py` after `system_prompt = load_system_prompt(...)`). Insert
one runpod-gated print right before the `continue`:

```python
if not decision.respond:
    if runpod:
        print(f"[skip] {decision.reason}", flush=True)
    continue
```

No other change to this file. The session-log `_log` line keeps the
authoritative record; the new print is purely operator visibility.

### 2. `assignment2_part3/tests/test_reply_policy.py` — broadcast coverage

Add the following tests (function names listed; each is a small
`should_reply(...)` call + assertions, mirroring the existing style at
`test_reply_policy.py:12-105`):

- `test_broadcast_keyword_everyone` — text `"everyone, status please"` → `respond is True`, reason contains `"broadcast"`.
- `test_broadcast_keyword_all_agents` — `"all agents, please report"` → True.
- `test_broadcast_keyword_any_volunteers` — `"any volunteers to help?"` → True.
- `test_broadcast_keyword_whoever` — `"whoever picks this up, go ahead"` → True.
- `test_bare_all_does_not_trigger_broadcast` — `"All systems go"` → `respond is False`. Captures the regex's `\b...all\s+agents?\b` requirement so future regex tweaks don't silently broaden the match.
- `test_multi_mention_triggers_both_agents` — single message `"@alice-swe @bob-swe ping"` evaluated twice (once as alice, once as bob); both return `respond is True` with reason `"directly addressed"`.
- `test_cooldown_blocks_broadcast_silently` — recent reply 5 s ago + broadcast text → `respond is False` AND `reason.startswith("cooldown:")` (NOT `"broadcast"`). This is exactly the silent skip the user hit; pinning the reason prefix means a refactor that flips the check order will fail loudly.
- `test_broadcast_window_resets_after_window_seconds` — recent reply older than `BROADCAST_WINDOW_SECONDS` does not count toward back-off; broadcast respond=True.

All tests reuse the existing `_msg` helper and use `now=1000.0` + a
`random.Random(0)` rng for determinism (same pattern as the current
`test_broadcast_triggers_reply_when_under_back_off`).

### 3. `assignment2_part3/tests/test_group_chat.py` — end-to-end broadcast

Add `test_broadcast_message_triggers_reply` using the existing
`_setup_run` / `_outbox_replies` helpers
(`test_group_chat.py:48-93`):

- Seed inbox: one PeerMessage with `text="all agents, please share your status"`.
- Scripted FakeChat reply: `{"type":"final","answer":"Status: all green"}`.
- Run the orchestrator briefly, then `stop.set()`.
- Assert:
  - `len(replies) == 1` in the outbox.
  - `"green"` appears in `replies[0]["text"]`.
  - Session DB has a `reply_decision` row whose content contains `"broadcast"`.

This is the regression test the user actually wanted: *can my agents
answer a broadcast at all?*

### 4. `assignment2_part3/tests/test_group_chat.py` — visibility tests

Add two short tests using pytest's `capsys` fixture:

- `test_skip_reason_silent_in_stub_mode`:
  - `monkeypatch.setenv("AGENT_MODE", "stub")` (already the default in `_setup_run`).
  - Inject a peer message that is *not* addressed (`"random chatter, no mention"`).
  - After the run, `captured = capsys.readouterr()`; assert `"[skip]" not in captured.out`.

- `test_skip_reason_printed_in_runpod_mode`:
  - `monkeypatch.setenv("AGENT_MODE", "runpod")`.
  - Inject the same unaddressed peer message; the injected `StubTransport`
    bypasses `build_transport` so no actual hub call happens.
  - Assert `"[skip]" in captured.out` AND the reason text appears
    (e.g. `"not addressed"`).

The runpod-mode test must inject `transport=StubTransport(...)` into
`run_group_chat` (the existing `_setup_run` already does this), so the
`mode == "runpod"` branch fires only for the print, not for transport
construction. `build_transport` is only called when `transport is None`
(`group_chat.py:97-98`).

### 5. `assignment2_part3/README.md` — document the new prefix

The README currently lists three log prefixes under the operator-console
section: `[hub<-]`, `[hub->]`, `[hub!]`. Add one row:

- `[skip]` — an incoming message was dropped by `reply_policy`. Shown in runpod mode only. Useful when broadcasts appear to go unanswered.

## Files / utilities reused (no new abstractions)

- `reply_policy.should_reply` (`reply_policy.py:82-132`) — already
  returns `ReplyDecision.reason`. Plumb directly to stdout.
- `group_chat.run_group_chat`'s existing `runpod` local — set once and
  used to gate the `[hub<-]` echo today; same gate works for `[skip]`.
- `tests/test_group_chat.py::_setup_run` and `_outbox_replies` — reuse
  for the new end-to-end + visibility tests.
- `tests/test_reply_policy.py::_msg` — reuse for all new unit tests.

## Out of scope

- Tuning `REPLY_COOLDOWN_SECONDS`, `REPLY_MAX_BROADCAST`, or
  `REPLY_BROADCAST_WINDOW_SECONDS` defaults — user picked "no behavior
  change". They remain configurable via `.env`.
- Changing the order of checks in `should_reply` — the cooldown-first
  ordering is the N×M storm prevention, kept intentionally; the new
  test pins the order so it isn't reordered by accident.
- A separate `[skip]` log file or hook system.

## Verification

1. `python -m pytest assignment2_part3 -q` — currently 76 tests, expect
   **~86** after this plan (8 new unit + 1 e2e broadcast + 2 visibility).

2. Stub-mode regression: existing tests must not start failing because
   of new stdout. The visibility tests confirm `[skip]` does NOT print
   in stub mode.

3. Live smoke (manual, no API key needed):
   ```bash
   AGENT_MODE=runpod AGENT_ID=alice AGENT_DISPLAY_NAME=alice-swe \
     RUNPOD_CHAT_URL=http://localhost:8080 RUNPOD_CHAT_PASSWORD=local-hub \
     python agent.py
   ```
   Then in another terminal: `python tools/chat.py say "just chatting"`.
   Expect alice's log: `[skip] not addressed; not a broadcast`.

4. The user's original scenario: with the docker hub demo running, send
   two `chat.py say` broadcasts in quick succession. The first should
   reply; the second should show
   `agent-alice-1 | [skip] cooldown: last reply X.Xs ago` and
   `agent-bob-1  | [skip] broadcast back-off: replied 1 times in last 300s`
   (or the equivalent reason). The operator now sees the silence
   explained.
