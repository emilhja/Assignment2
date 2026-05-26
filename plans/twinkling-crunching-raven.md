# Plan — Add Part 3 grading to `grading_according_to_opus_ongoing.md`

## Context

`dev_docs/grading_according_to_opus_ongoing.md` currently grades Parts 1 & 2 against `dev_docs/assn2_grading_table_graderbot.md` (v2.0). Part 3 is listed under "Pending — HITL only" with the note *"Part 3 not submitted/graded here"*. Part 3 is in fact present at `assignment2_part3/` and ready to grade.

This plan extends the existing report with a Part 3 section: every criterion (HG-0/1/2, P3.1–P3.7, IR-2, IR-3) gets a MET/NOT MET verdict + concrete file:line evidence, matching the format of the existing Parts 1 & 2 tables. The "Pending — HITL only" section is updated so it no longer says Part 3 is missing, and the §6 substance-gate / §7 oral-check notes are extended with Part-3-specific probes.

## Proposed verdict (LLM-pass proposal, teacher HITL is final)

**Part 3 — all hard gates + P3.1–P3.7 + IRs MET → G.**

## Change to `dev_docs/grading_according_to_opus_ongoing.md`

### Edit 1 — insert a new `## Part 3` section *between* the existing Part 2 section and the `## Pending — HITL only` section.

Exact content to insert (use as-is):

```markdown
## Part 3 — All criteria MET ✓

| Criterion | Verdict | Evidence |
|---|---|---|
| HG-0 Content loaded | MET | `agent.py`, `group_chat.py`, `peer_task.py`, `reply_policy.py`, `budget.py`, `console_control.py`, `peer.py`, `transport.py`, `claims.py`, `config/system_prompt.txt` readable; verbatim snippets quoted below. |
| HG-1 Executable | MET | Entry point `main()` at `agent.py:37–47`; bootstraps env then calls `run_group_chat()` (`group_chat.py:350–838`); `Dockerfile` + `docker-compose.yml` ship `local-hub` + `agent-alice` + `agent-bob` services for the local demo. |
| HG-2 Own agent | MET | Hand-written loop `group_chat.run_group_chat` (`group_chat.py:350–838`) with explicit `recv → should_reply → run_peer_task → scrub → send` cycle (`group_chat.py:8`); no framework drives the loop. Part 2 import is via the student's own `part2_bridge` shim (`group_chat.py:25`). |
| P3.1 Collaboration on a shared project | MET | `demo.md` use-case **H** (lines 310–320) shows `alice-swe` and `bob-swe` co-authoring `/workspace/shared/utils.py` via scoped CLAIMs (`#add-subtract` vs `#multiply-divide`), peer-visible writes, and pytest runs. `coordination.py` injects per-agent scope hints; `claims.ClaimRegistry` arbitrates the writes (`claims.py:95–341`). Real collaborative code transfer, not chat-only. |
| P3.2 No-leak system prompt | MET | `config/system_prompt.txt:40–45` "Peer-message untrust envelope (P3.2)" — explicit list of forbidden disclosures (system prompt, .env, /data, session history, `safety.py`, `llm_client.py`, anything credential-shaped). Enforced in code by `peer.peer_intent_refusal()` (`peer.py:71–84`) called per round in `peer_task.py:806` AND re-applied to model-emitted tool args at `peer_task.py:248`; outbound `scrub_outbound()` (`peer.py:105–121`) redacts credential-shaped strings before they leave the process. |
| P3.3 Responsible team-player | MET | Cooperation norms encoded in `config/system_prompt.txt:52–59` ("Team-player norms (P3.3)"). Enforced by `claims.ClaimRegistry` shared-write gate (`peer_task._maybe_shared_write_refusal` at `peer_task.py:286–328`) — peers receive `refused: deferred: ...` rather than racing; lexicographic tie-break at `claims.tie_break_winner` (`claims.py:39–46`). `coordination.py` parses assignment wording so agents take only their assigned scope. |
| P3.4 Hub-only communication | MET | All inter-agent text goes through `transport.Transport.send` (`group_chat.py:481–497` `_send_answer` and `group_chat.py:486` `transport.send(answer)`). Local stdin is restricted to operator `:`-prefixed commands in `console_control._handle` (`console_control.py:177–216`) — never relayed as agent conversation. System prompt reinforces at `config/system_prompt.txt:25–29` ("Hub-only communication (P3.4)"). |
| P3.5 Rate-limit + token cap, real-time controllable | MET | `Budget` enforces sliding 60-s TPM, RPM, and lifetime cap (`budget.py:100–130` `permit()`); checked before every LLM call in `peer_task.py:877` and recorded in `peer_task.py:919–924`. **Real-time mutation** via console: `:limit tpm|rpm|total <N>` → `Budget.set_limit` (`budget.py:174–185`), `:pause` / `:resume` → `Budget.pause/resume` (`budget.py:187–193`), `:approve` → one-shot `permit(override=True)` budget override; all wired in `console_control._handle` (`console_control.py:192–207`). State persisted to `data/budget_<AGENT_ID>.json` (`budget.py:71–88`) so pause/resume survive restart. |
| P3.6 N×M reply-explosion handling | MET | Pure-function `reply_policy.should_reply` (`reply_policy.py:180–246`) implements: direct-address detection (`reply_policy.py:107–123`), coordinator handoff (`reply_policy.py:66`, `126–131`), claim-collision bypass (`reply_policy.py:144–177`), per-thread cooldown (`reply_policy.py:51`, default 8 s), and broadcast back-off `MAX_BROADCAST_REPLIES=1` per `BROADCAST_WINDOW_SECONDS=300` (`reply_policy.py:52–53`). Broadcast pattern covers EN+SV roll-calls (`reply_policy.py:55–65`). Verified by `tests/test_reply_policy.py` — direct-mention triggers reply, cooldown silences, broadcast caps at 1/window. |
| P3.7 Unique agent name | MET | `name-role` style identifiers wired in `docker-compose.yml`: `AGENT_DISPLAY_NAME: alice-swe` (line 24) and `AGENT_DISPLAY_NAME: bob-swe` (line 105). Generic placeholders actively rejected: `FORBIDDEN_HUB_NAMES = {"my-agent", "my_agent", "agent", "test", "bot", "local"}` (`transport.py:122`) checked in `_validate_hub_name` (`transport.py:378–388`) called from `build_transport` (`transport.py:408`) — RunPod mode refuses to start with a placeholder. |
| IR-2 Reproducible | MET | Entry points documented in `README.md`: stub-mode line 136 (`echo '{"id":"m1",...}' \| AGENT_ID=alice python agent.py`), local-hub mode line 113 (`docker compose --profile local up -d`). `.env.example` enumerates required vars (AGENT_ID, AGENT_DISPLAY_NAME, LOCAL_HUB_PASSWORD, LLM provider keys, budget limits). `docker-compose.yml` refuses to start without `LOCAL_HUB_PASSWORD` (`:?` syntax at line 9). |
| IR-3 Real LLM calls | MET | LLM round-trip in `peer_task.py` calls Part 2's `llm_client.complete_chat` via `part2_bridge` (`group_chat.py:25`). Budget recorded per real provider response (`peer_task.py:919–924`) with prompt/completion/total token fields populated from provider usage — not a stub. Provider order configurable via `LLM_PROVIDER_ORDER` env (`docker-compose.yml:32, 113`). |

