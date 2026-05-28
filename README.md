# Assignment 2

This repository contains three Python agent implementations that build on each
other:

- `part1/` - a minimal ReAct-style CLI agent that parses a homemade text
  protocol and can run approved bash commands.
- `assignment2_part2/` - a structured JSON tool agent with workspace file
  tools, command safety checks, session logging, and tests.
- `assignment2_part3/` - a hub-connected collaborative agent that reuses Part 2
  through `part2_bridge.py` and adds group-chat transport, budgets, reply
  policy, coordination, and audit tools.

Part 3 imports Part 2 logic instead of duplicating it. Keep shared behavior in
Part 2 unless a change is truly specific to the collaborative hub flow.

## Repository Layout

```text
.
|-- part1/                 ReAct bash agent
|-- assignment2_part2/     JSON tool agent
|-- assignment2_part3/     hub-connected collaborative agent
|-- plans/                 root-level planning notes, if any
`-- README.md              repository overview
```

Each part has its own `requirements.txt`, `Dockerfile`, `docker-compose.yml`,
tests, workspace/data directories, and more detailed docs:

- `part1/dev_docs/README.md`
- `assignment2_part2/dev_docs/README.md`
- `assignment2_part3/README.md`

## Quick Start

Install dependencies inside the part you want to run:

```powershell
cd part1
python -m pip install -r requirements.txt
```

or:

```powershell
cd assignment2_part2
python -m pip install -r requirements.txt
```

or:

```powershell
cd assignment2_part3
python -m pip install -r requirements.txt
```

Create a local environment file from the example for the part you are using:

```powershell
Copy-Item .env.example .env
```

Then fill in only the credentials and provider settings needed for your run.
The `.env.example` files are committed as templates; real `.env` files are
local-only and ignored by Git.

## Running Agents

Part 1:

```powershell
cd part1
python agent.py
```

Part 2:

```powershell
cd assignment2_part2
python agent.py
```

Part 3 stub mode:

```powershell
cd assignment2_part3
echo '{"id":"m1","sender_id":"bob","text":"@alice list files in /workspace"}' | python agent.py
```

Part 3 local hub mode:

```powershell
cd assignment2_part3
docker compose --profile local up -d
python tools/chat.py live --as emil-user
```

See `assignment2_part3/README.md` for remote hub setup, operator commands,
budget controls, claim/release coordination, and audit tooling.

## Testing

Run tests from the repository root:

```powershell
python -m pytest part1 -q
python -m pytest assignment2_part2 -q
python -m pytest assignment2_part3/tests -q
```

When changing Part 2 behavior, also run the Part 3 tests because Part 3 imports
Part 2 through `assignment2_part3/part2_bridge.py`.

## Docker

Each part can be built and run from its own directory:

```powershell
docker compose build
docker compose run --rm agent
```

Part 3 also defines profile-based multi-agent flows:

```powershell
docker compose --profile local up -d
docker compose --profile local logs -f
```

Use the `remote` profile only when intentionally connecting a single agent to a
remote hub.

## Environment Files

Commit templates:

- `part1/.env.example`
- `assignment2_part2/.env.example`
- `assignment2_part3/.env.example`

Do not commit:

- `.env`
- `.env.*` except `.env.example`
- API keys, provider tokens, session databases, logs, or generated workspace
  contents

The root `.gitignore` keeps real environment files ignored while allowing
example files to be shared.

## Development Notes

- Use Python 3, 4-space indentation, and lowercase snake_case module names.
- Keep tests focused and deterministic. Mock provider calls instead of using
  real network calls in tests.
- Preserve command safety checks, credential scrubbing, workspace confinement,
  and manual approval behavior unless the assignment explicitly requires a
  change.
- Runtime scratch output belongs under the relevant part's `workspace/` or
  `data/` directory.
