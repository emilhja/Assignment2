# Plan: Fix mutual-defer deadlock and stale file-state perception

## Context

In a live demo of the part3 multi-agent chat, two coordination bugs surfaced beyond LLM rate-limiting:

1. **Mutual-defer deadlock.** Alice and Bob each posted `DEFER to @<peer>` for 5+ consecutive turns instead of one of them proceeding. The tie-break rule lives only in `system_prompt.txt` lines 62-65 (lexicographic AGENT_ID comparison) and is purely model-driven — neither LLM applied it.
2. **Stale file-state perception.** Bob asserted "multiply/divide functions are missing" by reasoning from memory rather than re-reading `calculator.py`. Alice later asserted she had "merged multiply/divide with add/subtract" — also without re-reading. There is no `read_file` tool, and the system prompt does not mandate verifying file state before claiming a scope or asserting contents.

The user has already shipped a partial fix in working-tree changes (uncommitted):

- `config/system_prompt.txt`: added "do not DEFER for different scopes" and "do not recreate existing shared file with create_file" rules.
- `peer_task.py` `_maybe_shared_write_refusal()`: blocks `create_file` overwrite of an existing shared file under a scoped claim; switched `is_claimed_by_other(path, ...)` to `is_claimed_by_other(own_claim.target, ...)` so scope-aware conflict detection works.
- `tests/test_peer_task.py`: two new tests covering both behaviors.

This plan covers the **remaining work**: code-driven tie-break enforcement, a mutual-defer guard, a new `read_file` tool, and prompt rules that mandate reading before asserting.

## Approach

### 1. Code-driven tie-break + mutual-defer guard (bug 1)

**`assignment2_part3/claims.py`** — extend the registry:
- Add `tie_break_winner(self_id: str, peer_id: str) -> str` returning the lexicographically smaller id. Single source of truth; reuses the rule already in the prompt.
- Add `DEFER_PATTERN = re.compile(r"(?im)^\s*DEFER\s+to\s+@?(?P<target>[\w.-]+)")`.
- Extend `ClaimRegistry` with a `_defers: dict[tuple[str, str], int]` map keyed by `(observer_self_id, deferred_to_peer)`, tracking consecutive defer counts on a given path.
- Extend `absorb_text()` to also parse DEFER lines and update the counter.
- Add `mutual_defer_detected(self_id, peer_id, path) -> bool`: True if both `self → peer` and `peer → self` defers have been observed for the same path within the active claim window.
- Add `reset_defers_for(path)` called on RELEASE or successful write to the path.

**`assignment2_part3/reply_policy.py`** — enrich the collision signal:
- Change `_claim_collision()` to return `Optional[CollisionInfo]` (new dataclass) carrying `path`, `peer_id`, and `outcome: Literal["self-wins", "self-loses"]` (computed via `tie_break_winner`). Existing tests that check the contested-path string keep working via a `.path` attribute.
- `should_reply()` still bypasses the cooldown when a collision exists; passes the collision info through `ReplyDecision` via a new optional `collision: Optional[CollisionInfo]` field (default `None`).

**`assignment2_part3/peer_task.py`** — inject runtime guidance:
- Accept `collision: Optional[CollisionInfo]` in `run_peer_task()` (passed from `group_chat._process_message`).
- Before the LLM round-trip, if a collision exists, append a "runtime" user message:
  - `self-wins`: `"You hold the tie-break for <path>#<scope> (your AGENT_ID is lexicographically smaller than @<peer>). Do NOT post DEFER. Proceed with your claim and write."`
  - `self-loses`: `"You lost the tie-break to @<peer> for <path>#<scope>. Post exactly two lines and stop: 'DEFER to @<peer>' then 'RELEASE <path>#<scope>'. Propose a non-overlapping scope on your next turn."`
- Also inject when `claims.mutual_defer_detected(self_id, peer_sender, path)` is true: `"Mutual-defer detected on <path>. Apply tie-break: @<winner> must re-claim and proceed; @<loser> must release and propose a non-overlapping scope."`
- Cap defer self-reinforcement at **2 consecutive same-path defers** before the runtime forces tie-break injection regardless of LLM output.

**`assignment2_part3/group_chat.py`** — wire the collision info:
- In `_process_message()`, capture `decision.collision` and pass it to `_run_task_for_message` → `run_peer_task`.

### 2. `read_file` tool + pre-edit/pre-assert read mandate (bug 2 completion)

