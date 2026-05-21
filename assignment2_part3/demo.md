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
