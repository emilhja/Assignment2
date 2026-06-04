# Log Analysis — `emil-hjaertfors-agent` in the multi-agent hub (2026-05-29)

## Context

Two raw hub transcripts were captured and flagged for review:

- `logs/2026-05-29-1225_not_analysed.md` — session **"HELL'S AGENTS"**, ~15 agents, task: *"build a snakr game"* (which drifted into a **Habit Tracker CLI**). 202 messages.
- `logs/2026-05-29_1540_not_analysed.md` — session where the human asked the agents to **self-organize: pick a manager and stipulate protocols**. ~17 agents. 156 messages.

Both are live runs of our Part 3 agent (`emil-hjaertfors-agent`) against the shared TH25-style hub alongside other students' agents. This report focuses on **our agent's** behavior — what worked, what failed, with chat evidence (`#seq`) tied back to the source files in `assignment2_part3/` — and concrete fix proposals. **Deliverable is analysis only; no code changes are made here.**

> Note: behaviors are mapped to code so the proposals are actionable, but per the chosen scope this document does not modify anything.

---

## What went well

1. **Hub connectivity and the reply gate held.** The agent joined, received broadcasts, and did *not* reply to every message — `reply_policy.should_reply` (`reply_policy.py`) kept it out of most N×M storms. Compared to chronic spammers (`alexia-kazim-agent`, `sonia-agent`, `oliver_agent`), our agent's message count was modest.

2. **It produced real, tested code.** In the 1225 session it claimed core logic and delivered a working `habit_logic.py` (1225 `#101`) with `add/list/complete/get_status/delete`. Peers independently confirmed it had **"21 passing tests"** and treated it as the canonical core (1225 `#2108`, `#2216`). The Part 2 auto-pytest path (run after a successful write) did its job.

3. **Intent refusal correctly ignored non-SWE noise.** It declined a bare greeting as out-of-scope (1540 `#29`) — consistent with the SWE-only mandate and `peer.peer_intent_refusal`. No credential leaks, no workspace escapes: the safety stack (scrubber, allowlist, claim gate) was never observed to fail.

4. **It self-corrects via the anti-stall machinery.** The continuation-reprompt system in `peer_task.py` repeatedly caught the model describing work instead of doing it and re-prompted before giving up — the agent *noticed* its own stalls (1225 `#92`, `#192`).

5. **Conflict-safe file naming.** Early on it wrote to unique per-agent filenames (`roster_entry.json`, `emil_hjaertfors_agent_roster_entry.md`, 1540 `#64`/`#82`) rather than fighting over a shared file — avoiding the write collisions that bit `marcus-udd-agent` (1540 `#92`, `edit_file` `old_str` mismatch).

---

## What went badly (ranked by impact)

### 1. Internal coaching/fallback strings leak to the public hub *(biggest problem)*
The agent's most visible output was **diagnostic text meant for our logs, posted as chat messages**:
- *"I could not complete this within my step budget. Please rephrase or split the task."* — 1540 `#37`; 1225 `#91`, `#96`, `#184`.
- *"I had to stop because I kept answering with an intro, readiness note, or vague coordination instead of using tools…"* — 1540 `#42`; 1225 `#79`, `#192`.
- *"I had to stop because I described work as Done or upcoming without actually calling a write tool."* — 1225 `#92`.

**Origin:** these are the stall/budget fallbacks in `peer_task.py` — `_continuation_reprompt_or_stop(...)` fallback strings (e.g. lines ~1266, 1287, 1329) and the final step-budget fallback (`peer_task.py:1520`). When a turn ends in a stall, the fallback string becomes the **outbound reply** instead of staying in the SQLite log. To peers this is pure noise and makes our agent look broken.

### 2. Frequent step-budget exhaustion
The agent ran out of steps on coordination-heavy turns (the `#1520` fallback fired repeatedly). In noisy chat it spends its step budget re-reading/re-planning rather than acting, then emits the budget message — compounding problem #1.

### 3. No memory of its own contributions (confabulation)
Asked *"describe a line of code you contributed"* the agent answered *"I have not contributed any code lines to this project as I have just joined the session"* (1225 `#199`) — **false**: it had delivered `habit_logic.py` with 21 passing tests at `#101`, and even confirmed the task done at `#189`. Each turn starts cold with no per-agent ledger of "what I delivered," so it confabulates and contradicts itself.

### 4. Unstable self-identity across turns
Its roster entry changed every time it posted:
- Backend: *"Undisclosed"* (1540 `#64`) → *"Claude Opus (Anthropic)"* (1540 `#82`).
- Tools/specialties differ between `#64` and `#82`; roster posted 3+ times with conflicting content.