**`assignment2_part2/tools.py`** — add the tool:
- `def read_file(path: str, max_bytes: int = MAX_OUTPUT_CHARS) -> str`: resolves via `_resolve_workspace_path`, reads UTF-8 text, truncates to `MAX_OUTPUT_CHARS` using existing `_truncate`. No shell, no operator approval. Refuses non-files and missing paths with the existing `Edit blocked:` prefix style so the agent's "do not retry on Edit blocked" rule applies.
- Register in `TOOL_REGISTRY` as `"read_file"` with `required_args=("path",)`.

**`assignment2_part3/config/system_prompt.txt`** — add the tool and mandate reads:
- Add `read_file` to the "Available tools" listing (around line 13-17).
- After line 58 (the existing "Read the current file" guidance), add a stronger rule:
  - `"Before posting a CLAIM for a scope on an existing shared file, call read_file on that path so you reason from current contents, not memory."`
  - `"Before asserting in a final answer that a shared file contains, lacks, or was changed in some way, you MUST have a read_file or successful edit_section/replace_text/create_file tool_observation for that path within the current round; otherwise say you need to re-read."`

**`assignment2_part3/peer_task.py`** — soft enforcement (optional safety net):
- Extend the existing `_looks_like_write_success_claim` pattern to also flag answers that assert file contents (`"the file contains"`, `"functions are missing"`, etc.) when no `read_file`/successful edit observation for that path occurred this round. Log a `system/state_assertion_unverified` event but don't rewrite the answer — the prompt mandate is the primary fix.

### 3. Tests

- `assignment2_part2/tests/test_tools.py`: tests for `read_file` (happy path, missing file, dir path, oversize truncation, path-outside-workspace refusal).
- `assignment2_part3/tests/test_claims.py`: `test_tie_break_winner`, `test_defer_absorbed`, `test_mutual_defer_detection_resets_on_release`.
- `assignment2_part3/tests/test_reply_policy.py`: `test_claim_collision_returns_self_wins`, `test_claim_collision_returns_self_loses`.
- `assignment2_part3/tests/test_peer_task.py`: `test_collision_self_wins_injects_proceed_guidance`, `test_collision_self_loses_injects_defer_release_guidance`, `test_mutual_defer_forces_tie_break_after_two_turns`.

## Critical files to modify

- `assignment2_part3/claims.py` — registry extensions
- `assignment2_part3/reply_policy.py` — collision-info dataclass
- `assignment2_part3/peer_task.py` — runtime guidance injection
- `assignment2_part3/group_chat.py` — pass collision through
- `assignment2_part3/config/system_prompt.txt` — register `read_file`, mandate pre-claim/pre-assert reads
- `assignment2_part2/tools.py` — `read_file` tool
- Tests as listed above

## Reusable code referenced

- `claims.CLAIM_PATTERN`, `claims.RELEASE_PATTERN`, `claims.split_claim_target` (claims.py:26-55) — pattern style + scope parsing for `DEFER_PATTERN`.
- `tools._resolve_workspace_path`, `tools._truncate`, `tools.MAX_OUTPUT_CHARS` (tools.py:53-117) — used by `read_file`.
- `peer_task._refusal_observation`, `peer_task._json` — reuse for the runtime guidance message envelope.
- Existing `_looks_like_write_success_claim` (peer_task.py:198-203) — extend rather than duplicate.

## Verification

End-to-end:
1. `cd assignment2_part3 && pytest tests/ -x` — all existing tests pass; new tests pass.
2. `cd assignment2_part2 && pytest tests/test_tools.py -x` — `read_file` tests pass.
3. **Live replay**: `docker compose up -d local-hub`, start alice + bob agents, run `python tools/chat.py live --as emil-user` and re-issue the transcript's prompt: `@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: alice writes add+subtract, bob writes multiply+divide. Use the CLAIM/RELEASE protocol`.
   - Expect: no mutual-defer loop. If both agents race the same scope, exactly one (alice, lex-smaller) keeps the claim; the other posts `DEFER` + `RELEASE` once.
   - Expect: when an agent claims a scope on an existing `calculator.py`, it first emits `read_file` before `edit_section`.
   - Expect: no "functions are missing" assertions follow a successful peer write without a fresh `read_file` observation.
4. Targeted negative test: have alice and bob both claim `#multiply-divide` simultaneously via the test transport in `test_peer_task.py`; verify only one writes.
