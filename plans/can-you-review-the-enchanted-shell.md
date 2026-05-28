# Review: emil-hjaertfors-agent in the 4-agent calculator chat

## Context

The user attached to `emil-hjaertfors-agent` (container `agent-remote-1`) while four agents (`emil-hjaertfors-agent`, `josef-agent`, `marcus-udd-agent`, `lullo-swe-agent`) were asked to play a sequential dev team (Planner → Developer → Tester → Refiner) and build a basic calculator. This is a post-mortem of how *only* the emil-agent behaved, since that's the one whose logs we can see.

Overall verdict: **mostly worked, but with three real defects worth flagging**. Not as clean as it looks at first read.

---

## What worked well

1. **Took the Planner role cleanly (18:14).** Produced a coherent spec in Swedish, listed the four ops, conceptual buttons, expected output, and named division-by-zero / non-numeric input as error cases. Matches the spec the user actually wanted.
2. **Persisted the spec to disk** as `calculator_spec.md` (593b) rather than only chatting it. Good behavior — later agents could read it.
3. **Safety gate caught `python3` (18:22).** The agent tried `python3 /workspace/.../calculator.py` and the allowlist blocked it. The agent then reported "Tester kunde inte köras" honestly instead of fabricating test output (compare: `marcus-udd-agent` at 18:23 claimed it ran the file and "all functions work as expected" — that was likely hallucinated).
4. **Empty-acknowledgment suppression fired** (18:15 `[suppress] empty acknowledgment`) — the dedup heuristic in `group_chat` is doing its job for noisy continuations.
5. **Operator `:say` worked end-to-end** (18:24, 18:25, 18:26) — the operator console route to peers stayed out of the LLM and through the scrubber.
6. **Tool sequence on the calc extension (18:22) was reasonable**: `read_file` → `replace_text` (remove stub comment) → `append_text` (add subtract/multiply/divide + `__main__` block with divide-by-zero handling). Final code emitted in chat is correct and complete.

---

## What went badly

### 1. Triple-send of the Planner spec (18:14)

The agent emitted the same Planner response **three times in a row** (`[hub->] emil-hjaertfors-agent: Bekräftat, jag tar: Produktplaneraren...` × 3), each a fresh `step=1` LLM call:

- step=1 prompt=3335t out=267t
- step=1 prompt=4021t out=267t  ← identical body
- step=1 prompt=4123t out=287t  ← also same body, then a tool call

That's ~821 wasted output tokens and three duplicate hub messages. Likely cause: each inbound message from another agent in the team prompt re-triggered a peer task, and the LLM kept re-asserting the Planner role from scratch instead of recognizing "I already did this". The reply gate has no "you already answered this thread" check beyond `COOLDOWN_SECONDS` (default 8s) — at 18:14 the gate may have been bypassed because the new inbound messages were direct mentions or broadcasts.

### 2. Replied to a message addressed to another agent (18:21)

User Emil wrote `@lullo-swe-agent, could you write the py-file that fulfills the specification from @emil-hjaertfors-agent`. The actual addressee is `@lullo-swe-agent`. But emil-agent **answered** ("I apologize, it seems the `calculator.py` file does not exist…").

Root cause is in `reply_policy._mentions` at `assignment2_part3/reply_policy.py:112-128`: the function returns `True` on *any* `@<own-name>` match, with no positional check. The phrase `"specification from @emil-hjaertfors-agent"` is a back-reference, not an address — but the policy can't tell. Multi-mention disambiguation isn't implemented.

This is fixable: the gate could prefer the **first** `@mention` as the addressee, or look for "@X, " / "@X please" patterns vs "from @X" / "by @X" back-references.

### 3. Path/project routing confusion (18:21–18:22)

The agent ran `read_file path=/workspace/calculator.py` and got back `Edit blocked: file does not exist: /workspace/emil_hjaertfors_bot/calculator.py`. Files actually live under `/workspace/emil_hjaertfors_bot/project21/...` (the `:project new` scope), so the agent's mental model of the path namespace is off by one or two levels. It eventually used the right absolute paths at 18:22, but the recovery cost a turn and an apologetic message to chat. The system prompt or coordinator hints aren't telling the agent its workspace root.

---

## Minor smells

- **No CLAIM/RELEASE traffic visible** for `calculator.py` even though three agents (`marcus-udd-agent`, `emil-hjaertfors-agent`, `josef-agent`) all touched it. The `ClaimRegistry` exists for exactly this case but nobody used it. Result: marcus and emil both rewrote calculator.py in overlapping turns at 18:22. Got lucky with the timing.
- **Tester step (Agent 3) and Refiner step (Agent 4)** of the original 4-agent workflow never happened in the chat we see — `josef-agent` produced tests only after the operator manually re-prompted it at 18:25. The role-sequencing in the meta-prompt was not enforced by any agent.
- The 18:22 final response ends with "Tester kunde inte köras på grund av att `python3` är blockerat" — accurate, but the agent didn't try `python` (which may be on the allowlist) as a fallback. Single-attempt fail.

---

## Suggested follow-ups (not implemented — review only)

If the user wants to act on this, the highest-leverage fixes are:

1. **`reply_policy._mentions`** (`assignment2_part3/reply_policy.py:112`): treat `@X` as an address only when it appears in addressee position (start of message, or before a comma/imperative), and ignore back-references like `from @X`, `by @X`, `the spec from @X`. Add a test in `tests/test_reply_policy.py`.
2. **Add a "this thread is already answered" guard** for the duplicate-Planner case. Track per-thread `last_outbound_text_hash` and skip if the LLM is about to emit a near-identical response within N seconds.
3. **Inject the project/workspace root into the system prompt** when `:project new` is active so the agent stops guessing paths.
4. **Add `python` to the allowlist** (or a tighter `python -m pytest`-style allowlist) so the agent can self-verify Python files instead of shipping unverified code.

## Verification

This file is a review document only. No code changes proposed here need verification beyond reading the cited file paths. If any of the suggested follow-ups are picked up, each needs its own plan with proper test coverage in `assignment2_part3/tests/`.
