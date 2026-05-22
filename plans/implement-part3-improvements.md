# Plan — three defensive-depth fixes for assignment2_part3

## Context

Follow-up to `plans/can-you-check-if-fluttering-thacker.md`. Part 3 is
already MET on every rubric criterion; these three changes harden the
defence-in-depth posture without changing the public surface.

Goal: close three small gaps surfaced during the grading pass:
1. `:say` bypasses the outbound credential scrubber.
2. Broadcast back-off (P3.6) is English-only — silent on Swedish peers.
3. `:pause`/`:resume` state is not persisted across restarts.

## Fix 1 — Scrub `:say` outbound text

**Problem.** `console_control.py:154-165` `_cmd_say` calls
`self.send_fn(message)` directly. The outbound scrubber only runs inside
`peer_task.py:141`, so an operator typo (`:say api key = sk-abc...`) hits
the hub verbatim.

**Decision.** Apply `scrub_outbound` in `_cmd_say`. Print a hint when a
redaction fires so the operator notices.

**File:** `assignment2_part3/console_control.py`

```python
# top of file
from peer import scrub_outbound

# _cmd_say (lines 154-165) — replace body after the empty-check:
def _cmd_say(self, text: str) -> None:
    message = text.strip()
    if not message:
        self._print("[usage: :say <text>]")
        return
    if self.send_fn is None:
        self._print("[say not wired — transport unavailable]")
        return
    scrubbed, hits = scrub_outbound(message)
    if hits:
        self._print(f"[say scrubbed: {sorted(set(hits))}]")
    try:
        self.send_fn(scrubbed)
    except Exception as exc:
        self._print(f"[say failed: {exc}]")
```

**Tests** — `assignment2_part3/tests/test_console_control.py`:
- Add `test_say_scrubs_credentials_before_send`: feed `:say leak sk-abc123def456ghi789jkl0`, assert `send_fn` was called with `[REDACTED:openai_key]` and stdout shows `[say scrubbed: ['openai_key']]`.
- Add `test_say_passthrough_when_clean`: feed `:say hello team`, assert `send_fn` received exactly `hello team`.

## Fix 2 — Swedish broadcast keywords for the N×M gate

**Problem.** `reply_policy.py:40` `BROADCAST_PATTERN` matches only
English keywords (`everyone|anyone|all\s+agents?|any\s+volunteers?|whoever`).
The course is TH25 (Swedish); a peer asking "kan någon kolla det här?"
will not trigger broadcast back-off, so the N×M reduction silently
regresses to "always reply" for Swedish broadcasts.

**Decision.** Extend the pattern with Swedish alternates. Keep it one
regex — the existing tests only check that broadcast keywords trigger
the back-off, so they continue to pass.

**File:** `assignment2_part3/reply_policy.py:40`

```python
BROADCAST_PATTERN = re.compile(
    r"(?i)\b("
    r"everyone|anyone|all\s+agents?|any\s+volunteers?|whoever"
    r"|alla|någon|vem\s+som\s+helst|alla\s+agenter|volontär(er)?"
    r")\b"
)
```

**Tests** — `assignment2_part3/tests/test_reply_policy.py`:
- Add `test_swedish_broadcast_triggers_reply`: message text "kan någon kolla det här?", empty `recent_replies` → `respond=True`, `reason="broadcast question"`.
- Add `test_swedish_broadcast_backoff`: same text, `recent_replies` already at `MAX_BROADCAST_REPLIES` within the window → `respond=False`.

## Fix 3 — Persist pause/resume state

**Problem.** `console_control.py:135, 138` call `self.budget.pause()` /
`self.budget.resume()` but never `self.budget.save()`. `:limit` correctly
saves at line 182. If the process restarts while paused, it comes back
un-paused, defeating the operator's intent.

**Decision.** Add `self.budget.save()` after each mutation. The save is
cheap (one JSON file write) and matches the `:limit` precedent.

**File:** `assignment2_part3/console_control.py:134-139`

```python
elif cmd == "pause":
    self.budget.pause()
    self.budget.save()
    self._print("[budget paused]")
elif cmd == "resume":
    self.budget.resume()
    self.budget.save()
    self._print("[budget resumed]")
```

**Tests** — `assignment2_part3/tests/test_console_control.py`:
- Add `test_pause_persists_to_disk`: build a Budget with a tmp_path persist_path, feed `:pause`, reload with `Budget.load(path)`, assert `paused is True`.
- Add `test_resume_persists_to_disk`: same setup starting paused, feed `:resume`, reload, assert `paused is False`.

## Verification

After all three edits:

```bash
cd assignment2_part3
python -m pytest tests/ -q
```

Expected: existing 87 tests still pass, +6 new tests pass (2 per fix).

Smoke check on the live agent:
```bash
python agent.py     # in stub mode
# In the stdin: ":say my key sk-fakefakefakefakefakefake"
# Expect: "[say scrubbed: ['openai_key']]" line in stdout,
#   and the JSON sent to stub transport has [REDACTED:openai_key]
# Then: ":pause", restart the process, ":budget"
# Expect: snapshot shows paused: True
```

## Critical files

- `assignment2_part3/console_control.py` — Fix 1 + Fix 3
- `assignment2_part3/reply_policy.py` — Fix 2
- `assignment2_part3/tests/test_console_control.py` — Fix 1 + Fix 3 tests
- `assignment2_part3/tests/test_reply_policy.py` — Fix 2 tests
- `assignment2_part3/peer.py` — already exports `scrub_outbound` (no change)
- `assignment2_part3/budget.py` — already exports `save/pause/resume` (no change)

## Out of scope

- Refactoring `transport.send` to scrub centrally — would require
  touching every transport implementation and is a larger redesign.
  Fix 1 closes the only known bypass.
- Tightening `_mentions` literal-name matching (false-positive risk on
  common-word agent names) — listed in the grading evaluation as
  "take or leave", not pursued here.
- Counting prompt tokens in `budget.record` — same status.
