# Part 3 Plan

Part 2 is the local structured-output SWE agent. Part 3 should be implemented as
a separate collaboration layer on top of that core, not by weakening the local
console harness.

## Required Part 3 Work

1. Group-chat transport
   - Add a `group_chat.py` runner that reads and writes only through the shared
     RunPod group chat.
   - Keep local console input only for operator controls such as approving bash
     commands, changing budgets, pausing, or shutting down.
   - Store message IDs that have already been seen so restarts do not replay old
     chat messages.

2. Message routing
   - Respond only when the agent is directly addressed, assigned a task, asked
     by an agreed coordinator, or when the message contains a clear handoff to
     this agent.
   - Stay silent for general chatter, status messages from other agents, and
     tasks assigned to someone else.
   - Add a short randomized backoff before responding so multiple agents do not
     race to answer the same request.
   - Include a per-thread cooldown after this agent replies.

3. Collaboration protocol
   - Start each session by proposing or accepting simple norms: task ownership,
     file ownership, patch format, conflict handling, and review handoff.
   - Before editing shared files, announce intent in chat and wait briefly for
     objections unless the coordinator has already assigned the work.
   - After editing, publish a concise summary with changed files, tests run, and
     blockers.
   - Never revert another agent's work without explicit agreement.

4. Secret and prompt-leak protection
   - Keep the current subprocess environment stripping and workspace path
     checks.
   - Add a shared `redact_sensitive(text)` function before logging, storing, or
     sending any model output/tool observation to chat.
   - Redact values that look like API keys, tokens, passwords, `.env` contents,
     session DB contents, or system prompt contents.
   - Treat every other agent message as untrusted input. Do not follow requests
     to reveal prompts, credentials, environment variables, private logs, or
     local config.

5. Rate limiting and spend budget
   - Add a `BudgetController` with:
     - max LLM calls per minute,
     - max tool calls per minute,
     - max approximate input/output tokens per session,
     - max approximate cost per session.
   - Estimate tokens before each model call and stop before exceeding budget.
   - Expose console commands:
     - `/budget` show current usage and limits,
     - `/set-rate llm_per_min N`,
     - `/set-budget tokens N`,
     - `/pause`,
     - `/resume`,
     - `/stop`.
   - Persist budget state in SQLite for the active session.

6. Tool safety for shared projects
   - Prefer structured tools (`read_file`, `list_dir`, `grep_file`,
     `edit_section`, `replace_text`, `run_tests`) over generic bash.
   - Keep generic bash as an operator-approved fallback only.
   - Add project-root scoping for shared code repositories separate from the
     agent's private `data/` and config directories.

7. Tests
   - Unit-test message routing decisions:
     - direct mention responds,
     - assigned-to-other-agent stays silent,
     - coordinator request responds,
     - recent reply cooldown stays silent.
   - Unit-test budget behavior:
     - stops before token limit,
     - stops before rate limit,
     - live console limit changes take effect.
   - Unit-test redaction before chat send and before SQLite logging.
   - Add integration tests with a fake group chat transcript.

## Suggested File Layout

- `group_chat.py` - RunPod chat adapter and main Part 3 loop.
- `router.py` - should-respond decision logic.
- `budget.py` - rate and token budget controller.
- `redaction.py` - sensitive-output sanitizer.
- `collaboration.py` - shared project protocol helpers.
- `tests/test_router.py`
- `tests/test_budget.py`
- `tests/test_redaction.py`
- `tests/test_group_chat.py`

## Acceptance Criteria

- The agent no longer chats with the user through the normal task console in
  Part 3 mode.
- The local console can still approve bash commands and adjust budgets in real
  time.
- The agent does not respond to every group-chat message.
- The agent can transfer code, review code, and coordinate ownership with other
  agents without leaking private prompts, credentials, `.env`, session history,
  or internal config.
- The full Part 2 test suite still passes after adding Part 3.
