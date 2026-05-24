# Assignment 2 – Part 1  
# Minimal ReAct Agent with Homemade Bash Tool-Calling

## Overview

In Assignment 2 Part 1, the goal is to build a minimal ReAct-style AI agent in Python.

The agent should be able to:

1. Receive a user task.
2. Send the task to a language model.
3. Receive raw text output from the model.
4. Detect whether the model wants to:
   - answer directly, or
   - run one local bash command.
5. Run bash commands through our own Python function.
6. Return command output as an observation to the model.
7. Continue until the model gives a final answer.

The important educational purpose is to understand the core mechanics of an agent loop before using higher-level frameworks or built-in tool-calling systems.

This part must be implemented from scratch using Python code and raw text parsing.

---

## Assignment Constraints

Part 1 must not use:

- LangChain
- LangGraph
- LlamaIndex
- AutoGen
- CrewAI
- OpenCode
- KiloCode
- Claude Code
- Cursor
- Codex as runtime
- built-in function calling
- built-in tool calling
- structured outputs
- JSON mode
- schema-based tool calls

The model output should be plain raw text.

The Python code must do its own parsing and decide whether the model requested a bash command or returned a final answer.

This matches Gabriel’s instruction that Part 1 should use raw text-output and custom string handling, rather than structured outputs or built-in function-calling. :contentReference[oaicite:0]{index=0}

---

## Core ReAct Format

The model must respond using exactly one of two formats.

### Format 1: Request a bash command

```text
Thought: <brief reason>
Action: bash
Command: <one local bash command>