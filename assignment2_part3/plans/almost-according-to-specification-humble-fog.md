# Stop the second writer from breaking shared test-file imports

## Context

The 2026-05-25 06:12–06:16 calculator demo ran almost to spec: alice and
bob each emitted a properly scoped CLAIM, both wrote their assigned
functions into `/workspace/shared/calculator.py`, the DEFER ping-pong
fix from `a754af5` held (one DEFER per side, no loop), and the operator
got "Done:" replies on demand. The artifact at
`workspace/shared/calculator.py` is correct — all four functions are
defined.

The "almost" is in `workspace/shared/test_calculator.py`:

```python
import pytest
from calculator import add, subtract     # <- never extended

def test_add(): ...
def test_subtract(): ...
def test_multiply(): assert multiply(2, 3) == 6   # NameError at runtime
def test_divide():  assert divide(10, 2) == 5      # NameError at runtime
```

alice wrote the file first under `#add-subtract-tests` with a 2-symbol
import. bob then claimed `#multiply-divide-tests`, saw the file exists,
and used `append_text` for his test bodies — but left the `from
calculator import add, subtract` line untouched. His own `run_tests`
observation came back as `NameError: name 'multiply' is not defined`
(this is the "Tests: ran and failed. Blockers: …" line he reported to
the operator).

The race-hint in `coordination.assignment_guidance` (coordination.py
lines 188-194) already tells the second writer to prefer `append_text`
/ `edit_section` over `create_file` for existing shared files, but it
says nothing about **module-level imports** on shared *test* files,
where additive content also requires extending the `import` line. That
is the single gap to close. Implementation file `calculator.py` and the
claim/defer machinery do not need to change.

Out of scope (explicitly): the `run_tests`-after-write enforcement
already designed in `plans/yes-please-virtual-cookie.md`, the
step-budget fixes proposed in
`assignment2_part3/plans/delegated-wibbling-abelson.md`, and broadening
the `SIGNATURE_AGREEMENT_PATTERN` to cover the operator's "state
agreement on signatures" phrasing. Each is a separate plan and they
stay deferred.

## Approach

Extend the pytest sidecar guidance in `coordination.py` so that, when a
coordinator plan calls for pytest coverage AND multiple peers are
assigned to the shared source file (so a test-file race is possible),
the per-agent runtime hint also instructs:

> If the shared test file already exists when you go to write, do not
> append-only. Call `read_file` first, then use `replace_text` on the
> `from <module> import …` line to extend it with the symbol(s) you
> are about to test, and only then add your new test functions.
> Otherwise pytest fails with `NameError`.

The guidance fires for every assigned agent (not just "the second
one") because no agent knows at parse time whether it will be first or
second to land on the test file — making both check `read_file` first
is cheap and idempotent.

## Files to modify

1. `assignment2_part3/coordination.py`

   Extend `_pytest_sidecar_guidance` (currently coordination.py:150-160)
   so the returned string also carries the import-extension reminder
   when (a) a test path exists for the shared source AND (b) the
   coordination plan has more than one assignment. Pseudocode:

   ```python
   def _pytest_sidecar_guidance(text: str, path: str, own: Assignment,
                                peer_count: int) -> str:
       if PYTEST_REQUEST_PATTERN.search(text) is None:
           return ""
       test_path = _test_path_for_source(path)
       if not test_path:
           return ""
       base = (
           " Pytest coverage was requested next to the shared file. After "
           f"completing the implementation write, use a separate CLAIM for "
           f"{test_path}#{own.scope}-tests before creating or editing tests "
           "for your scope."
       )
       if peer_count == 0:
           return base
       return base + (
           f" If {test_path} already exists when you go to write, do not "
           "append-only. Call read_file on it first, then use replace_text "
           "on the `from <module> import …` line to add the symbol(s) you "
           "are about to test before adding your test functions — otherwise "
           "pytest fails with NameError on the symbols your peer did not "
           "import."
       )
   ```

   Caller change in `assignment_guidance` (coordination.py:163-205) —
   pass `peer_count=len(peer_bits)` (or equivalent — the variable is
   already built one line above the call).

2. `assignment2_part3/tests/test_coordination.py`

   Add two unit tests around the existing
   `_pytest_sidecar_guidance` / `assignment_guidance` coverage:

   - `test_pytest_sidecar_warns_about_extending_imports_when_peers_present`:
     plan text `"@bob @alice build /workspace/shared/calculator.py. alice
     writes add+subtract, bob writes multiply+divide. Add pytest tests."`
     → guidance for bob must contain both `replace_text` and `import` and
     reference `/workspace/shared/test_calculator.py`.

   - `test_pytest_sidecar_skips_import_warning_when_solo`:
     plan text with only one assignment → guidance must NOT contain the
     `replace_text` import sentence. Locks in the "only when a race is
     possible" condition.

## Critical files (read-only context)

- `assignment2_part3/coordination.py` — `_pytest_sidecar_guidance`
  (lines 150-160), `assignment_guidance` (lines 163-205), and the
  existing `race_hint` block (lines 188-194) that the new wording sits
  next to in the agent's prompt.
- `assignment2_part3/config/system_prompt.txt` — `Claim/defer protocol`
  block at lines 54-68 already covers "use append_text or
  edit_section/replace_text" but does not mention imports; the new
  guidance is per-task, not added to the static prompt.
- `assignment2_part3/peer_task.py` — `_maybe_shared_write_refusal`
  (lines 221-256) is the existing claim-block site; unchanged.
- `assignment2_part3/workspace/shared/test_calculator.py` — the
  artifact that demonstrates the bug; safe to delete before re-running
  the demo so the second writer actually sees an existing-file
  condition next round too.

## Verification

```bash
# 1. New unit tests pass; no existing coordination tests regress.
python -m pytest assignment2_part3/tests/test_coordination.py -q

# 2. Full Part 3 suite stays green.
python -m pytest assignment2_part3/tests -q

# 3. Part 2 regression suite (Part 3 changes have regressed Part 2 before).
python -m pytest assignment2_part2 -q

# 4. End-to-end smoke in the 4-terminal docker setup.
cd assignment2_part3
docker compose down
rm -f workspace/shared/test_calculator.py workspace/shared/calculator.py
docker compose build agent
docker compose up -d
python tools/chat.py say --as emil-user \
  "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. \
   alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM with \
   the function names in the scope. Write pytest tests next to it."
python tools/chat.py live --as emil-user
# Expect: workspace/shared/test_calculator.py ends with a single import
# line that names add, subtract, multiply, divide (in any order), and
# bob's "Done:" line reports `Tests: ran and passed` rather than the
# previous NameError blocker.

# 5. Audit confirms the new guidance text was injected.
python tools/audit.py tail --agent bob --kind assignment_guidance | head
```

Acceptance: bob's `run_tests` observation in the new run is green (or
fails on the `ZeroDivisionError` vs `ValueError` mismatch — that is a
separate contract bug, also out of scope here; the NameError-on-imports
symptom must be gone).
