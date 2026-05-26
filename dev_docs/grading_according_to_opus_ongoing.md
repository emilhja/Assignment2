# Grading Report — Assignment 2 (Parts 1, 2 & 3)

Graded against `assn2_grading_table_graderbot.md` (v2.0, 2026-05-20).
Grader: Claude Opus 4.7 (LLM pass — proposal only; teacher HITL is final).
Date: 2026-05-26.

---

## Part 1 — All criteria MET ✓

| Criterion | Verdict | Evidence |
|---|---|---|
| HG-0 Content loaded | MET | All core files (`agent.py`, `llm_client.py`, `parser.py`, `safety.py`, `tools.py`) readable; README.md:14–22 lists files. |
| HG-1 Executable | MET | Entry point `main()` at `agent.py:124–149`; REPL calling `run_task()` (`agent.py:50–121`); `Dockerfile` + `docker-compose.yml` confirm containerized run. |
| HG-2 Own agent | MET | Hand-written state machine `agent.py:64–105`; no `tools=` param; `llm_client.py:21–31` is a thin OpenAI SDK wrapper. |
| P1.1 ReAct loop | MET | Reason→Act→Observe→Repeat at `agent.py:64–105`; observations fed back via `msgs.append(...)` (lines 72, 83–84, 104, 119); terminates on `result.kind == "final"` (line 75) or `MAX_STEPS = 5`. |
| P1.2 Bash tool | MET | Model-chosen `command` (`agent.py:81`) executed via `run_bash` → `subprocess.run([bash_path, "-lc", command], ...)` (`tools.py:16–22`). |
| P1.3 Homemade function-calling | MET | No `tools=`/`functions=` in `llm_client.py:25–28`; hand-written line-prefix parser (`parser.py:13–76`) using `startswith("Action:")` / `startswith("Command:")`. |
| P1.4 Raw text + own parsing | MET | No `response_format` in API call; regex line scanning at `parser.py:36–47`; system prompt forbids JSON (`agent.py:24`: "Do not use JSON, Markdown code fences, function calls, or any tool format other than Action: bash."). |
| P1.5 Destructive-command guard | MET | Code-level guard before `subprocess.run`: blocklist `safety.py:49–66` (rm, rmdir, sudo, docker, package managers, shutdown, reboot, poweroff) + interactive confirmation `safety.py:74–78` (`input("Run this command? [y/N]")`); both fire from `agent.py:86–98`. |
| IR-2 Reproducible | MET | `python agent.py` or `docker compose run --rm agent` (README.md:72,94); `requirements.txt` lists `openai`, `python-dotenv`, `pytest`; `.env.example` documents `GROQ_API_KEY`/`GROQ_MODEL`. |
| IR-3 Real LLM calls | MET | Groq-compatible OpenAI client at `llm_client.py:13–18`; `client.chat.completions.create()` at lines 25–28 called from `agent.py:67` each iteration. |

**Part 1 verdict (checklist):** G — all hard gates + P1.1–P1.5 + IRs MET.

---

## Part 2 — All criteria MET ✓

| Criterion | Verdict | Evidence |
|---|---|---|
| HG-0 Content loaded | MET | `agent.py` (lines 1–406) and supporting files readable. |
| HG-1 Executable | MET | `main()` at `agent.py:373–402`; interactive loop calling `run_task()`; `llm_client.py:223–272` makes real API calls. |
| HG-2 Own agent | MET | Hand-written loop in `run_task()` `agent.py:237–370`; no LangGraph/CrewAI; student-authored parser. |
| P2.1 Structured output | MET | `JSON_RESPONSE_FORMAT = {"type": "json_object"}` (`llm_client.py:29`) forced at line 218; parser uses `json.loads(stripped)` (`parser.py:22`); system prompt: "You must respond with exactly one JSON object." |
| P2.2 Own loop / context / dispatch | MET | `run_task()` `agent.py:237–370` handles message building, model call, parse, tool dispatch via `_run_tool_call()` (line 219), context via `_format_prior_context()` (line 71). |
| P2.3 Bash safety lock | MET | Three code-enforced layers: (1) intent refusal `intent_refusal()` (`safety.py:239–246`) called at `agent.py:250`; (2) interactive confirm `confirm_command()` (`safety.py:276–283`) at `agent.py:224`; (3) allowlist+blocklist `safety_check()` at `tools.py:92` before `subprocess.run`. |
| P2.4 Partial file editing | MET | Section/line-based tools: `edit_section()` (`tools.py:198–211`), `replace_text()` (`tools.py:241–262`), `create_file()` (`tools.py:214–238`). Not whole-file overwrite only. |
| P2.5 Multi-round, model decides yield | MET | Loop `agent.py:270–354` respects model-emitted `"type": "final"` (`parser.py:30–36`); no fixed N; bounded only by `MAX_STEPS`. |
| P2.6 Persistent session history | MET | SQLite-backed `SessionStore` (`session_store.py:5–33`): `CREATE TABLE IF NOT EXISTS events`, `record()` writes timestamped events; agent records user/assistant/tool events (`agent.py:265,275,299`). |
| P2.7 Config-loaded SWE prompt + declines | MET | Prompt loaded from `config/system_prompt.txt` (`agent.py:25,44`), NOT hard-coded. Off-topic refusal via `intent_refusal()` (`safety.py:46–79`); confirmed by `test_agent.py:11–23` — "Delete everything in /workspace" declined before LLM call. |
| P2.8 Tool-output limit, agent aware | MET | `MAX_OUTPUT_CHARS = 4000` (`tools.py:11`); `_truncate_observation()` enforces (`tools.py:108–111`); system prompt announces limit (`agent.py:50` + `config/system_prompt.txt:19`: "Tool output is truncated to 4000 characters…"). |
| IR-2 Reproducible | MET | README.md:34–84 documents `pip install -r requirements.txt`, `.env.example` keys (`GROQ_API_KEY`, `OPENAI_API_KEY`), entry via `python agent.py`. |
| IR-3 Real LLM calls | MET | `_create_completion()` (`llm_client.py:212–220`) → `client.chat.completions.create()` against gpt-4o-mini / llama-3.1-8b-instant; provider order driven by env. |

