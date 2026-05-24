# Repository Guidelines

## Project Structure & Module Organization

This repository contains three Python assignment parts:

- `assignment2_part1/`: minimal ReAct CLI agent with homemade text parsing, safety checks, Docker support, and tests.
- `assignment2_part2/`: structured JSON tool agent. Core modules include `agent.py`, `parser.py`, `tools.py`, `safety.py`, `session_store.py`, and `config/system_prompt.txt`.
- `assignment2_part3/`: hub-connected collaborative agent. It imports Part 2 through `part2_bridge.py`; do not duplicate Part 2 logic here.

Each part has its own `README.md`, `requirements.txt`, `Dockerfile`, `docker-compose.yml`, and `tests/` directory. Runtime scratch files belong under the relevant `workspace/` or `data/` directory. Planning notes are stored in `plans/`.

## Build, Test, and Development Commands

Install dependencies per part:

```powershell
cd assignment2_part2
python -m pip install -r requirements.txt
```

Run a local CLI agent from its part directory:

```powershell
python agent.py
```

Run tests from the repository root:

```powershell
python -m pytest assignment2_part1 -q
python -m pytest assignment2_part2 -q
python -m pytest assignment2_part3/tests -q
```

Use Docker Compose from a part directory when testing the containerized flow:

```powershell
docker compose build
docker compose run --rm agent
```

For Part 3's multi-agent local hub, use `docker compose up -d` and `python tools/chat.py live --as <name>`.

## Coding Style & Naming Conventions

Use Python 3 with 4-space indentation, clear function names, and small modules aligned to the existing files. Prefer pure functions for policy logic, such as `reply_policy.should_reply`, and keep environment/path setup isolated, as in `part2_bridge.py` and `agent.py`. Test files should be named `test_*.py`; modules use lowercase snake_case.

## Testing Guidelines

The project uses `pytest`. Add or update focused tests beside the affected part. For shared behavior, run both the direct target suite and any dependent suite; Part 3 changes often require `assignment2_part2` tests as well. Keep tests deterministic and avoid real provider calls by using mocks or local stubs.

## Commit & Pull Request Guidelines

Recent history mixes descriptive subjects with Conventional Commit prefixes, for example `feat(part3): ...` and `fix(tools): ...`. Prefer `type(scope): summary` for feature and fix work, and keep summaries imperative and specific.

Pull requests should describe the changed behavior, list test commands run, mention affected parts, and call out any environment or Docker changes. Include screenshots or terminal snippets only for CLI/hub behavior that is hard to verify from tests.

## Security & Configuration Tips

Never commit `.env`, API keys, session databases, or generated workspace contents. Keep provider credentials in per-part `.env` files. Preserve the command approval, safety blocklist, credential scrubber, and workspace confinement behavior unless the assignment explicitly requires a change.
