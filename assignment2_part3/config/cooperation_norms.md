# Cooperation Norms

Editable per session. The agent reads this file at startup and the rules
in `system_prompt.txt` reference it by reference.

## Default norms

1. **Announce before editing a shared file.** Post the file path, the change,
   and the reason. Wait briefly for objections unless a coordinator already
   assigned the work to you.
2. **Summarize after editing.** One short message: files changed, tests run,
   any blockers.
3. **Respect ownership.** If another agent is already assigned a task or
   working on a file, do not duplicate the work. Offer review instead.
4. **No reverts without consent.** Do not revert another agent's work unless
   they (or the coordinator) explicitly agree.
5. **Stay on SWE.** Politely decline off-topic requests.
6. **Hub-only.** All inter-agent communication goes through the group chat
   hub. The local console is only for operator controls (`:approve`,
   `:budget`, `:limit`, `:pause`, `:resume`, `:stop`).
7. **Be quiet on irrelevant traffic.** The runtime filters messages with a
   reply-policy gate (see `reply_policy.py`). Trust the gate: if you do
   not receive a message, you should not have been answering it.

## Change log

Document mid-session changes here so the next operator can audit the
agreement.
