# Plan: single remote bot via compose profile

## Context

The user has a public RunPod hub URL + password and wants **one** agent
(using their personal identity `emil_hjaertfors_bot`) to join that hub,
while keeping the existing **two-agent local-hub** Docker flow
(`agent-alice` + `agent-bob` + `local-hub`) unchanged for everyday dev.

Original idea was to repurpose `AGENT_MODE=stub` for "local 2-agent" and
`AGENT_MODE=runpod` for "remote 1-agent", but that's incorrect:
`AGENT_MODE=stub` is the test transport (stdin/stdout JSON, no HTTP).
The local-hub flow already uses `AGENT_MODE=runpod` pointed at
`http://local-hub:8080`. The switch needs to happen at the
Docker-compose level, not via `AGENT_MODE`.

## Approach: Docker Compose profiles

Use compose **profiles** to gate which services start:

- `local-hub`, `agent-alice`, `agent-bob` → `profiles: [local]`
- New service `agent-remote` → `profiles: [remote]`

Then:
- `docker compose --profile local up -d`  → today's two-agent local-hub flow.
- `docker compose --profile remote up -d agent-remote`  → single bot to RunPod.

Services without a matching `--profile` are not started, so the local hub
container stays dormant in remote mode (no port conflict, no wasted memory).

The single remote bot reads its identity directly from `.env`
(`AGENT_ID`, `AGENT_DISPLAY_NAME`) per the user's choice — no new env vars.

## Files to modify

### 1. `assignment2_part3/docker-compose.yml`

- Add `profiles: [local]` to `local-hub` (line 2 block), `agent-alice`
  (line 14 block), `agent-bob` (line 53 block).
- Add a new `agent-remote` service after `agent-bob`, structured like
  `agent-alice` but with these differences:
  - `profiles: [remote]`
  - `AGENT_ID: ${AGENT_ID:?Set AGENT_ID in .env for the remote bot}`
  - `AGENT_DISPLAY_NAME: ${AGENT_DISPLAY_NAME:?Set AGENT_DISPLAY_NAME in .env}`
  - `AGENT_WORKSPACE: /workspace/${AGENT_ID}`
  - `AGENT_SESSION_DB: /data/${AGENT_ID}.sqlite3`
  - `LLM_PROVIDER_ORDER: ${LLM_PROVIDER_ORDER:-local}`
  - **No** `depends_on: local-hub` (the remote hub is external).
  - `RUNPOD_CHAT_URL: ${RUNPOD_CHAT_URL:?Set RUNPOD_CHAT_URL in .env to the RunPod hub URL}`
    (no fallback to `http://local-hub:8080` for this service — it must be
    a real remote URL, otherwise fail fast).
  - Same `RUNPOD_CHAT_PASSWORD`, poll interval, `LOCAL_LLM_BASE_URL`,
    `AGENT_TPM_LIMIT`/`AGENT_RPM_LIMIT`/`AGENT_TOTAL_TOKEN_LIMIT`,
    `stdin_open: true`, `tty: true`, security options, and `cpus`/`mem_limit`
    as the alice/bob blocks (so console control via `docker attach` works
    the same way).
  - Same volume mounts (`./workspace:/workspace`, `./data:/data`).

### 2. `assignment2_part3/.env`

No new variables required. The user already has:

```
AGENT_ID=emil_hjaertfors_bot
AGENT_DISPLAY_NAME=emil_hjaertfors_bot
AGENT_MODE=runpod
RUNPOD_CHAT_URL=<runpod hub url>
RUNPOD_CHAT_PASSWORD=<actual password>   # NOT th25-agents-vg (group name)
```

Reminder for the user (not a code change): the `th25-agents-vg` value
currently in `RUNPOD_CHAT_PASSWORD` looks like a group/room identifier,
not a password. The 401 won't clear until the real password is used.

### 3. `assignment2_part3/README.md`

Append a short "Mode 4: single agent to a remote hub" section after the
existing modes (around lines 122–191), documenting:

```bash
docker compose --profile remote up -d agent-remote
docker attach assignment2_part3-agent-remote-1   # :budget :approve :pause
```

And note that `agent-alice` / `agent-bob` are NOT started in this mode.

### 4. `assignment2_part3/demo.md`

Add a brief "Single bot to remote hub" section that mirrors the existing
4-terminal layout but with only:
- T1: `docker compose --profile remote logs -f agent-remote`
- T2: `docker attach assignment2_part3-agent-remote-1`
- T3: `python tools/chat.py live --url <runpod url> --password <pw> --as emil-user`

## Reused existing code (no changes needed)

- `agent.py:1-47` — already reads `AGENT_ID`, `AGENT_DISPLAY_NAME`,
  `AGENT_WORKSPACE`, `AGENT_SESSION_DB`, `AGENT_MODE` from env. One
  process per container is exactly what the new service needs.
- `transport.py:391-420` (`build_transport`) — already builds
  `RunPodTransport` when `AGENT_MODE=runpod`, reads `RUNPOD_CHAT_URL`
  and `RUNPOD_CHAT_PASSWORD`. No transport-layer changes.
- `transport.py:122` (`FORBIDDEN_HUB_NAMES`) — `emil_hjaertfors_bot` is
  not in the list, so the hub-name validator passes.
- `console_control.py` (daemon stdin thread) — `:budget`, `:approve`,
  `:say`, `:pause` etc. work over `docker attach` regardless of which
  hub the agent is talking to.
- `tools/chat.py` — already URL/password-configurable; no changes.
- `tools/audit.py` — uses `data/<agent_id>.sqlite3`, so the remote bot's
  SQLite file picks up automatically (`data/emil_hjaertfors_bot.sqlite3`).

## Side-effect from prior turn (carry forward)

In the previous turn we already changed `docker-compose.yml` lines 34
and 73 from `${LOCAL_AGENT_HUB_URL:-...}` to `${RUNPOD_CHAT_URL:-...}`,
and updated `README.md:190-191` accordingly. Both changes remain — they
make `RUNPOD_CHAT_URL` in `.env` actually take effect for alice/bob too,
which is consistent with the new single-bot service.

## Verification

1. **Local 2-agent flow still works:**
   ```bash
   docker compose --profile local up -d
   docker compose ps                                  # alice, bob, local-hub all up
   docker compose exec agent-alice printenv RUNPOD_CHAT_URL
   # → http://local-hub:8080
   python -m pytest assignment2_part3/tests -q
   ```

2. **Single remote bot:**
   ```bash
   docker compose --profile local down
   docker compose --profile remote up -d agent-remote
   docker compose ps                                  # only agent-remote up
   docker compose exec agent-remote printenv RUNPOD_CHAT_URL RUNPOD_CHAT_PASSWORD AGENT_DISPLAY_NAME
   # → <runpod url>  <real pw>  emil_hjaertfors_bot
   docker compose logs -f agent-remote                # should NOT see 401
   ```

3. **Console control works:**
   ```bash
   docker attach assignment2_part3-agent-remote-1
   :budget
   # detach with Ctrl-P Ctrl-Q (NOT Ctrl-C)
   ```

4. **Operator chat reaches the bot via RunPod:**
   ```bash
   python tools/chat.py live --url <runpod url> --password <pw> --as emil-user
   # send "hi emil_hjaertfors_bot, are you there?"
   # bot should reply
   ```

5. **Both flows are mutually exclusive:** `docker compose up -d` without
   any `--profile` flag starts nothing now (all services are profiled).
   This is intentional — it forces explicit mode choice. If the user
   wants the old default behavior, add `profiles: [local, default]` to
   the local-hub services. Recommend NOT doing that; explicit is safer.
