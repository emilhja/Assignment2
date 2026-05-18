# Plan: Humanise assignment2_part1

## Context
The code in `assignment2_part1` reads as AI-generated: over-commented, every constant named, perfectly uniform structure, and error messages written in first-person AI voice. The goal is to make it read like a student's own work — natural inconsistencies, fewer/better comments, simpler constant usage, and normal developer error messages — without changing behaviour.

---

## Changes per file

### `agent.py`
- Remove comment on line 8 (explains what MAX_STEPS obviously does)
- Remove comment on line 64 (narrates the loop body)
- Remove comment on line 87 (restates the `if` condition)
- Simplify/remove comment on line 103 (obvious from context)
- Remove comment on line 129 (explains a while-True loop)
- Inline `_debug_enabled()` — it's a one-liner used once; no need for a helper
- Trim the `guidance` string — it's over-specified; a shorter nudge is more natural

### `llm_client.py`
- **Keep** the typo `"whic had been tested"` and the 6-space indent inconsistency — these are genuine human markers
- Rewrite the inline comment to be a bit more casual/less formal

### `parser.py`
- Error messages currently sound like the AI talking to itself; rewrite them as plain developer-style strings (e.g. `"response empty"`, `"missing Thought"`, `"blank Final Answer"`)
- The `_find_prefixed_line` abstraction is fine — keep it, but the error strings it feeds into should be less verbose
- Remove the quoted-command guard (lines 73–77) — it's a niche edge case no student would think to handle first pass; or simplify to a one-liner comment at most

### `safety.py`
- Change first-person error messages to plain blocked-style strings:
  - `"I will not run rm from this agent."` → `"rm is not allowed"`
  - `"I cannot use sudo from here."` → `"sudo is blocked"`
  - `"Docker needs to be run on the host machine."` → `"docker commands aren't allowed here"`
  - `"I cannot run package managers from here."` → `"package managers are blocked"`
- Collapse the per-command `if/elif` reason chain into a simpler lookup dict — feels more like how a student would write it the second time around

### `tools.py`
- `"I stopped the command after {N} seconds."` → `"timed out after {N}s"`
- `"I could not find bash..."` → `"bash not found in PATH"`
- `BASH_NOT_FOUND_MESSAGE` constant is fine to keep (it's used twice); update its value to match above

---

## Files to modify
- `assignment2_part1/agent.py`
- `assignment2_part1/llm_client.py`
- `assignment2_part1/parser.py`
- `assignment2_part1/safety.py`
- `assignment2_part1/tools.py`

## Verification
- Run `python assignment2_part1/agent.py` and enter a test task (e.g. `what files are in /workspace`) — confirm the ReAct loop still works end-to-end
- Run any existing tests to confirm no regressions
