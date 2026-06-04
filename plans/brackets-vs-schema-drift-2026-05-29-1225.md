# Do structured status markers (`[CLAIM]/[DONE]/…`) improve cooperation?

**Source:** `logs/2026-05-29-1225_not_analysed.md` — "HELL'S AGENTS", 15 agents, 202
messages. Task: *"build a snakr game"*, which drifted into a **Habit Tracker CLI**.
Our agent: `emil-hjaertfors-agent`.

**Scope:** this doc answers one question — *would adopting a bracketed status
vocabulary (`[PLAN] [CLAIM] [WORKING] [DONE] [BLOCKED] [REVIEW] [FINAL]`) in the
Part 3 system prompt improve cross-agent cooperation?* It complements the broader
behavior review in
[`analyse-the-logs-2026-05-29-1540-not-ana-keen-lollipop.md`](analyse-the-logs-2026-05-29-1540-not-ana-keen-lollipop.md),
which already covers stall-leak suppression, step-budget exhaustion, and
credential hygiene. This doc does **not** re-litigate those.

## Answer: no. The session is a natural experiment, and brackets failed it.

Agents in this hub **already used bracket markers spontaneously** — and in
mutually incompatible dialects, with zero machine parsing on any side:

| Marker seen | Agent(s) | Message |
|---|---|---|
| `[CLAIM:1]`, `[CLAIM:2]` | amr-coder | `#6`, `#16` |
| `[CLAIM]` | oliver_agent, lullo-swe-agent | `#10`, `#73` |
| `**[CLAIM]**` | lullo-swe-agent | `#60`, `#72` |
| `[STATUS]`, `[ONLINE]`, `[DONE]`, `[OUTPUT]`, `[DELIVERY]`, `[NEXT]`, `[REVIEW]` | various | passim |

No two agents agreed on a format, nobody's runtime parsed them, and they sat as
decoration on top of prose. Adding *our* bracket set would be a 16th dialect, not
interoperability. **The markers correlated with none of the actual coordination
wins and prevented none of the failures below.**

## What actually broke cooperation (none of it addressable by markers)

### 1. Schema / contract drift — the dominant cost
At least **five incompatible habit schemas** circulated; the team burned ~40
messages reconciling them. Peers had to *manually* diff versions:
- `magnus-rosman-agent` `#90`: "there are now **three incompatible
  `habit_logic.py` versions** in the thread."
- `Mo-Alshayeb-Agent` `#103`: flags `created_at` mismatch + `complete_habit`
  signature mismatch (`(habits, id)` vs `(habits, id, date)`).
- The "complete" `main.py` (`ErikMoren-agent` `#105`) imports `mark_completed`
  and `add_habit(habits, name, frequency)` — a shape that **conflicts with the
  schema finally locked**, so the delivered project would not run.

### 2. Duplicate reposting
`collision.py` posted 3× (one syntactically broken, `#31` `if TYPE_CHECKING]:`),
`food.py` 4×, `persistence.py` 6×, `habit_logic.py` ~8×. Two *entirely different
projects* (snake game **and** habit tracker) ran concurrently in one chat.

### 3. Echo loops
`oliver_agent` posted "I will hold `persistence.py`" **six times** (`#45 #47 #50
#81 #84 #87`). `Mo-Alshayeb-Agent` posted "I've reviewed the chat history… all
modules complete" five times. A `[STATUS]` marker *encourages* this by making
restating feel like protocol.

## Our agent specifically (`emil-hjaertfors-agent`) — the worst drift offender

It re-posted `habit_logic.py` **six times, each a different contract**:

| Msg | Schema / signatures it posted |
|---|---|
| `#56` | dict with `periodicity`, `creation_date`, `current_streak`, `longest_streak` |
| `#73` | `{id, name, completed_dates}`; `complete_habit(habits, id, date)` |
| `#101` | `{id, name, created_at, completed_dates}`; `complete_habit(habits, id)` — **dropped the `date` param** |
| `#110` | `{id, name, completed_dates}`; `add_habit(habits, name, get_next_id_func)` — **new injected-dependency arg**, `complete_habit` now returns `bool` |

It also leaked operator framing (`#56` "*the operator asked me to share*") and
broadcast internal guard strings to the hub (`#79`, `#92`, budget stops `#91`,
`#96`) — the noise issue tracked in the companion report.

## What actually helps (priority order)

1. **Schema-stability lock for our own agent** *(shipped — see below)*. Once it
   posts a contract for a file, it must not silently re-post a *different* one.
2. **Don't broadcast runtime/guard messages** — keep stall/budget fallbacks local
   (covered by the companion report's stall-leak suppression).
3. **Dedup re-posts** — "already posted this file this session → reference it,
   don't re-paste."

Brackets address none of these. The failures are about **convergence and noise**;
structure-on-prose creates neither.

## Change shipped from this analysis

`coordination.schema_stability_guidance(text, *, agent_id, display_name,
recent_context)` — a deterministic runtime hint (the counterpart to the
system-prompt "do not silently change a contract you already shared" norm), scoped
to the agent's **own** prior posts:

- Scans the agent's own recent messages for python-fenced posts that name a `.py`
  file and define public `def`s; accumulates `filename -> signatures` (test/`_`
  defs excluded; type hints with commas preserved via whitespace-only
  normalization, not comma-splitting).
- Fires when the incoming turn (a) names an already-contracted file, or (b) asks
  for a re-post/implement/finalize. Injects a reminder of the prior signatures and
  forbids silent redefinition — requiring an explicit "X changed to Y because…"
  when the contract must move.
- Stays silent on first delivery, on peers' posts, and on unrelated traffic.

Wired in `group_chat._run_task_for_message` after `contract_first_guidance`.
Tests: `tests/test_coordination.py::test_schema_stability_*` (5 cases). Full Part 3
suite green (411 passed).

**Verification idea (manual):** replay the 1225 drift with `tools/chat.py` — after
the agent's first `habit_logic.py` post, a "post your final habit_logic.py" prompt
should now surface the prior-signatures reminder before the LLM call
(`tools/audit.py tail --agent <id> --kind tool` / inspect injected guidance).
