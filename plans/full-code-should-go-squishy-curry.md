# Fix three Part 3 issues from the calculator-collab session

## Context

During a local-hub demo of the calculator collab task (`@bob-swe @alice-swe build a calculator…`), three issues showed up. The user keeps the runpod-style behaviour (private workspaces, code-in-chat) intentional — those are **not** issues. The issues to fix are:

1. **Operator had to type `continue` after `:project new`.** The first broadcast arrived before any project was active, so each agent dropped it (`group_chat.py:969-977`). Activating a project did not replay the parked message. Operator wants auto-resume.
2. **Literal `<self>` placeholder leaked into chat output** — bob posted `/workspace/<self>/project4/calculator.py`. The token comes from `scrub_outbound` (`peer.py:137`) rewriting `/workspace/<agent_id>/...` for privacy, but the placeholder is ugly and the tool observation format already strips the agent segment (`assignment2_part2/tools.py:122-142`).
3. **Alice answered "everyone are done?" with "I have not actually created or edited any file in this round"** even though she had written files two minutes earlier. `STATUS_REQUEST_PATTERN` (`coordination.py:38-43`) does match, and the guidance does fire, but `_latest_shared_test_path` only scans for `/workspace/shared/...`. In runpod-private mode it returns `None`, so the guidance sends a generic "call run_tests on the shared test file" hint that the model ignores.

Each fix is independent. Recommended ship order: **Fix 2 → Fix 3 → Fix 1** (smallest/safest first, main-loop touch last).

---

## Fix 1 — Auto-replay parked inbound after `:project new` / `:project use`

**Goal.** When the runpod-mode skip path parks an inbound (`group_chat.py:969-977`), the next successful project activation replays the parked message through the same `_process_message` path so claims, reply gate, and code-save all re-fire normally.

**Changes.**

- `group_chat.py`, inside `run_chat` (the closure that already owns `recent_context`, `recent_replies`, `pending_followup`):
  - Add `pending_replay: list[PeerMessage]` (cap 4, drop-oldest on overflow) plus `pending_replay_lock: threading.Lock`.
  - Add a small helper `_drain_pending_replay()` that returns the parked list under the lock and clears it.
- `group_chat.py:969-977` (runpod skip branch only — leave the permissive local-shared path at 906-961 alone):
  - After `_print_no_project_prompt(message)` and before `return`, append `message` to `pending_replay` under the lock and `_log(store, "inbound_parked_no_project", f"msg_id=... depth=...")`. Enforce the 4-item cap; on overflow log `inbound_parked_overflow_dropped`.
- `group_chat.py:282-345` (`_build_project_handler`):
  - Add `on_activate: Callable[[str], None] | None = None` parameter. Invoke `on_activate("new")` at line 302, `on_activate("use")` at lines 319 and 326 — only on the success returns, not on error strings.
- `group_chat.py` (handler construction site, around line 630-636): pass an `on_activate` callback that just signals "drain on next loop tick" — no work needed, the main loop already polls every ≤ 1 s.
- `group_chat.py:1173-1178` (main loop): before `transport.recv`, drain via `_drain_pending_replay()`; for each parked message, log `inbound_replay msg_id=... sender=...` and call `_process_message(parked)`. Replay-then-recv ordering means the parked broadcast lands before any new fresh input.

**Why a `list` + `Lock`, not `queue.Queue`.** One producer (recv thread) + one consumer (recv thread), with the console daemon only flipping a flag. We need bulk drain, dedup by `message.id`, and bounded capacity — `list` is the right shape, `Queue` is overkill.

**Tests (`assignment2_part3/tests/test_group_chat.py`).**
- `test_runpod_skip_parks_inbound_without_active_project` — assert the message lands in the queue + the audit row.
- `test_project_new_drains_parked_inbound_through_process_message` — activate via the handler, assert `_process_message` ran on the parked id (use a `StubTransport` and read the audit log).
- `test_replay_queue_caps_at_four_drops_oldest` — 5th inbound while no project active → oldest dropped, overflow audit row, drain returns the newest 4 in arrival order.
- `test_local_shared_mode_does_not_park` — covers regression of the permissive lines 906-961.

---

## Fix 2 — Drop the `<self>` placeholder from outbound scrub

**Goal.** Outbound private-workspace paths render as `/workspace/project4/...` (matching what the LLM already sees in tool observations via `_display_workspace_path` in `assignment2_part2/tools.py:122-142`), not `/workspace/<self>/project4/...`.

**Changes.**

- `peer.py:133-140`: change the `private_pattern.subn` replacement from `"/workspace/<self>"` to `"/workspace"`. Keep `hits.append("private_workspace_path")` exactly as-is so the audit telemetry (`[say scrubbed: …]` print at `console_control.py:227-228`) is preserved.
- Do **not** touch incoming peer text or `peer_intent_refusal` — Fix 2 is cosmetic-outbound only.

**Tests (`assignment2_part3/tests/test_peer.py`).**
- Update existing `scrub_outbound` cases that expect `"/workspace/<self>/"` → expect `"/workspace/"`.
- Add `test_scrub_outbound_strips_agent_segment_matches_observation_format` — pin parity with the tool-observation format.
- Add `test_scrub_outbound_still_emits_private_workspace_path_hit` — guard the audit tag.

---

## Fix 3 — Status guidance finds the agent's own private test path

