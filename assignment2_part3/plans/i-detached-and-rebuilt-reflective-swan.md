# Why a rebuilt agent "continued" the prior calculator task

## Context

You ran `docker compose build agent && docker compose up` on the agents only, expecting a clean slate. Bob came back and immediately:

- Re-emitted `Done: implemented multiply and divide functions ...`
- Re-issued a fresh `CLAIM /workspace/shared/calculator.py#multiply-divide`
- Then processed your new `@bob-swe run the tests`

That looked like Bob remembered the previous conversation. He didn't — his in-memory LLM context was wiped. What actually happened: **the hub still had the entire prior message backlog in memory, and Bob re-fetched it on reconnect**.

## Diagnosis — what survives a rebuild

| Component | Where it lives | Bind-mounted? | Wiped by agent rebuild? |
|---|---|---|---|
| Agent LLM history (`recent_context`) | process memory (`group_chat.py:276`) | n/a | **YES** (wiped) |
| Hub message backlog | hub process memory (`tools/local_hub.py:54`, `HubState._messages: list[dict]`) | no | **NO** — only wiped by restarting the hub container |
| Agent "seen" cursor | `data/seen_ids_<AGENT_ID>.json` (`transport.py:33-42`) | yes (`./data:/data`) | **NO** |
| Per-agent audit log | `data/<AGENT_ID>.sqlite3` (`thread_safe_store.py:23-72`) | yes | **NO** (but not fed back to LLM) |
| Budget lifetime counter | `data/budget_<AGENT_ID>.json` (`budget.py:50-69`) | yes | **NO** (lifetime cap survives by design) |
| Claim registry | process memory (`claims.py:113`) | no | **YES** (but stale CLAIM/RELEASE text is still in the hub backlog, so peers may re-observe it until the 5-min TTL passes) |

So when you rebuild **only the agent containers**:

1. Bob starts fresh with empty `recent_context` and empty `ClaimRegistry`.
2. `RunPodTransport` loads `data/seen_ids_bob-swe.json` and asks the hub for messages with `seq > last_seen`.
3. The hub (still running, still holding every message in `HubState._messages`) returns whatever wasn't yet acknowledged — including any unread `@bob-swe` from the prior session.
4. Bob's reply gate (`should_reply`) sees a direct mention and replies. With no prior context, the LLM has to reconstruct intent from one stray message and the assignment hint injected by `coordination.py` — which is exactly the "continuing the old task" behavior you saw.

There's a second, subtler effect: even if Bob *had* seen the prior message, restart-time peer messages like alice's leftover `CLAIM ...#add-subtract` get replayed into Bob's view, so the coordination hints behave as if Alice has just claimed her half.

## Recommendation

This is a procedure issue, **not a code bug** — nothing to implement. For a true cold reboot of the multi-agent demo, also wipe the hub backlog and the seen-cursors:

```bash
cd assignment2_part3

# 1. stop everything
docker compose down

# 2. clear per-agent persistent state on host (bind-mounted into containers)
rm -f data/seen_ids_*.json          # forces fresh "since=0" semantics, but combined with #3 there's nothing to re-fetch
rm -f data/*.sqlite3                # optional: clears audit log
rm -f data/budget_*.json            # optional: also resets budget lifetime counter
rm -rf workspace/shared/*           # optional: clears any leftover calculator.py / tests

# 3. start hub fresh (this is the important one — wipes HubState._messages)
docker compose up -d local-hub

# 4. then start agents
docker compose up -d agent-alice agent-bob
docker compose logs -f
```

Short version of your question: **yes, also restart (or rebuild) the hub** — `docker compose restart local-hub` alone is enough to flush the backlog; a full `down` + `up` is the belt-and-braces version. Rebuilding the agent image is only needed when you've changed agent-side source code.

### Optional follow-up (only if this keeps biting you)

If you want a one-shot reset command, the lightest-touch option is a `tools/reset_demo.sh` (or a `make reset` target) that wraps the four steps above. I'd hold off on this until you've confirmed the procedure above resolves the issue — adding scripts pre-emptively is the kind of scope creep the project conventions warn against.

## Verification

After running the cleanup:

1. `docker compose logs local-hub` should show the hub starting fresh (no replay of old messages).
2. `python tools/chat.py live --as emil-user` then `@bob-swe ping` — Bob should reply with no recollection of the calculator task and no spontaneous `CLAIM`.
3. `python tools/audit.py tail --agent bob-swe -n 20` should show only events from after the restart.

## Critical files referenced (read-only — no edits needed)

- `assignment2_part3/tools/local_hub.py:49-87` — in-memory `HubState._messages`
- `assignment2_part3/transport.py:33-42, 233` — `seen_ids` cursor + `recv` since-semantics
- `assignment2_part3/group_chat.py:250, 261, 276` — budget load, claims init, recent_context
- `assignment2_part3/docker-compose.yml:42-44` — `./workspace` and `./data` bind mounts
- `assignment2_part3/claims.py:24, 113` — 5-min TTL, in-memory dict
- `assignment2_part3/budget.py:50-69` — budget JSON persistence
