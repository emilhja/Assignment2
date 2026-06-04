# Emil-Hjaertfors Agent Improvements

## Context

Two live hub runs (`logs/2026-05-29-1225.md` — Habit Tracker CLI; `logs/2026-05-29_1540_not_analysed.md` — self-organize/manager) exposed recurring problems in our Part 3 agent (`emil-hjaertfors-agent`). Goal: make Emil **less noisy, more truthful about file/test state, and resistant to API/schema drift** during multi-agent work — while staying a general SWE agent (not a tester or manager). Primary target is the public RunPod hub with per-agent private workspaces; local shared-workspace claim/defer stays supported but does not drive defaults.

Key findings driving this change (with evidence):
- **Internal coaching/budget strings leaked to the hub** — "I could not complete this within my step budget", "I had to stop because I kept answering with an intro…" posted as chat replies (1540 `#37`,`#42`; 1225 `#79`,`#91`,`#92`,`#192`). Origin: `peer_task.py` stall fallbacks (~1266, 1287, 1329) and the terminal step-budget fallback (`peer_task.py:1520`).
- **Schema flip-flopping** — 3 incompatible habit schemas before a 4th, forcing peers to reconcile (1225 `#56`/`#62`/`#64`/`#101`, mismatch called out at `#103`/`#2297`).
- **Confabulated state** — "I have not contributed any code… just joined" despite delivering `habit_logic.py` earlier (1225 `#199` vs `#101`).
- **Duplicate large code pastes** (1540 `#70`/`#76`; 1225 `#56`/`#104`/`#108`).

### Already present — do NOT re-add, only tune
- Truthfulness ("created = written only; tests not run unless `run_tests` observation") — `part3 config/system_prompt.txt:85`, `part2 config/system_prompt.txt:40`.
- No-echo / dedupe / "ready once" — `part3 config/system_prompt.txt:68–72`.
- Reply discipline (no broadcast volunteering, no answering for others) — `part3 config/system_prompt.txt:56–63`.
- Narrow contract-first hook — `coordination.py:_signature_agreement_guidance` (fires only on an explicit "agree on signatures" request).

## Approved decisions
1. **Stall leak:** suppress/neutralize — route terminal fallbacks to the log; on give-up send one short neutral line or stay silent.
2. **Paste policy:** paste full contents **once** on first delivery (and in RunPod private mode where peers can't read the path), then status-only afterward.
3. **Credential hygiene:** confirm `.env` is untracked + document manual key rotation. No startup-warning code.

## Changes

### 1. `assignment2_part3/config/system_prompt.txt`
- **Contract-first norm:** for multi-file work (shared schemas, function signatures, tools, JSON shapes), state or confirm the data/API contract **once** before implementing dependent files, and reference that contract instead of re-proposing it. Do not silently change a contract you already posted.
- **Paste-once policy (resolve tension with `:69–70`):** on first delivery of a file, paste full contents (split per the 4096 cap) **and** give the exact path the runtime reported; on later status pings, status-only unless the file changed or a peer asks to see it. Keep the existing "don't re-post unchanged files" rule intact — this just makes the first paste explicit for private-workspace/RunPod mode where peers cannot read the path.
- **Prefer integration-fix over re-implement:** strengthen `:52` — when a peer already posted code that satisfies the request, review/fix the integration mismatch rather than re-implementing from scratch.

### 2. `assignment2_part3/coordination.py`
- Broaden contract-first guidance beyond the literal "agree on signatures" trigger: when a prompt involves multiple agents + a shared schema / function signatures / JSON shape / tests, inject a hint to **post the contract once, then implement to it** (reuse the structure of `_signature_agreement_guidance:167` and the `SIGNATURE_AGREEMENT_PATTERN`/new patterns). Keep it deterministic and pure, consistent with the existing helpers.

### 3. `assignment2_part3/peer_task.py`
- **(cause)** Extend the continuation-reprompt classification so "I will create/fix/test…" prose without a matching tool call is also caught for **direct-implementation, review, and test-fix** requests — not just the cases already handled (~1118–1351). Keep the existing no-write and pytest-required guards.
- **(symptom)** Neutralize the terminal fallbacks so internal coaching never reaches the hub: the stall fallbacks (~1266, 1287, 1329) and the step-budget fallback (`:1520`) write their diagnostic reason to the SQLite log only, and the outbound reply becomes either a single neutral line or nothing. Gate with an env flag (e.g. `SUPPRESS_STALL_REPLIES`, default on) so behavior is configurable. Confirm `group_chat.py` send path treats an empty/sentinel reply as "send nothing".

### 4. `assignment2_part2/config/system_prompt.txt` (+ tool registry)
- Add the same **contract-before-dependent-implementation** guidance for JSON/tool-agent work.
- Verify the prompt's Available-tools list matches every registered tool in Part 2's `tools.py` (add a regression test, see below). Truthfulness rule already present (`:40`) — leave as is.

### 5. Credential hygiene (no app code)
- Confirm `.env` is gitignored and not tracked in either part; confirm only `*.env.example` is committed.
- Document (in the plan handoff / commit notes) that the user must **rotate provider keys via their dashboards** — rotation cannot be performed from here.

## Test Plan
Run both suites (must stay green):
```bash
python -m pytest assignment2_part2 -q
python -m pytest assignment2_part3/tests -q
```
Add Part 3 tests (extend `tests/test_peer_task.py`, `tests/test_coordination.py`, `tests/test_reply_policy.py`):
- Direct feature request → Emil must write before claiming "done" (existing no-write guard still fires).
- **Stall-leak suppression:** when the loop hits the terminal/step-budget fallback with `SUPPRESS_STALL_REPLIES=1`, the outbound reply is empty/neutral and the diagnostic is logged (assert it is NOT the "I had to stop…/step budget" sentence).
- **Contract-first:** a multi-agent + schema prompt yields a "post the contract once, then implement" hint.
- API-mismatch scenario modeled on the Habit Tracker (main.py vs habit_logic.py signature/`created_at` mismatch) → Emil reports a blocker instead of "complete".
- Broadcast status does not produce repeated readiness spam (reply-gate + no-echo).
- Direct "fix failing tests" → forces `read_file`/write/`run_tests` path (existing `fix_blockers_guidance:520`), not prose-only refusal.

Add Part 2 tests:
- System prompt mentions every registered tool in `tools.py`.
- Final answer cannot claim tests passed without a test observation.

## Verification
- Both pytest suites green.
- Manual Part 3 smoke (per `CLAUDE.md` 4-terminal layout) or a targeted `tools/chat.py` exchange: trigger a stall and confirm the hub sees a neutral/empty reply (not the coaching string), and that a multi-file request produces a single posted contract before implementation.
- `git status` / `git ls-files` confirm no `.env` is tracked.
