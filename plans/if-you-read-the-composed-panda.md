# Plan: Wire Part 3 to the TH25 RunPod Hub (Opt-In)

## Context

Part 3 already has the full agent loop (`group_chat.run_group_chat`) plus
the `Transport` protocol abstraction, but `transport.RunPodTransport` is a
placeholder that raises `NotImplementedError`. The TH25 lecture hub is now
live at `https://wb48jtfnjng6on-8080.proxy.runpod.net` (REST + JSON,
password-protected, 1 req/s, 10-msg per-agent cap — see
`assignment2_part3/th25-hub-connection.md`).

Goal: the user wants to **prepare** the connection so a single env flip
(`AGENT_MODE=runpod`) makes their local agent join the hub, chat with the
other agents listed there, and surface every hub send/recv as a
confirmation line on the **local** terminal. Bash approvals must continue
to happen on the local console (`:approve` / `:deny`) — the hub never
sees the approval prompts. Default stays `stub`; nothing auto-connects.

User-confirmed design choices:
- **Operator input:** new `:say <text>` console command posts to the hub
  as the agent.
- **Opt-in:** env var `AGENT_MODE=runpod`. No CLI flag.
- **Env vars:** new `RUNPOD_CHAT_PASSWORD` (fallback to existing
  `RUNPOD_CHAT_TOKEN` so the current `.env` keeps working).
- **Docker:** `docker-compose.yml` stays on `AGENT_MODE=stub`. Hub opt-in
  is done by overriding `AGENT_MODE` per-run.

## Files to change

### 1. `assignment2_part3/transport.py` — implement `RunPodTransport`

Replace the placeholder body. Keep the existing `Transport` Protocol and
`StubTransport` untouched.

- `__init__(self, agent_name, url, password, *, poll_interval=4.0,
  seen_path=None, session=None, stdout=sys.stdout)`:
  - `agent_name` is the hub identity (taken from `AGENT_DISPLAY_NAME`).
  - `session` defaults to `requests.Session()` — injected in tests.
  - `stdout` is where the `[hub→]` / `[hub←]` confirmation lines go; tests
    inject a `StringIO`.
  - Keep `self._seen` + `_seen_path` JSON dedup (same pattern as
    `StubTransport`, file `data/seen_messages_<agent_id>.json`).
  - Track `self._last_seen_seq: int = 0` (resume value if the seen file
    encodes the highest seq it has seen).
  - `self._last_request_ts: float = 0.0` — enforces the hub's 1 req/s.
- `_throttle()`: sleeps until `time.monotonic() - self._last_request_ts
  >= 1.0`; updates `self._last_request_ts` on return.
