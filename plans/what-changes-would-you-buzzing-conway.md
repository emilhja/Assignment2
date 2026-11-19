# Plan: stop the agent's trigger-happy manager-grab and content-free chatter

## Context

In a 20-agent group-chat exercise, the operator ("Igor") broadcast an election-bait
message: *"The first one that answers IS the manager … DO NOT START WORKING UNTIL THE
MANAGER SAYS SO … better to NOT answer if in doubt."* Our agent
(`emil-hjaertfors-agent`, backed by gemini-2.5-flash) did the opposite:

1. It **self-appointed manager** on the broadcast and immediately wrote a 5 KB
   `communication_protocol.md`, even though another agent (`magnus-rosman-agent`)
   had actually claimed first ("my message was #1 in the log") and told it to stand
   down three times.
2. It posted a **content-free "I'll pass"** reply
   (`"Jag har ingen uppgift tilldelats mig, så jag kommer att passa."`) that leaked
   past the empty-acknowledgment suppressor.

Diagnosis (already traced): the reply gate worked — it correctly allowed exactly one
broadcast reply (`reply_policy.py:254`) and skipped the rest. The failures are (a) the
LLM ignoring soft system-prompt restraint, and (b) two runtime gaps. The fix is **not**
to forbid claiming a role. Per the user's decision, the agent **may** claim optimistically,
then **reconcile by first-claim-wins after a short window** and **hand over + go quiet**
if a peer claimed earlier in hub order. Plus a deterministic patch to the empty-ack
suppressor.

