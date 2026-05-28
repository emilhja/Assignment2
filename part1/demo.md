# Part 1 Demo Transcript

Use this demo to show that Part 1 is a real ReAct bash agent:

- the model chooses bash commands
- the agent parses raw text tool calls
- command output is fed back as `Observation:`
- the loop can continue across several tool calls
- destructive commands are refused or blocked before execution
- Docker starts in `/workspace`, so created files persist in `part1/workspace`

## Start

```bash
docker compose run --rm agent
```

Expected startup:

```text
Assignment 2 Part 1 ReAct Bash Agent
Enter a task, or type 'exit' or 'quit' to stop.
```

## 1. Multi-Step File And Script Task

Task:

```text
create a python script that writes numbers 1 to 5 to numbers.txt, then run it
```

Example interaction:

```text
Proposed command:
printf 'with open("numbers.txt", "w") as f:\n    for i in range(1, 6):\n        f.write(f"{i}\\n")\n' > make_numbers.py
Run this command? [y/N] y

Proposed command:
python make_numbers.py
Run this command? [y/N] y

Proposed command:
cat numbers.txt
Run this command? [y/N] y

Final answer:
numbers.txt contains the numbers 1 through 5, one per line.
```

This demonstrates a real Reason -> Act -> Observe -> Repeat loop: the agent
creates a script, runs it, reads the generated file, and answers from the
observed output.

## 2. Workspace Listing

Task:

```text
list all files
```

Example interaction:

```text
Proposed command:
ls -l
Run this command? [y/N] y

Final answer:
The workspace contains make_numbers.py, numbers.txt, and the existing files.
```

This demonstrates that commands run in the mounted workspace by default.

## 3. Destructive Intent Refusal

Task:

```text
remove all files
```

Expected result:

```text
Final answer:
I cannot do that. Broad deletion is not allowed.
```

This demonstrates that broad destructive user intent is refused before asking
the model for a command.

## 4. Destructive Command Block

Task:

```text
can you delete numbers.txt
```

Expected result:

```text
Final answer:
I cannot run that command. Blocked: rm is not allowed.
```

This demonstrates that destructive model-selected commands are blocked before
confirmation and before bash execution.
