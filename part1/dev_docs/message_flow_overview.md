# Part 1 Message Flow Overview

This document follows one chat question through the Part 1 files. The key idea
is that the LLM does not directly run tools. The LLM only writes raw text. Local
Python code parses that text, checks safety, runs bash if approved, and sends
the output back to the LLM.

## High-Level Flow

```text
User
  |
  v
agent.py: main() reads the task
  |
  v
agent.py: run_task() creates chat messages
  |
  v
llm_client.py: complete_chat() calls the configured LLM
  |
  v
LLM returns raw text:
  Thought: ...
  Action: bash
  Command: ...
  |
  v
protocol.py: parse_model_response()
  |
  v
safety.py: check_command() and confirm_command()
  |
  v
bash_tool.py: run_bash()
  |
  v
agent.py: append "Observation: <command output>"
  |
  v
llm_client.py: send updated messages back to the LLM
  |
  v
LLM returns:
  Thought: ...
  Final Answer: ...
  |
  v
agent.py: print final answer
```

## Step-by-Step Example

Example user task:

```text
Tell me what files are in the current workspace.
```

### 1. CLI Reads The Question

`agent.py` starts in `main()`. It prints a small prompt, reads the user's input,
and passes the text into:

```python
run_task(user_task)
```

### 2. Broad User Intent Is Checked

At the top of `run_task`, the user task is checked by:

```python
refuse_user_intent(user_task)
```

If the user asked for something broad and destructive, such as deleting
everything, the agent refuses before calling the LLM. For normal questions, the
agent continues.

### 3. Agent Builds The First Chat Messages

`agent.py` creates a message list:

```python
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": user_task},
]
```

The system prompt tells the model to use only the Part 1 raw text protocol.

### 4. LLM Client Sends The Messages

`agent.py` calls:

```python
raw_response = complete_chat(messages)
```

This goes into `llm_client.py`. That file chooses the configured provider, builds
an OpenAI-compatible client, sends the chat messages, and returns only the raw
assistant text.

No provider tool calling happens here. The LLM receives text and returns text.

### 5. Model Requests A Bash Action

For a workspace listing question, the model might reply:

```text
Thought: I need to inspect the current directory.
Action: bash
Command: ls
```

`agent.py` stores this raw assistant response in the conversation history.

### 6. Protocol Parser Reads The Raw Text

`agent.py` calls:

```python
parsed = parse_model_response(raw_response)
```

This goes into `protocol.py`. The parser looks for the exact marker lines:

- `Thought:`
- `Action:`
- `Command:`
- `Final Answer:`

For the example above, it returns a `ParsedResponse` with:

```text
kind = "action"
action = "bash"
command = "ls"
```

If the model gives bad formatting, `agent.py` sends protocol guidance back to
the model and asks it to retry.

### 7. Command Safety Is Checked

For parsed bash actions, `agent.py` extracts the command and calls:

```python
allowed, reason = check_command(command)
```

This goes into `safety.py`. The command is rejected if it starts with a blocked
command family such as `rm`, `sudo`, `docker`, a package manager, `shutdown`, or
`reboot`. The check also looks across simple shell separators so commands such
as `pwd && rm -rf .` are blocked.

If the command is blocked, the agent prints a final refusal and never asks for
confirmation or executes bash.

### 8. User Confirms The Command

If the command passes the safety check, `agent.py` calls:

```python
confirm_command(command)
```

The user sees:

```text
Proposed command:
ls
Run this command? [y/N]
```

If the user denies the command, the command is not executed. Instead, the agent
sends an observation back to the model saying the user denied it.

### 9. Bash Tool Runs The Command

If the user confirms, `agent.py` calls:

```python
observation = run_bash(command)
```

This goes into `bash_tool.py`, which runs bash with:

```python
[bash_path, "-lc", command]
```

The tool captures stdout and stderr and returns text. For example:

```text
README.md
agent.py
bash_tool.py
protocol.py
safety.py
```

### 10. Observation Goes Back To The LLM

`agent.py` appends the tool result as a new user message:

```python
messages.append({"role": "user", "content": f"Observation: {observation}"})
```

This is the "Observe" part of ReAct. The model can now answer based on real
command output instead of guessing.

### 11. LLM Produces The Final Answer

On the next loop step, `agent.py` sends the updated messages back through
`llm_client.complete_chat`. The model now sees the original task, its command,
and the observation. It can reply:

```text
Thought: The observation lists the files.
Final Answer: The current workspace contains README.md, agent.py, bash_tool.py,
protocol.py, and safety.py.
```

`protocol.py` parses this as `kind = "final"`. Then `agent.py` prints:

```text
Final answer:
The current workspace contains README.md, agent.py, bash_tool.py, protocol.py,
and safety.py.
```

## Branches In The Flow

The parser result controls what happens next:

| Parser result | Agent behavior |
|---|---|
| `kind == "final"` | Print the answer and stop. |
| `kind == "action"` with `action == "bash"` | Safety check, confirmation, execution, observation append, continue. |
| `kind == "invalid"` | Send formatting guidance back to the LLM and continue. |

Safety also controls execution:

| Safety result | Agent behavior |
|---|---|
| Broad destructive user task | Refuse before the LLM call. |
| Blocked command | Refuse before confirmation and before execution. |
| User denies confirmation | Do not execute; send denial as observation. |
| Allowed and confirmed command | Run `bash_tool.run_bash`. |

## Why This Is A Real Agent Loop

Part 1 is not a one-shot chatbot. A one-shot chatbot would ask the LLM once and
print whatever it says. This agent keeps state in `messages`, gives the model a
limited action format, executes the selected action in local Python code, and
feeds the result back into the next LLM call.

That repeated cycle is the core Part 1 behavior:

```text
Reason -> Act -> Observe -> Reason again -> Final Answer
```
