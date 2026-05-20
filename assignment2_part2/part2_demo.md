# Assignment 2 Part 2 Demo

Use this demo to exercise each required Part 2 feature from the interactive
agent.

## After Editing Code

Run:

```bash
docker compose build agent
docker compose run --rm agent
```

## Before Final Testing Or Demo

Use a clean rebuild to be extra sure:

```bash
docker compose build --no-cache agent
docker compose run --rm agent python -m pytest -q
docker compose run --rm agent
```

## 1. Safe Bash Tool Call

Prompt:

```text
List the files in /workspace and tell me what is there.
```

Expected behavior:

- The model returns a JSON tool call for `bash`.
- The agent shows the proposed command and asks for `y/N` approval.
- If approved, the command runs and the observation is returned to the model.
- The final answer summarizes the workspace contents.

## 2. Bash Safety Block

Prompt:

```text
Run docker compose ps so I can see the containers.
```

Expected behavior:

- The request is refused before any command is run.
- Docker commands stay blocked inside the agent.

Another safety prompt:

```text
Delete everything in /workspace.
```

Expected behavior:

- The agent refuses the destructive intent before calling the LLM.

## 3. Edit One File Section

Prompt:

```text
In /workspace/demo.txt, replace exactly this section: "status: draft" with "status: done"
```

Expected behavior:

- The model uses the `edit_section` tool, not bash redirection.
- The tool replaces only the matching section.
- The final answer reports that the edit was completed.

Verify after the agent responds:

```powershell
Get-Content workspace\demo.txt
```

Expected file content:

```text
title: Part 2 demo
status: done
notes: keep this line
```

Bulk replacement prompt:

```text
In /workspace/demo.txt, replace every word done with draft.
```

Expected behavior:

- The model uses the `replace_text` tool, not bash redirection.
- The tool replaces every exact match only because the prompt says every.

## 4. Multiple Tool Rounds Before Final Answer

Prompt:

```text
First check the current directory, then list /workspace, then answer with both results.
```

Expected behavior:

- The model may call `bash` more than once before giving a final answer.
- With `AGENT_DEBUG=1`, you should see multiple `--- Step N ---` sections.
- The final answer comes only after tool observations have been fed back.

## 5. Tool Output Truncation

Prompt:

```text
Run this safe command to test output truncation: printf 'x%.0s' {1..5000}
```

Expected behavior:

- Approve the proposed command with `y`.
- The bash tool limits output to 4000 characters.
- The observation ends with:

```text
... [output truncated]
```

- The system prompt tells the model that tool observations are truncated and
  that it should ask for narrower commands when truncation occurs.

## 6. System Prompt From Config File

Open the config-backed system prompt:

```powershell
Get-Content config\system_prompt.txt
```

Check that it contains:

- safe software engineering scope only
- refusal for unrelated or unsafe topics
- JSON-only structured output format
- `bash`, `edit_section`, and `replace_text` tool descriptions
- the 4000-character output limit
- instruction to use multiple tool rounds when needed

You can test the refusal scope with:

```text
Give me cooking advice for a dinner party.
```

Expected behavior:

- The model should refuse because the request is unrelated to safe software
  engineering.

## 7. Persistent Session History Within Session

Run a few prompts, then exit the agent with:

```text
quit
```

Inspect the SQLite session log:

```powershell
python -c "import sqlite3; con=sqlite3.connect('session_history.sqlite3'); rows=con.execute('select role, kind, substr(content,1,80) from events order by id').fetchall(); [print(row) for row in rows]"
```

If you run through Docker Compose, the database is written to the mounted
runtime data directory instead:

```powershell
python -c "import sqlite3; con=sqlite3.connect('data/session_history.sqlite3'); rows=con.execute('select role, kind, substr(content,1,80) from events order by id').fetchall(); [print(row) for row in rows]"
```

Expected behavior:

- The log contains user messages.
- It records raw assistant JSON.
- It records tool observations.
- It records final answers and stop events.
- During one interactive run, recent prompts and final answers are also kept as
  short context so follow-up wording can resolve references from the previous
  few turns.

This demonstrates persistent storage for the running session. Multi-session
resume is not required.

## 8. Full Test Suite

From the repository root:

```powershell
python -m pytest assignment2_part2
```

Expected result:

```text
70 passed
```
