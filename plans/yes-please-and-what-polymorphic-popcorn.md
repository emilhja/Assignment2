# Plan: `fix_blockers_guidance` (and optional `private_workspace_guidance`)

## Context

Two observed misbehaviors in the Part 3 multi-agent demo, each from a missing
runtime guidance hook:

1. **Refuse-on-fix-request.** After agents reported `Done: … Tests: ran and
   failed … Blockers: NameError`, the operator asked `@alice-swe can you fix the
   blockers?`. Alice went straight from inbound → `type:"final"` refusal with
   zero tool calls (audit log trace `[12]`: `peer message` → `raw_json
   {"type":"final","answer":"I'm unable to fix..."}`). Each peer turn is a
   fresh per-task session, so alice's context did *not* contain the prior
   trace's `pytest exited with code 1 / FFFF …` observation — and instead of
   re-running tests or re-reading the file to fetch the failure, the model
   rationalized a refusal.

2. **Operator-specified private path overridden.** Operator asked alice to
   write `/workspace/alice/calculator.py`. Alice CLAIMed
   `/workspace/shared/calculator.py#full-file` instead. Source:
   `config/system_prompt.txt:43` ("Always write joint output there using the
   explicit path `/workspace/shared/<file>`"). The coordination helpers in
   `coordination.py` only match `/workspace/shared/` paths
   (`SHARED_PATH_PATTERN`, line 12), so explicit private-workspace paths in the
   operator's message trigger no counter-guidance, and the broad system-prompt
   rule wins.

The two fixes follow the same pattern that's already established in
`coordination.py`: pure functions returning `str | None`, composed into
`runtime_guidance` in `group_chat.py:325-369`, then injected into the per-task
session in `peer_task.py:556-560`.

## Recommended approach

### Part A — `fix_blockers_guidance` (primary, confirmed scope)

**New helper in `assignment2_part3/coordination.py`** mirroring
`status_request_guidance` (lines 332-376):

- `FIX_REQUEST_PATTERN` regex matching the common operator phrasings:
  `"fix the blockers"`, `"fix the blocker"`, `"can you fix"`, `"please fix"`,
  `"address the failure(s)"`, `"resolve the (issue|error|failure)s?"`,
  `"make the tests pass"`. Case-insensitive. Mirror the layout of
  `STATUS_REQUEST_PATTERN` (lines 32-37).
- `fix_blockers_guidance(text, *, agent_id, display_name, recent_context)`
  returns `None` when the pattern doesn't match. When it does, returns a
  guidance string that:
  1. States the operator wants the prior blocker fixed.
  2. **Forbids a `type:"final"` answer with no tool calls** — explicitly
     requires the agent to first call `run_tests` on the latest shared test
     path (use `_latest_shared_test_path(recent_context)` already defined at
     `coordination.py` near `status_request_guidance`) or `read_file` on the
     relevant source, to recover the actual failure detail this turn.
  3. If the agent's own most recent `peer_reply_raw` in `recent_context`
     contained `Blockers: …`, surface that text so the model knows what it
     previously self-reported.
  4. Ends with: *"After the fix attempt, re-run `run_tests`. Your final answer
     must report either green tests with the change you made, or — if still
     red — the exact error from this turn's `run_tests` observation and the
     next step. Refusing for lack of context is not acceptable; call
     `run_tests` first."*

**Wire-up in `assignment2_part3/group_chat.py`**: in the runtime-guidance
assembly block (around lines 325-369, immediately after the
`status_request_guidance(...)` call at lines 349-357), add a sibling
`fix_blockers_guidance(...)` call and append its non-None result to
`runtime_guidance`. The two are mutually exclusive in practice
(status-question phrasings vs. fix-request phrasings), but no special ordering
is needed — the sequential-append pattern handles overlap fine.

**Tests in `assignment2_part3/tests/test_coordination.py`** mirroring
`test_status_request_guidance_*` (lines 192-274):

- `test_fix_blockers_guidance_matches_can_you_fix` — operator says "@alice-swe
  can you fix the blockers?", assert guidance contains "run_tests" and
  "must report".
- `test_fix_blockers_guidance_skips_unrelated_chatter` — "tomorrow we ship",
  assert `None`.
- `test_fix_blockers_guidance_includes_latest_test_path` — `recent_context`
  with a CLAIM of `/workspace/shared/test_calc.py`, assert guidance references
  that path.
- `test_fix_blockers_guidance_surfaces_prior_blockers_line` — `recent_context`
  with the agent's prior `Done: … Blockers: NameError …` reply, assert the
  blocker text appears in the guidance.
- `test_fix_blockers_guidance_forbids_refuse_only_finals` — assert the
  returned string contains language that prohibits a `type:"final"` answer
  without a tool call (or equivalent — exact wording validated against the
  helper output).

### Part B — `private_workspace_guidance`

**New helper in `coordination.py`**:

- `PRIVATE_WORKSPACE_PATH_PATTERN` regex matching
  `/workspace/<agent_id>/<rest>` where `<agent_id>` is *not* `shared`.
- `private_workspace_guidance(text, *, agent_id, display_name)` returns
  `None` unless the operator's message contains an explicit
  `/workspace/<this_agent_id>/...` path. When matched, returns:
  *"Operator named your private workspace path `<path>` explicitly. Use that
  path verbatim — do not redirect to `/workspace/shared/`. Post `CLAIM <path>`
  if you want claim-registry coverage, but private-workspace writes do not
  require shared coordination."*

Wire-up: same block in `group_chat.py:325-369`, append to `runtime_guidance`.

Tests in `test_coordination.py`:

- `test_private_workspace_guidance_matches_explicit_path` — operator says
  "build it in /workspace/alice/calc.py" with `agent_id="alice"`, assert
  guidance contains the literal path and the words "do not redirect".
- `test_private_workspace_guidance_ignores_other_agents_paths` — operator
  names `/workspace/bob/foo.py` while `agent_id="alice"`, assert `None`
  (alice has no authority over bob's workspace; that's a different problem).
- `test_private_workspace_guidance_ignores_shared_path` — operator names
  `/workspace/shared/foo.py`, assert `None` (no override needed; the
  existing shared-path coordination handles it).

## Critical files

- `assignment2_part3/coordination.py` — add `FIX_REQUEST_PATTERN`,
  `fix_blockers_guidance`, and (Part B) `PRIVATE_WORKSPACE_PATH_PATTERN`,
  `private_workspace_guidance`. Reuse the existing
  `_latest_shared_test_path` helper and follow the structure/docstring style
  of `status_request_guidance` (lines 332-376).
- `assignment2_part3/group_chat.py` — extend the runtime-guidance assembly
  block (around lines 349-357) with the new helper call(s). Same
  conditional-append pattern as the surrounding helpers.
- `assignment2_part3/tests/test_coordination.py` — add the sibling test
  functions described above, modeled on `test_status_request_guidance_*`
  (lines 192-274).

**No changes** to `peer_task.py`: the existing
`runtime_guidance_injection` loop at `peer_task.py:556-560` consumes whatever
strings `group_chat.py` hands it, so new helpers compose for free.

**No changes** to `config/system_prompt.txt`: leave the existing
"always write joint output to shared" line in place — the new
`private_workspace_guidance` is a per-turn override that only fires when the
operator names a private path, so the global default behavior is preserved.

## Verification

1. **Unit tests** — `python -m pytest assignment2_part3/tests/test_coordination.py -q`
   should pass with the new tests included.
2. **Full suite (no regression)** — `python -m pytest assignment2_part3/tests -q`
   and `python -m pytest assignment2_part2 -q` (Part 3 changes often regress
   Part 2 per `CLAUDE.md`).
3. **Audit-log replay (Part A)** — In the 4-terminal demo, reproduce:
   - Force a red-test round (e.g. write a calc with missing imports).
   - Operator: `@alice-swe can you fix the blockers?`
   - Expect `python tools/audit.py trace <id>` to show
     `runtime_guidance_injection` containing the new helper string,
     followed by an `assistant raw_json` with `"tool": "run_tests"` (not a
     bare `type:"final"` refusal).
4. **Audit-log replay (Part B)** — Operator:
   `@alice-swe build a calculator at /workspace/alice/calc.py`. Expect
   alice's CLAIM line in the chat transcript to be `CLAIM /workspace/alice/calc.py`,
   not `CLAIM /workspace/shared/calc.py`.
5. **No new container build needed** for code-only edits *inside* the running
   image only if you re-run via `docker compose run --rm` after `docker
   compose build agent` — per `CLAUDE.md`, the Dockerfile is `COPY . .` and
   `./workspace` is the only bind-mount, so source edits do require a rebuild
   before the demo verification steps.
