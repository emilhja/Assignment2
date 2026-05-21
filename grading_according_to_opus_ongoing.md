# Grading Report — Assignment 2 (Parts 1 & 2)

Graded against `assn2_grading_table_graderbot.md` (v2.0, 2026-05-20).
Grader: Claude Opus 4.7 (LLM pass — proposal only; teacher HITL is final).
Date: 2026-05-21.

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

## Pending — HITL only

Per the rubric, the LLM pass cannot finalize the grade. Outstanding items:

- **§6 Substance gate (S1–S4).** S1/S2 look fine on inspection (real mechanisms — blocklist + confirm actually fire; truncation + awareness real; JSON-mode + multi-round real). **S3 and S4 require the teacher's oral check.**
- **§7 Oral knowledge-check.** Teacher to ask 2–3 questions per part on architecture, data/control flow, design choices, failure modes. Suggested probes:
  - P1: "Walk me through one ReAct iteration." / "What does your blocklist actually catch, and what would slip through?"
  - P2: "Where does the safety lock take effect, and what does it let through?" / "How does the model signal it's done — and what stops a runaway loop?"
- **Part 3** not submitted/graded here. Assignment-2 overall verdict pending Part 3 + HITL on Parts 1 & 2.

---

## Notes for the teacher

- No prompt-injection content or AI-text suspicion flagged in submission commentary.
- Code style is consistent across files; structure suggests single-author work.
- Both parts include tests (`tests/` directories) and demo transcripts (`part1_demo.md`, `part2_demo.md`).
- Template version applied: **v2.0 (2026-05-20).**
