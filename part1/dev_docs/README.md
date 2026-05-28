# Part 1 Developer Docs

This folder explains the Part 1 ReAct bash agent for grading and review. The
main goal is to make the agent mechanics easy to follow: a user asks a question,
the program sends chat messages to an LLM, the LLM replies in a small raw-text
protocol, local code parses that protocol, approved bash commands run, command
output is sent back as an observation, and the loop continues until a final
answer is produced.

In this repository the Part 1 folder is named `part1`.

## Documents

- [architecture_blueprint.md](architecture_blueprint.md) describes the module
  responsibilities, design choices, safety gates, and rubric evidence.
- [message_flow_overview.md](message_flow_overview.md) traces one user question
  through the files and back to the final answer.

## Core Files

| File | Role |
|---|---|
| `part1/agent.py` | CLI entry point, system prompt, and ReAct control loop. |
| `part1/llm_client.py` | OpenAI-compatible chat client for Groq or a local server. |
| `part1/protocol.py` | Homemade raw-text parser for `Action: bash` and `Final Answer:`. |
| `part1/safety.py` | User-intent refusal, command blocklist, and confirmation prompt. |
| `part1/bash_tool.py` | Bash command runner with timeout, output capture, and truncation. |
| `part1/tests/` | Focused tests for parsing, safety, tool execution, and loop behavior. |

## Quick Run Commands

From the repository root:

```powershell
python -m pytest part1 -q
```

From the Part 1 directory:

```powershell
cd part1
python -m pip install -r requirements.txt
python agent.py
```

To see the internal ReAct trace:

```powershell
$env:AGENT_DEBUG = "1"
python agent.py
```

## What This Proves

Part 1 is intentionally small and manual. It does not use LangChain,
LangGraph, provider tool calling, function calling, JSON mode, or structured
outputs. The point is to show the basic agent loop directly in Python:

1. Call an LLM with normal chat messages.
2. Parse the LLM's raw text response with local code.
3. Execute only the supported `bash` action after safety checks.
4. Feed command output back to the model as an `Observation:`.
5. Stop when the model returns `Final Answer:`.
