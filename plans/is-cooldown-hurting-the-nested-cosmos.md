# Is cooldown hurting the collaboration?

## Context

In the live session, two agents (`bob-swe`, `alice-swe`) each emitted a CLAIM on `/workspace/shared/calculator.py` with different scope fragments (`#basic-functionality` vs `#scope`). Bob then posted a substantive update ("I implemented add and subtract"), but **neither agent replied to the other's messages** until the operator typed "everyone please continue" two minutes later. The console logs show repeated `[skip] cooldown: last reply 1.0s ago` lines. The user wants to know whether this gate is hurting collaboration.

## Findings

**Cooldown is a per-agent reply-rate gate, not a context drop.** It lives in `assignment2_part3/reply_policy.py:42` and `:181-183`. Default is **30 seconds**, configurable via `REPLY_COOLDOWN_SECONDS`. Its job (`reply_policy.py:1-18`) is to prevent N×M chatter when many agents see the same broadcast.

**The gate ordering** (`reply_policy.py:155-196`) is:
1. own-message → skip
2. coordinator handoff → reply
3. direct mention → reply
4. claim collision (`lookup(path#scope)`) → reply, bypassing cooldown
5. **cooldown** → skip
6. broadcast → maybe reply, bounded by `MAX_BROADCAST_REPLIES`

**Two things in the user's session combined to break collaboration:**

### Problem 1: scope-mismatched CLAIMs are not recognized as collisions
`reply_policy._claim_collision()` uses `ClaimRegistry.lookup(path)` which keys by `path#scope` (`claims.py:72-73, 141-151`). Bob's `#basic-functionality` and Alice's `#scope` therefore don't match — even though `claims_conflict()` (`claims.py:76-83`) explicitly says "scopes conflict by name" and these scope names overlap semantically. The collision-bypass at line 172 misses the case. Cooldown then kicks in and silences the racing CLAIM. Net effect: no DEFER/tie-break exchange, and the agents each think they own the file independently.

### Problem 2: 30s cooldown is too long for an interactive coding session
Even outside the CLAIM-collision path, Bob's "I implemented add and subtract" arrived at Alice 1.0s after her own CLAIM. Alice was in cooldown → reply skipped. Messages **are** remembered (`_remember_inbound` at `group_chat.py:348` runs before the gate; `_absorb_inbound_claims` at `:389` runs on the skip path), so Alice's prior_context contains Bob's progress note — but she doesn't *act* on it. When the operator nudges 2 min later, Alice still re-reads the file from disk because she never got to react to the chat update in turn.

**So yes, cooldown is hurting collaboration in this scenario** — primarily because (a) scope-fragment CLAIMs slip past the collision bypass, and (b) 30s is far longer than the natural turn time of an active 2-agent coding pair (≈5-10s).

## Recommended fix

Two small, targeted changes — both in `assignment2_part3/reply_policy.py`:

### Change A — broaden collision detection to use `is_claimed_by_other`

Replace the scope-strict `claims.lookup(path)` call in `_claim_collision` (`reply_policy.py:126`) with a check that mirrors `ClaimRegistry.is_claimed_by_other()` (`claims.py:163-175`): same path + `claims_conflict()` semantics. That way any incoming CLAIM on a path this agent already owns (whole-file or overlapping scope) triggers the tie-break/DEFER reply, regardless of fragment-name mismatch.

```python
# reply_policy.py:_claim_collision (sketch — same shape, broader match)
for match in CLAIM_PATTERN.finditer(text):
    incoming_path, incoming_scope = split_claim_target(match.group("path"))
    for own in claims.active_claims_for(agent_id):
        if own.path == incoming_path and claims_conflict(own, _candidate(incoming_path, incoming_scope, peer_id)):
            winner = tie_break_winner(agent_id, peer_id)
            outcome = "self-wins" if winner == agent_id else "self-loses"
            return CollisionInfo(path=own.target, peer_id=peer_id, outcome=outcome)
```

Reuses existing functions: `split_claim_target`, `claims_conflict`, `tie_break_winner`, `ClaimRegistry.active_claims_for` — all already in `claims.py`.

### Change B — drop the default cooldown to 8s

`reply_policy.py:42` — change the default from `30` to `8`. Keep the env override (`REPLY_COOLDOWN_SECONDS`) so demos that want to suppress chatter can crank it back up. 8s is long enough to break tight echo loops but short enough that an agent reacts to a peer's substantive update within the same coordination round.

(Alternative — keep 30s and only bypass cooldown when a peer message contains any CLAIM/RELEASE/DEFER marker. More surgical, but Change A already handles the CLAIM-on-own-path case, and substantive non-marker updates would still be silenced. The default-lower approach addresses both.)

### Files to modify

- `assignment2_part3/reply_policy.py:42` — lower default
- `assignment2_part3/reply_policy.py:109-131` — broaden `_claim_collision`

### Tests to update / add

- `assignment2_part3/tests/test_reply_policy.py` — existing collision tests at the `:256-276` range. Add a case: agent owns `path#scope-A`, peer CLAIMs `path#scope-B` → expect `respond=True`, `outcome="self-wins"` (or `self-loses` depending on lex order).
- No change to cooldown tests required if they reference the env var rather than the literal 30.

## Verification

1. Run the unit tests:
   `cd assignment2_part3 && python -m pytest tests/test_reply_policy.py -v`
2. Replay the same live demo:
   `python tools/chat.py live --as emil-user`
   `> can @bob-swe and @alice-swe collaborate on building a calculator in python. divide it up between you`
   Expect: when both agents emit overlapping-scope CLAIMs, one posts `DEFER to @<winner>` within a few seconds *without* operator intervention. Expect: Bob's "I implemented add/subtract" gets a reply from Alice ("ok, I'll add multiply/divide") inside the same minute.
3. Sanity-check chatter ceiling — run a 3-agent broadcast (`@everyone status?`) and confirm `MAX_BROADCAST_REPLIES` still bounds the response count. With cooldown at 8s and `MAX_BROADCAST_REPLIES=1`/`BROADCAST_WINDOW_SECONDS=300`, broadcast back-off (`reply_policy.py:185-194`) still does the heavy lifting against N×M.