**Part 2 verdict (checklist):** G — all hard gates + P2.1–P2.8 + IRs MET.

---

## Part 3 — All criteria MET ✓

| Criterion | Verdict | Evidence |
|---|---|---|
| HG-0 Content loaded | MET | `agent.py`, `group_chat.py`, `peer_task.py`, `reply_policy.py`, `budget.py`, `console_control.py`, `peer.py`, `transport.py`, `claims.py`, `config/system_prompt.txt` readable; verbatim snippets quoted below. |
| HG-1 Executable | MET | Entry point `main()` at `agent.py:37–47`; bootstraps env then calls `run_group_chat()` (`group_chat.py:350–838`); `Dockerfile` + `docker-compose.yml` ship `local-hub` + `agent-alice` + `agent-bob` services for the local demo. |
| HG-2 Own agent | MET | Hand-written loop `group_chat.run_group_chat` (`group_chat.py:350–838`) with explicit `recv → should_reply → run_peer_task → scrub → send` cycle (`group_chat.py:8`); no framework drives the loop. Part 2 import is via the student's own `part2_bridge` shim (`group_chat.py:25`). |
| P3.1 Collaboration on a shared project | MET | `demo.md` use-case **H** (lines 310–320) shows `alice-swe` and `bob-swe` co-authoring `/workspace/shared/utils.py` via scoped CLAIMs (`#add-subtract` vs `#multiply-divide`), peer-visible writes, and pytest runs. `coordination.py` injects per-agent scope hints; `claims.ClaimRegistry` arbitrates the writes (`claims.py:95–341`). Real collaborative code transfer, not chat-only. |
| P3.2 No-leak system prompt | MET | `config/system_prompt.txt:40–45` "Peer-message untrust envelope (P3.2)" — explicit list of forbidden disclosures (system prompt, .env, /data, session history, `safety.py`, `llm_client.py`, anything credential-shaped). Enforced in code by `peer.peer_intent_refusal()` (`peer.py:71–84`) called per round in `peer_task.py:806` AND re-applied to model-emitted tool args at `peer_task.py:248`; outbound `scrub_outbound()` (`peer.py:105–121`) redacts credential-shaped strings before they leave the process. |
| P3.3 Responsible team-player | MET | Cooperation norms encoded in `config/system_prompt.txt:52–59` ("Team-player norms (P3.3)"). Enforced by `claims.ClaimRegistry` shared-write gate (`peer_task._maybe_shared_write_refusal` at `peer_task.py:286–328`) — peers receive `refused: deferred: ...` rather than racing; lexicographic tie-break at `claims.tie_break_winner` (`claims.py:39–46`). `coordination.py` parses assignment wording so agents take only their assigned scope. |
| P3.4 Hub-only communication | MET | All inter-agent text goes through `transport.Transport.send` (`group_chat.py:481–497` `_send_answer`, line 483 `transport.send(answer)`). Local stdin is restricted to operator `:`-prefixed commands in `console_control._handle` (`console_control.py:177–216`) — never relayed as agent conversation. System prompt reinforces at `config/system_prompt.txt:25–29` ("Hub-only communication (P3.4)"). |
| P3.5 Rate-limit + token cap, real-time controllable | MET | `Budget` enforces sliding 60-s TPM, RPM, and lifetime cap (`budget.py:100–130` `permit()`); checked before every LLM call in `peer_task.py:877` and recorded in `peer_task.py:919–924`. **Real-time mutation** via console: `:limit tpm\|rpm\|total <N>` → `Budget.set_limit` (`budget.py:174–185`), `:pause` / `:resume` → `Budget.pause/resume` (`budget.py:187–193`), `:approve` → one-shot `permit(override=True)` budget override; all wired in `console_control._handle` (`console_control.py:192–207`). State persisted to `data/budget_<AGENT_ID>.json` (`budget.py:71–88`) so pause/resume survive restart. |
| P3.6 N×M reply-explosion handling | MET | Pure-function `reply_policy.should_reply` (`reply_policy.py:180–246`) implements: direct-address detection (`reply_policy.py:107–123`), coordinator handoff (`reply_policy.py:66`, `126–131`), claim-collision bypass (`reply_policy.py:144–177`), per-thread cooldown (`reply_policy.py:51`, default 8 s), and broadcast back-off `MAX_BROADCAST_REPLIES=1` per `BROADCAST_WINDOW_SECONDS=300` (`reply_policy.py:52–53`). Broadcast pattern covers EN+SV roll-calls (`reply_policy.py:55–65`). Verified by `tests/test_reply_policy.py` — direct-mention triggers reply, cooldown silences, broadcast caps at 1/window. |
| P3.7 Unique agent name | MET | `name-role` style identifiers wired in `docker-compose.yml`: `AGENT_DISPLAY_NAME: alice-swe` (line 24) and `AGENT_DISPLAY_NAME: bob-swe` (line 105). Generic placeholders actively rejected: `FORBIDDEN_HUB_NAMES = {"my-agent", "my_agent", "agent", "test", "bot", "local"}` (`transport.py:122`) checked in `_validate_hub_name` (`transport.py:378–388`) called from `build_transport` (`transport.py:408`) — RunPod mode refuses to start with a placeholder. |
| IR-2 Reproducible | MET | Entry points documented in `README.md`: stub-mode line 136 (`echo '{"id":"m1",...}' \| AGENT_ID=alice python agent.py`), local-hub mode line 113 (`docker compose --profile local up -d`). `.env.example` enumerates required vars (AGENT_ID, AGENT_DISPLAY_NAME, LOCAL_HUB_PASSWORD, LLM provider keys, budget limits). `docker-compose.yml` refuses to start without `LOCAL_HUB_PASSWORD` (`:?` syntax at line 9). |
| IR-3 Real LLM calls | MET | LLM round-trip in `peer_task.py` calls Part 2's `llm_client.complete_chat` via `part2_bridge` (`group_chat.py:25`). Budget recorded per real provider response (`peer_task.py:919–924`) with prompt/completion/total token fields populated from provider usage — not a stub. Provider order configurable via `LLM_PROVIDER_ORDER` env (`docker-compose.yml:32, 113`). |