**Goal.** When `status_request_guidance` fires and no `/workspace/shared/...` test path exists in context, fall back to the agent's own most recent `/workspace/<agent_id>/projectN/test_*.py` so the hint points to a real file the agent can `run_tests` against.

**Changes.**

- `coordination.py`, near the existing `SHARED_TEST_PATH_PATTERN` at line 37: add
  `PRIVATE_TEST_PATH_PATTERN = re.compile(r"(?P<path>/workspace/[A-Za-z0-9_-]+/project\d+/(?:test_[^\s:;,]+|[^\s:;,]+_test)\.py)")`.
- `coordination.py`, near the existing `_latest_shared_test_path` (around 326-341 — exact helper found by Explore agent #3): add `_latest_self_test_path(recent_context, agent_aliases)` that walks `recent_context` in reverse, filters entries whose `sender_id.lower()` is in `agent_aliases` (reuse `_agent_aliases` at line 75-79), and returns the first `PRIVATE_TEST_PATH_PATTERN` match. Returns `None` when no match.
- `coordination.py:443-448` (inside `status_request_guidance`): change `test_path = _latest_shared_test_path(recent_context or [])` to:
  `test_path = _latest_shared_test_path(recent_context or []) or _latest_self_test_path(recent_context or [], _agent_aliases(agent_id, display_name))`. The downstream hint string at line 445 already says "on {test_path} first" and is path-agnostic.
- Do **not** widen `SHARED_TEST_PATH_PATTERN` — `/workspace/shared/...` is meaningful for the local-shared mode prompts elsewhere.

**Tests (`assignment2_part3/tests/test_coordination.py`).**
- `test_status_request_guidance_uses_shared_test_path_when_present` — regression that shared still wins when both exist.
- `test_status_request_guidance_falls_back_to_self_private_test_path` — alice's own message contains `/workspace/alice-swe/project4/test_calculator.py`, expect that exact path in the hint.
- `test_status_request_guidance_only_matches_own_sender_id` — bob's path in `recent_context` must NOT appear in alice's hint.
- `test_status_request_guidance_done_question_mark_still_matches` — pin "everyone are done?" so a future regex tweak does not silently drop it.

Swedish forms (`är ni klara`, `klar(a)?`, `färdig(a)?`) — out of scope; user did not ask. Note in a follow-up if it bites again.

---

## Critical files

- `C:\Users\emil_\vscode\Assignment2\assignment2_part3\group_chat.py` — Fix 1 producer / consumer / handler signature.
- `C:\Users\emil_\vscode\Assignment2\assignment2_part3\peer.py` — Fix 2 one-line subn change.
- `C:\Users\emil_\vscode\Assignment2\assignment2_part3\coordination.py` — Fix 3 new helper + pattern + fallback wiring.
- `C:\Users\emil_\vscode\Assignment2\assignment2_part3\tests\test_group_chat.py` — Fix 1 tests.
- `C:\Users\emil_\vscode\Assignment2\assignment2_part3\tests\test_peer.py` — Fix 2 tests.
- `C:\Users\emil_\vscode\Assignment2\assignment2_part3\tests\test_coordination.py` — Fix 3 tests.

## Reuse callouts

- `_agent_aliases` (`coordination.py:75-79`) — reuse in `_latest_self_test_path`, do not re-implement.
- `_log(store, kind, body)` audit helper — used by the existing skip path; reuse for `inbound_parked_no_project`, `inbound_parked_overflow_dropped`, `inbound_replay`.
- `_process_message` — reuse on replay; do not bypass to a leaner path or claim absorption + reply gate will diverge.
- `_display_workspace_path` (`assignment2_part2/tools.py:122-142`) — Fix 2 matches this format; do not duplicate the logic.

## Verification

End-to-end (after all three fixes land):

```bash
cd assignment2_part3
docker compose build agent
docker compose up -d
docker compose logs -f                               # T1
docker attach assignment2_part3-agent-alice-1        # T2 — Ctrl-P Ctrl-Q to detach
docker attach assignment2_part3-agent-bob-1          # T3
python tools/chat.py live --as emil-user             # T4
```

In the chat session reproduce the original scenario:

1. Send the calculator broadcast as the first message.
2. In each agent terminal type `:project new` — confirm the agent **resumes** the parked broadcast without operator re-prompting (Fix 1).
3. Confirm chat output shows `/workspace/project4/...` (or whatever the stripped path is), not `/workspace/<self>/...` (Fix 2).
4. After both agents post their first "Done with: …" status, send `everyone are done?`. Confirm both reply with the structured Done/Tests/Blockers shape and that the alice-style "no file in this round" reply no longer happens (Fix 3).

Unit suites:

```bash
python -m pytest assignment2_part3/tests/test_group_chat.py -q
python -m pytest assignment2_part3/tests/test_peer.py -q
python -m pytest assignment2_part3/tests/test_coordination.py -q
python -m pytest assignment2_part3/tests -q          # full part 3
python -m pytest assignment2_part2 -q                # regression — Part 3 changes often nick Part 2
```

Cross-agent audit (sanity check after the e2e run):

```bash
python tools/audit.py tail --agent alice --kind inbound_parked_no_project
python tools/audit.py tail --agent alice --kind inbound_replay
```

Both event kinds should appear once per agent for the reproduction scenario.
