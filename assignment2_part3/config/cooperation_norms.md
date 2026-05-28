# Cooperation Norms

Editable per session. The agent reads this file at startup and the rules
in `system_prompt.txt` reference it by reference.

## Default norms

0. **Use the shared workspace for joint work.** Each agent has a private
   workspace at `/workspace/<agent_id>` that no peer can read. Files meant
   for collaboration must live under `/workspace/shared/` and be referenced
   by that full path in chat.
1. **Claim-then-write protocol for shared files.** Before invoking any
   tool that writes to `/workspace/shared/<path>`, post a single line on
   its own in chat: `CLAIM /workspace/shared/<path>: <one-line reason>`.
   If you see another agent's `CLAIM` for the same path and the work
   would overlap, reply `DEFER to @<agent>` and offer review instead of
   writing. When done, post `RELEASE /workspace/shared/<path>`. The
   runtime gates shared writes against this registry; if a tool comes
   back with `refused: deferred: ...`, do not retry — post the DEFER
   line and stop. Claims expire after 5 minutes.
2. **Summarize after editing.** One short message: files changed (full
   `/workspace/shared/...` paths), tests run, any blockers.
3. **Respect ownership.** If another agent is already assigned a task or
   working on a file, do not duplicate the work. Offer review instead.
4. **No reverts without consent.** Do not revert another agent's work unless
   they (or the coordinator) explicitly agree.
5. **Stay on SWE.** Politely decline off-topic requests.
6. **Hub-only.** All inter-agent communication goes through the group chat
   hub. The local console is only for operator controls (`:approve`,
   `:budget`, `:limit`, `:pause`, `:resume`, `:continue`, `:stop`).
7. **Be quiet on irrelevant traffic.** The runtime filters messages with a
   reply-policy gate (see `reply_policy.py`). Trust the gate: if you do
   not receive a message, you should not have been answering it.

## Change log

Document mid-session changes here so the next operator can audit the
agreement.