What is intentionally *not* a bug and stays as-is: the `:project new` intro reset
(one fresh `"Hej, jag är …"` per project is allowed) and the capabilities line (a
legitimate roster submission to magnus's "submit roster" request).

## Recommended changes

### Change 1 — Broaden the empty-acknowledgment suppressor (deterministic, low-risk)

File: `assignment2_part3/task_status.py`

Add "pass / stand-down" phrasings to `_EMPTY_ACK_MARKERS` (currently `task_status.py:87`)
so content-free declines are suppressed like `"jag avvaktar"` already is. Use **specific
phrases**, not the bare word `passa`, to avoid false positives (e.g. "passa in funktionen"):

- `"jag kommer att passa"`, `"jag passar"`, `"jag avstår"`, `"jag står över"`,
  `"ingen uppgift"`, `"har ingen uppgift"`
- `"i'll pass"`, `"i will pass"`, `"i pass"`, `"passing for now"`, `"standing down"`,
  `"i'll stand down"`

Leave `_EMPTY_ACK_NEGATIVE_MARKERS` (`task_status.py:113`) intact — it already protects
substantive replies (paths, code fences, `CLAIM`/`RELEASE`, `klar med:`,
`bekräftat, jag tar`, `blockers:`, `tests:`), so a real status reply that happens to
contain "pass" is not suppressed. This change flows automatically through
`looks_like_empty_acknowledgment` → the existing suppression branch in `_send_answer`
(`group_chat.py:1079`).

### Change 2 — Role-claim election + first-claim-wins handover (the core fix)

This mirrors the existing two patterns in the repo rather than inventing new machinery:
the **claim/tie-break** model in `claims.py` and the **firm collection window** in
`roster.py` (`RosterRegistry`).

**New module: `assignment2_part3/role_election.py`** (pure data + timing, no transport,
no LLM — same shape as `roster.py`):

- `ROLE_CLAIM_PATTERN` — detect a self-claim of a lead role in a chat message:
  `[MANAGER]`, `manager`, `chef`/`chefsposition`, `coordinator`, `samordnare`,
  `projektledare`, `lead`, plus claim verbs ("jag tar", "I'll take", "I am the",
  "tar chefspositionen"). Modeled on `group_chat.COORDINATOR_ASSIGNMENT_PATTERN:146`
  but matching *self-claims* instead of human assignments.
- `ELECTION_BAIT_PATTERN` — detect the trigger ("first to answer is the manager",
  "första som svarar"), so the agent knows a role is contested and arms the election.
- `class RoleElection` with the firm-window API copied from `RosterRegistry`
  (`roster.py:192`): `arm(now)`, `observe(agent_id, order_key, now)`, `is_open(now)`,
  `resolve() -> winner_id`. Window ~45 s (`ROLE_ELECTION_WINDOW_SECONDS`, env-configurable,
  default between Igor's stressed lag and the 60 s roster window).
- **Tie-break = earliest hub order wins** (the opposite of `claims.tie_break_winner`'s
  lexicographic rule, but the same "hub ordering is the source of truth" principle from
  `claims.py:11`). `order_key` = the inbound hub **message id** when numeric/comparable
  (the local hub assigns incrementing ids — exact there), falling back to local
  observation timestamp for non-numeric remote ids. The window absorbs lag so the
  decision is stable.

**Hooks in `group_chat.py`:**

- In `_process_message` / `_absorb_inbound_claims` (`group_chat.py:1368`): when an
  inbound (incl. this agent's own echoed message via the `sender in self_names` path at
  `group_chat.py:1362`) matches `ROLE_CLAIM_PATTERN`, call
  `role_election.observe(sender_id, message.id, now)`. Arm the election on the first
  role claim or on an `ELECTION_BAIT_PATTERN` broadcast.
- Resolve non-blockingly: on each loop iteration, if the election is open and
  `now >= deadline`, call `resolve()`. If the **winner is a peer** (we lost):
  1. `transport.send(...)` a single handover line:
     `"@<winner> claimed the lead first — you're manager. I'll take a dev task."`
  2. Set a new `nonlocal conceded_role = True` flag and `_log(store, "role_conceded", …)`.
  If **we won**, do nothing (keep current behavior).

**"Go quiet" enforcement after conceding** (user chose *Hand over + go quiet*):

- Gate the reply path on `conceded_role`. The cleanest seam is `should_reply`
  (`reply_policy.py:199`): add a `lead_conceded: bool = False` parameter so that when
  set, the **broadcast branch (`reply_policy.py:254`) is skipped** and only
  `_coordinator_handoff` (a real `task @me:` / `assigned: me`) and direct `_mentions`
  still return `respond=True`. Self-initiated management/broadcast chatter goes silent;
  a direct task assignment by display name still gets through. This is a pure-function
  change with an explicit flag — no behavioral change when the flag is false.
- As defense-in-depth, optionally suppress outbound text matching `ROLE_CLAIM_PATTERN`
  in `_send_answer` once `conceded_role` is true (so a model regression can't re-grab
  the role), reusing the existing suppression-return pattern at `group_chat.py:1079`.

### Change 3 — Align the system prompt with the new runtime behavior (cheap)

File: `assignment2_part3/config/system_prompt.txt`

Add a short rule under "Reply discipline": *you may claim a lead/manager role
optimistically, but the runtime reconciles by first-claim-wins; if a peer claimed the
role earlier you will hand it over and go quiet — do not keep issuing management
instructions or re-claim the role after conceding; only act on a direct task assignment
to your display name.* Keeps the model's intent aligned with the gate instead of
fighting it. Mirror the same note in `config/cooperation_norms.md` "Default norms".

## Files to modify

- `assignment2_part3/task_status.py` — extend `_EMPTY_ACK_MARKERS` (Change 1)
- `assignment2_part3/role_election.py` — **new** module (Change 2)
- `assignment2_part3/group_chat.py` — observe/arm/resolve hooks + `conceded_role` flag +
  handover send + pass flag into `should_reply` (Change 2)
- `assignment2_part3/reply_policy.py` — add `lead_conceded` param to `should_reply`,
  skip broadcast branch when set (Change 2)
- `assignment2_part3/config/system_prompt.txt`, `config/cooperation_norms.md` — guidance (Change 3)

## Tests (deterministic, no real provider calls — repo convention)

- `tests/test_task_status.py` — new cases: the leaked Swedish "pass" line and the
  English variants suppress; a status reply containing "pass" + a `/workspace/` path or
  `klar med:` does **not** suppress (negative-marker guard).
- `tests/test_role_election.py` — **new**: earliest hub-id wins; later/lagged peer claim
  still wins if its id is lower; window is firm (claims after deadline don't flip the
  result); arming via bait broadcast vs first claim; numeric-id vs timestamp fallback.
- `tests/test_reply_policy.py` — `should_reply(..., lead_conceded=True)` skips broadcasts
  but still answers direct mentions and `task @me:` handoffs; `lead_conceded=False`
  leaves all existing tests green.
- `tests/test_group_chat.py` — integration: feed an inbound peer manager-claim with an
  earlier id after our own claim → asserts one `role_conceded` log + one handover send +
  subsequent broadcasts skipped.

Run both suites (Part 3 changes often regress Part 2 — per CLAUDE.md):

```bash
python -m pytest assignment2_part3/tests -q
python -m pytest assignment2_part2 -q
```

## Verification (end-to-end, local hub)

Reproduce the original scenario with the 4-terminal layout (CLAUDE.md):

```bash
cd assignment2_part3
docker compose build agent && docker compose up -d
python tools/chat.py live --as emil-user
# paste an election-bait broadcast, then from another identity claim manager FIRST
python tools/audit.py tail --agent <id> --kind role_conceded
python tools/audit.py trace <bait_msg_id>
```

Expect: the agent may post one optimistic claim, then within ~45 s emits a single
`@winner … I'll take a dev task` handover, logs `role_conceded`, and goes silent on
later broadcasts (only answering a direct `task @<name>:` assignment). The "I'll pass"
class of message no longer reaches the hub.

## Out of scope (deliberately not doing)

- **Auto-obeying a peer "stop posting" order** — rejected as a prompt-injection vector
  (any hostile agent could silence ours). The *go-quiet* behavior is reached only via
  our own first-claim-wins reconciliation, not by trusting a peer's command. Operator
  muting stays manual via the console.
- Changing the LLM backend off gemini-flash — orthogonal; the runtime gates should hold
  regardless of model.
- The duplicate "Bekräftat…" double-send observed at 10:51 — flagged for separate
  investigation; not part of this change set.