- `recv(timeout)`:
  - Respect `timeout` (default 1.0 from `group_chat`): if a poll is too
    soon, sleep min(timeout, gap) and return `None`.
  - `_throttle()`, then `GET {url}/api/messages?since={last_seen}&password={pw}`.
  - On `200`: iterate `messages`, filter (a) `seq in self._seen` and
    (b) `agent_name == self.agent_name` (don't echo own messages).
  - Pick the next un-seen message; mark `self._seen.add(str(seq))`,
    update `self._last_seen_seq = max(...)`, persist via
    `_save_seen_ids`.
  - Build a `PeerMessage` (see field map below) and return it. Remaining
    messages in the batch stay in `self._buffer: deque` so the next
    `recv` doesn't burn a request.
  - On `401` / `429` / network error: print a one-line `[hub!]`
    diagnostic to `self._stdout`, sleep a small backoff (4s for 429,
    2s otherwise), return `None`. **Never raise** — the main loop should
    keep running.
- `send(text)`:
  - Guard: empty / whitespace-only → no-op.
  - Truncate to 4096 chars (hub limit).
  - `_throttle()`, then `POST {url}/api/message` with JSON
    `{"agent_name": self.agent_name, "content": text, "password": pw}`.
  - On `200`: write `[hub→] seq={seq} {agent_name}: {snippet}` to
    `self._stdout`.
  - On non-200: write `[hub!] send failed status={code} body={body[:200]}`
    to `self._stdout`. Do not raise.
- `close()`: set `_closed`, call `session.close()`.

**PeerMessage field map** (verified against `peer.PeerMessage` at
`assignment2_part3/peer.py:19-26`):
| Hub field | PeerMessage field |
|---|---|
| `seq` (int) | `id = str(seq)` |
| `agent_name` (str) | `sender_id` |
| `content` (str) | `text` |
| `timestamp` (ISO8601) | `received_at = time.time()` (parse is optional) |
| — | `addressed_to = ()` (hub has no addressing; `reply_policy.should_reply` already detects `@display_name` mentions in `text`) |

**`build_transport` (transport.py:147-159)** — extend the `runpod` branch:
- Read `RUNPOD_CHAT_URL` (required), strip trailing `/`.
- Resolve password as
  `os.environ.get("RUNPOD_CHAT_PASSWORD") or os.environ.get("RUNPOD_CHAT_TOKEN") or ""`.
  Raise `RuntimeError` if both are empty.
- Read `RUNPOD_CHAT_POLL_INTERVAL` (default `4.0`).
- Resolve `agent_name = os.environ.get("AGENT_DISPLAY_NAME") or
  f"{agent_id}-swe"` (matches `group_chat.run_group_chat:75`).
- Validate name against hub rule: reject `{"my-agent", "my_agent",
  "agent", "test", "bot", agent_id == "local"}`. Friendly error.

### 2. `assignment2_part3/console_control.py` — add `:say`

- New optional ctor arg `send_fn: Optional[Callable[[str], None]] = None`.
- `_handle`: add `elif cmd == "say": self._cmd_say(line[1:])`. Extract
  text after the literal `say ` prefix (preserve casing, allow spaces).
  Empty → `[usage: :say <text>]`.
- `_cmd_say(args_text)`:
  - If `self.send_fn is None`: `[say not wired — transport unavailable]`.
  - Else call `self.send_fn(text)` inside a try/except; on success print
    nothing locally (the transport's own `[hub→]` line is the receipt).
- Add `:say <text>` row to `HELP_TEXT` and to the docstring command list.

### 3. `assignment2_part3/group_chat.py` — wire confirmation + send_fn

- After building `transport` (line 98), instantiate `ConsoleControl` with
  `send_fn=transport.send` (the auto-create branch at line 93-95).
  Refactor: build `transport` **before** the console block so the
  `send_fn` is available.
- Add a tiny local helper `def _hub_echo(prefix, msg_text): print(f"[hub{prefix}] {msg_text[:160]}", flush=True)`.
- After `message = transport.recv(...)` returns non-None (line 111-114
  branch where we proceed): call `_hub_echo("←", f"{message.sender_id}: {message.text}")`.
- After `transport.send(answer)` (line 135): call `_hub_echo("→", answer)`.
  (When `transport` is the new `RunPodTransport`, the transport will
  *also* print its own `[hub→] seq=N ...` line — that's fine; they
  complement: group_chat's line confirms "we sent", transport's line
  confirms "hub accepted with seq N".) For tidiness, gate the
  group_chat echo on `mode != "runpod"` so we don't get duplicate
  `[hub→]` lines in runpod mode. Stub mode keeps the echo so the
  operator sees local replies.

### 4. `assignment2_part3/.env.example`

- Add `RUNPOD_CHAT_PASSWORD=` below `RUNPOD_CHAT_URL`.
- Add `RUNPOD_CHAT_POLL_INTERVAL=4` with a comment about the hub's 1 req/s.
- Leave existing `RUNPOD_CHAT_TOKEN=` line with a `# fallback for
  RUNPOD_CHAT_PASSWORD` comment.
- Add a one-line note: `# To join the hub: set AGENT_MODE=runpod and
  set a unique AGENT_DISPLAY_NAME (format yourname-rolename)`.

### 5. `assignment2_part3/requirements.txt`

- Verify `requests` is listed. If not, add `requests>=2.31`.

### 6. `assignment2_part3/README.md`

- Replace the placeholder "Wiring the live RunPod group chat" section
  (lines 159-167) with concrete steps: env vars to set, the unique-name
  rule (link to `th25-hub-connection.md`), the opt-in flip, and how the
  local console keeps working (`:say`, `:approve`, `:budget`).
- Add `:say <text>` row to the operator-console table.
- Add a short bullet under "Quickstart" noting the new `[hub→]` /
  `[hub←]` / `[hub!]` log prefixes.

### 7. `assignment2_part3/demo.md`

- New section "Run against the TH25 hub" with the env recipe:
  ```
  AGENT_MODE=runpod
  AGENT_DISPLAY_NAME=<your-unique-name>
  RUNPOD_CHAT_URL=https://wb48jtfnjng6on-8080.proxy.runpod.net
  RUNPOD_CHAT_PASSWORD=th25-agents-vg
  ```
- Show a sample local session:
  ```
  $ python agent.py
  [part3] <name> (id=...) listening via runpod. Type :help for console commands.
  [hub←] some-other-agent: hi everyone
  :say hello, I'm joining the discussion on X
  [hub→] seq=N <name>: hello, I'm joining ...
  ```
- Note: bash approvals still appear locally; the hub never sees
  `:approve`/`:deny`.

### 8. `assignment2_part3/tests/test_transport.py` — RunPodTransport tests

Inject a fake `session` object exposing `.get(url, params=...)` and
`.post(url, json=...)` returning a tiny `_FakeResponse(status_code,
json_payload)` helper. Cover:
- `test_runpod_send_posts_expected_payload` — verify URL, JSON body
  (agent_name, content, password), 4096 truncation.
- `test_runpod_recv_returns_peermessage_and_dedups` — feed two messages,
  call recv twice, assert second recv returns the second message; call
  recv a third time after exhausting buffer and assert it does a new
  GET; assert seen_messages JSON is persisted.
- `test_runpod_recv_skips_own_messages` — message with `agent_name ==
  self.agent_name` is not returned.
- `test_runpod_send_handles_429_without_raising` — fake returns 429;
  assert a `[hub!]` line is written to the injected stdout buffer and
  no exception escapes.
- `test_runpod_throttle_enforces_one_second` — patch `time.monotonic`
  to a fake clock and assert `_throttle` sleeps when calls arrive
  faster than 1/s.
- `test_build_transport_runpod_requires_password` — clear both env
  vars, expect `RuntimeError`. With `RUNPOD_CHAT_TOKEN` only, expect
  success (fallback path).

### 9. `assignment2_part3/tests/test_console_control.py` — `:say` tests

- `test_say_invokes_send_fn` — instantiate with a fake `send_fn` capturing
  calls; feed `":say hello world\n"` through stdin; assert send_fn got
  `"hello world"`.
- `test_say_without_text_prints_usage` — `":say\n"` writes `[usage: ...]`.
- `test_say_without_send_fn_warns` — `send_fn=None`; assert the
  unavailable message is printed.

## Functions/modules reused (no new abstractions needed)

- `peer.PeerMessage` — same dataclass used by `StubTransport`.
- `_load_seen_ids` / `_save_seen_ids` (`transport.py:29-43`) — JSON dedup
  store. `RunPodTransport` should reuse these directly.
- `reply_policy.should_reply` — already detects `@display_name` mentions
  in `PeerMessage.text`; no changes needed to handle hub-shaped messages.
- `ConsoleControl.request_bash_approval` (`console_control.py:77-92`) —
  unchanged; local approval flow already correct.
- `ThreadSafeSessionStore` — already records every reply_decision; no
  change needed. Optionally log a `hub_send` / `hub_recv` row from the
  RunPodTransport (kept out of scope unless trivial).

## Out of scope (explicit)

- Auto-flipping to runpod mode anywhere. The user wants manual opt-in.
- Changing `AGENT_MODE` default in `docker-compose.yml`.
- Implementing the OpenAI/Groq example loop from the hub doc — Part 3's
  `peer_task.run_peer_task` already does the LLM round-trip; we just
  swap the transport.
- Hub `/api/stats` endpoint — useful for debug but not required for the
  send/recv loop. Can be added later as a `:hub-stats` console command.

## Verification

1. **Unit tests:**
   ```bash
   python -m pytest assignment2_part3 -q
   ```
   Existing 59 tests must still pass; the new RunPodTransport and
   `:say` tests add ~7 more.

2. **Stub mode still works (regression):**
   ```bash
   cd assignment2_part3
   echo '{"id":"m1","sender_id":"bob","text":"@alice list utils.py"}' \
     | AGENT_ID=alice python agent.py
   ```
   Should produce a reply on stdout, no hub traffic.

3. **`:say` end-to-end in stub mode:** start `python agent.py`, type
   `:say hello` — confirm a JSON line appears on stdout
   (`{"sender_id": ..., "text": "hello", ...}`).

4. **Hub dry-run (manual, only when the user is ready):**
   - Fill `.env`: `AGENT_MODE=runpod`, unique `AGENT_DISPLAY_NAME`,
     `RUNPOD_CHAT_URL`, `RUNPOD_CHAT_PASSWORD`.
   - `python agent.py` — observe the banner switches to
     `listening via runpod`.
   - Watch for `[hub←]` lines as other agents post.
   - Type `:say hi everyone` — expect `[hub→] seq=N ...` confirmation.
   - Verify on the hub dashboard
     (`https://wb48jtfnjng6on-8080.proxy.runpod.net/`) that the agent
     name appears.
   - If the LLM proposes a bash command, expect the
     `[approval needed] bash> ...` line to appear **locally only**;
     type `:approve` or `:deny`. Confirm the hub log shows no approval
     text — only the resulting answer (or refusal).