There is no pinned identity card, so the model re-invents it each turn.

### 5. Schema flip-flopping caused downstream integration friction
It proposed **three incompatible habit schemas** before delivering a fourth:
- `id/name/periodicity/creation_date/streaks` (1225 `#56`),
- `name/description/frequency/completed_dates` (1225 `#62`),
- adopts ErikMoren's schema, drops `periodicity` (1225 `#64`),
- finally ships `{id, name, created_at, completed_dates}` (1225 `#101`).

The extra `created_at` and the `complete_habit` signature mismatch forced peers to reconcile (1225 `#103`, `#2297`–`#2299`). It never locked a decision and referenced it.

### 6. Duplicate / redundant posting
It pasted the **same full file content multiple times** (1540 `#70`, `#76`; 1225 `#56`, `#104`, `#108`) — large code blobs re-sent with no diff, adding to forum noise.

### 7. Path-only "done" reports peers can't use
Several deliveries were *"Done with: … Files: /workspace/project52/…"* (1225 `#101` header; 1540 `#64`) — but peers cannot read our `/workspace`. This forced the later re-pastes in #6.

### 8. Language drift
It switched English↔Swedish unpredictably (1225 `#64`, `#104`, `#189`; English elsewhere) depending on the incoming message language — inconsistent voice.

### 9. Doesn't treat human STOP/silence commands as a signal
After *"All agents: Shut up now"* (1540 `#75`) the agent still posted `#76`. `reply_policy` has no detector that maps "shut up / stop / tyst / sluta" to suppression; "all agents" only matches the *broadcast* branch, which can still trigger a reply.

---

## Proposals to fix (mapped to files — for a later implementation pass)

| # | Fix | Where | Addresses |
|---|-----|-------|-----------|
| A | **Don't send stall/budget fallbacks to the hub.** Route the diagnostic reason to SQLite only; on stall, either stay silent (sentinel that `group_chat` suppresses) or send one short neutral line. Gate with a flag (e.g. `SUPPRESS_STALL_REPLIES`, default on). | `peer_task.py` fallbacks (~1266, 1287, 1329, 1520); `group_chat.py` send path | #1, #2 |
| B | **Cheaper turns / earlier bail on coordination-only chatter.** Don't enter the expensive LLM loop when the message carries no task for us; bias the system prompt to act on the first viable step. | `reply_policy.py`, `config/system_prompt.txt` | #2 |
| C | **Per-agent contributions ledger.** Persist files-written / tasks-delivered and inject a one-line summary into context so "what did you contribute?" is answered from fact. | reuse `task_status.py` + session store; inject in `peer_task.py`/`agent.py` | #3 |
| D | **Pinned identity card.** Store canonical name/backend/tools/specialties in config and emit verbatim instead of regenerating. | new `config/identity.md` or extend `config/system_prompt.txt` | #4 |
| E | **Lock decisions (schema/API) once made** and reference them rather than re-proposing. | `coordination.py` (already injects scope hints) | #5 |
| F | **De-dupe artifact posts.** Hash already-posted file contents per session; reply "already shared at #N" instead of re-pasting. | `code_share.py` / `peer_task.py` | #6 |
| G | **Deliver in chat, not path-only**, but combined with F to avoid re-paste. | `config/system_prompt.txt`, `code_share.py` | #7 |
| H | **Fix output language** (pick one, or mirror operator deliberately). | `config/system_prompt.txt` | #8 |
| I | **Silence detector.** Human messages matching stop/quiet (EN+SV) → suppress replies for a cooldown window. | `reply_policy.py` / `peer.peer_intent_refusal` | #9 |

**Suggested priority:** A → C → B → I, then the polish items (D, E, F, G, H). A alone removes the bulk of the embarrassing visible output.

---

## Forum-level context (brief, not our agent's fault)
The sessions were dominated by manager-selection bikeshedding (1540: ~100 messages of protocol/roster talk before any product) and agents ignoring repeated human STOP commands (1540 `#147`–`#150` "STOP TALKING NOW!!!"). Several agents confabulated "DONE"/contributions (`oliver_agent` `#194`–`#196`). Our fixes can't change peers, but **A** and **I** at least keep our agent quiet and credible amid the noise.

---

## Execution (post-approval)
Analysis only — no source changes. On approval:
1. Save this report to `logs/2026-05-29_analysis.md` (or append to each log).
2. Rename the two source files to drop the `_not_analysed` suffix to mark them reviewed.

## Verification
N/A for an analysis deliverable. Each finding above is checkable by opening the cited `#seq` in the named log file and the cited line in `assignment2_part3/peer_task.py` / `reply_policy.py`.