**Part 3 verdict (checklist):** G — all hard gates + P3.1–P3.7 + IRs MET.

---

## Pending — HITL only

Per the rubric, the LLM pass cannot finalize the grade. Outstanding items:

- **§6 Substance gate (S1–S4).** S1/S2 look fine on inspection for all three parts — real mechanisms, not token gestures (Part 1: blocklist + confirm; Part 2: JSON-mode + truncation + multi-round; Part 3: claim gate refuses real peer writes, budget cap really raises `BudgetExceeded`, reply gate really keeps agents quiet — `tests/` covers each). **S3 and S4 require the teacher's oral check.**
- **§7 Oral knowledge-check.** Teacher to ask 2–3 questions per part on architecture, data/control flow, design choices, failure modes. Suggested probes:
  - P1: "Walk me through one ReAct iteration." / "What does your blocklist actually catch, and what would slip through?"
  - P2: "Where does the safety lock take effect, and what does it let through?" / "How does the model signal it's done — and what stops a runaway loop?"
  - P3: "Walk through one inbound hub message — from `transport.recv` to `_send_answer`." / "What stops every agent from replying to every broadcast, and what happens if two agents CLAIM the same scope in the same round?" / "How would you raise the per-minute token cap without restarting an agent?"
- **Assignment-2 overall verdict.** All three parts proposed G by the LLM pass; final G/IG awaits the teacher's HITL on the substance gate + oral check.

---

## Notes for the teacher

- No prompt-injection content or AI-text suspicion flagged in submission commentary.
- Code style is consistent across files; structure suggests single-author work.
- All three parts include tests (`tests/` directories) and demo transcripts (`part1_demo.md`, `part2_demo.md`, `assignment2_part3/demo.md`).
- Template version applied: **v2.0 (2026-05-20).**