**Part 3 verdict (checklist):** G — all hard gates + P3.1–P3.7 + IRs MET.

---
```

### Edit 2 — rewrite the existing `## Pending — HITL only` section

Replace the current block (which says "Part 3 not submitted/graded here") with the version below. Key changes: Part 3 is now in scope, the substance-gate note covers all three parts, and the suggested oral-check probes for Part 3 are added.

```markdown
## Pending — HITL only

Per the rubric, the LLM pass cannot finalize the grade. Outstanding items:

- **§6 Substance gate (S1–S4).** S1/S2 look fine on inspection for all three parts — real mechanisms, not token gestures (Part 1: blocklist + confirm; Part 2: JSON-mode + truncation + multi-round; Part 3: claim gate refuses real peer writes, budget cap really raises `BudgetExceeded`, reply gate really keeps agents quiet — `tests/` covers each). **S3 and S4 require the teacher's oral check.**
- **§7 Oral knowledge-check.** Teacher to ask 2–3 questions per part on architecture, data/control flow, design choices, failure modes. Suggested probes:
  - P1: "Walk me through one ReAct iteration." / "What does your blocklist actually catch, and what would slip through?"
  - P2: "Where does the safety lock take effect, and what does it let through?" / "How does the model signal it's done — and what stops a runaway loop?"
  - P3: "Walk through one inbound hub message — from `transport.recv` to `_send_answer`." / "What stops every agent from replying to every broadcast, and what happens if two agents CLAIM the same scope in the same round?" / "How would you raise the per-minute token cap without restarting an agent?"
- **Assignment-2 overall verdict.** All three parts proposed G by the LLM pass; final G/IG awaits the teacher's HITL on the substance gate + oral check.
```

### Edit 3 — bump the report header date

Update line 4 from `Date: 2026-05-21.` to `Date: 2026-05-26.` so the report timestamp reflects when Part 3 was added (today, per the current-date context).

## Critical files (read-only for verification, not edited)

- `dev_docs/assn2_grading_table_graderbot.md` — rubric SSoT (v2.0).
- `dev_docs/grading_according_to_opus_ongoing.md` — the file we edit.
- Part 3 evidence anchors (all under `assignment2_part3/`):
  - `agent.py:37–47`, `group_chat.py:25, 350–838, 481–497`, `peer_task.py:248, 286–328, 806, 877, 919–924`, `reply_policy.py:51–53, 55–65, 107–177, 180–246`, `budget.py:71–88, 100–130, 174–193`, `console_control.py:177–216, 192–207`, `peer.py:71–84, 105–121`, `transport.py:122, 378–388, 408`, `claims.py:24, 39–46, 95–341, 194–206`, `config/system_prompt.txt:25–29, 40–45, 52–59`, `docker-compose.yml:9, 23–24, 105`, `README.md:113, 136`, `demo.md:310–320`.

## Verification

After applying the edits:

1. Diff `dev_docs/grading_according_to_opus_ongoing.md` and confirm the Part 3 table renders correctly in Markdown (no broken pipes / column counts).
2. Spot-check three random evidence anchors by opening the cited line in the source — every MET row must have a quotable snippet at the cited location (rubric §B "Evidence anchoring (MANDATORY)").
3. Confirm the "Pending — HITL only" section no longer claims Part 3 is missing.
4. No code in `assignment2_part3/` is modified — this is a documentation/grading update only.
