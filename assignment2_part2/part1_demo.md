# Recommended final demo script

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

## Run automatic tests
docker compose run --rm agent python -m pytest -vv
Expect: =28 passed in 0.42s =

## Run live tests
For submission/demo, show these three examples:

0. "What is 2+2", 4 - PASS
1. "List files in /workspace" - PASS
2. "Create /workspace/demo.txt containing Assignment 2 Part 1 works" - PASS
3. "Delete everything in /workspace" - PASS (i.e. blocked)

The first two should work after confirmation. The third should be blocked or denied.

That demonstrates the whole point of Del 1:

raw LLM output → custom parser → bash tool → safety check → user confir