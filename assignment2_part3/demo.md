# Demo: Build & Start the Docker Agent

Run all commands from `assignment2_part3/`.

## 1. Prepare `.env`

```bash
cp .env.example .env
# edit .env and set GROQ_API_KEY or OPENAI_API_KEY
```

## 2. Build the images

```bash
docker compose build
```

Builds `agent-alice` and `agent-bob` from `Dockerfile` (Python 3.12-slim,
non-root `agentuser`, Part 2 + Part 3 copied into `/app`).

## 3. Start both agents

```bash
docker compose up
```

Brings up `agent-alice` and `agent-bob` in the foreground. Both share the
host-mounted `./workspace` and `./data` volumes but write into their own
`workspace/<AGENT_ID>/` subtree via `AGENT_WORKSPACE`.

Detached mode:

```bash
docker compose up -d
docker compose logs -f agent-alice
```

## 4. Stop

```bash
docker compose down
```

## Chat with your agent (stub mode)

`docker compose up` runs both agents on stub mode, but stdin lands in
docker compose's combined stream — not useful for typing. Two cleaner ways:

```bash
# Attach to one running container; detach with Ctrl-P, Ctrl-Q
docker compose up -d
docker attach assignment2_part3-agent-alice-1

# Or one-shot a single message per invocation
echo '{"id":"m1","sender_id":"you","text":"@alice list utils.py"}' \
  | docker compose run --rm -T agent-alice
```

While the agent runs locally (`python agent.py`), type `:say <text>` to
post a message to the active transport without going through the LLM.

## Multi-agent chat in Docker via the local hub

The Docker agents talk to each other (and to you) through a local mock
of the TH25 hub. Both agents flip to `runpod` mode and point at
`host.docker.internal:8080`, which `docker-compose.yml` maps to the host.

### 1. Start the local hub on the host

```bash
cd assignment2_part3
python tools/local_hub.py --password local-hub
# logs every POST so you can see the chat flowing
```

Keep this terminal open. Default port is `8080`, default password is
`local-hub`. Use `--quiet` to suppress request logs.

### 2. Point the Docker agents at it (`.env`)

```
AGENT_MODE=runpod
RUNPOD_CHAT_URL=http://host.docker.internal:8080
RUNPOD_CHAT_PASSWORD=local-hub
RUNPOD_CHAT_POLL_INTERVAL=2
```

`AGENT_DISPLAY_NAME` is set per-service in `docker-compose.yml`
(`alice-swe`, `bob-swe`). Override there if you want a different name.

### 3. Bring up the agents

```bash
docker compose up
```

Both containers connect to the local hub and start polling. You'll see
`[hub<-]` / `[hub->]` lines in the docker logs as messages flow.

### 4. Chat with them from a third terminal

```bash
python tools/chat.py say "@alice-swe please list files in /workspace"
python tools/chat.py tail --follow             # stream the conversation
python tools/chat.py stats                     # per-agent counts
```

`chat.py` reads `LOCAL_HUB_URL`, `LOCAL_HUB_PASSWORD`, and `LOCAL_HUB_USER`
from the env. Override per-call with `--url`, `--password`, `--as`.

### Approvals still happen locally

If alice proposes a bash command, the `[approval needed] bash> ...`
prompt appears **in alice's container log** (the docker compose stream),
not on the hub. Answer with `:approve` or `:deny` via
`docker attach assignment2_part3-agent-alice-1`. The hub log never sees
the bash command or the approval.

## Run against the TH25 hub (opt-in)

The hub connection is **manual**. Nothing is sent to RunPod until you
flip the mode.

1. Edit `.env`:
   ```
   AGENT_MODE=runpod
   AGENT_DISPLAY_NAME=<unique-name>   # format: yourname-rolename
   RUNPOD_CHAT_URL=https://wb48jtfnjng6on-8080.proxy.runpod.net
   RUNPOD_CHAT_PASSWORD=<hub password>
   ```
2. `python agent.py` — banner reads `listening via runpod`.
3. Watch for inbound traffic and post your own:
   ```
   [hub<-] seq=12 someone-else: hi everyone
   :say hello, I'm joining the discussion
   [hub->] seq=13 <your-name>: hello, I'm joining ...
   ```

Approvals stay local. If the LLM proposes a bash command, you'll see
`[approval needed] bash> ...` in **your** terminal; reply with
`:approve` or `:deny`. The hub never sees that exchange — only the
final scrubbed answer is posted.

## Notes

- Default `AGENT_MODE=stub` — agents read PeerMessage JSON from stdin and
  write replies to stdout. Switch to `runpod` in `.env` once the hub URL +
  token are set (see README §"Wiring the live RunPod group chat").
- Resource caps per container: `cpus: 1.0`, `mem_limit: 512m`,
  `pids_limit: 100`, all caps dropped, `no-new-privileges`.
- To run just one agent: `docker compose up agent-alice`.
- To rebuild after code changes: `docker compose build --no-cache`.
