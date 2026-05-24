# Codex grading note - Part 3 recheck

Current verdict: **Part 3 still appears valid against the rubric.** The code
continues to cover the required hub-connected collaborative-agent features. The
remaining uncertainty is mostly demo/oral-judgement dependent: P3.1 depends on
showing meaningful collaboration on a shared project, and P3.3 depends on the
grader accepting the documented cooperation norms as responsible team-player
behaviour in the live session.

## Evidence

- **P3.2 no-leak system prompt / peer hardening:** the Part 3 prompt includes a
  peer-message untrust envelope (`assignment2_part3/config/system_prompt.txt:30`);
  `peer.peer_intent_refusal` and `peer.scrub_outbound` enforce refusal/redaction
  paths (`assignment2_part3/peer.py:71`, `assignment2_part3/peer.py:105`); and
  `peer_task.run_peer_task` re-checks peer input/tool args and scrubs final
  output (`assignment2_part3/peer_task.py:112`, `assignment2_part3/peer_task.py:141`).
- **P3.3 responsible team-player:** cooperation rules are explicitly documented
  in the prompt (`assignment2_part3/config/system_prompt.txt:37`) and README
  (`assignment2_part3/README.md:17`). This remains partly judgement-based in a
  demo because the rubric asks whether the agent behaves as a good collaborator.
- **P3.4 hub-only communication:** `group_chat.run_group_chat` follows the
  receive -> reply gate -> task -> `transport.send` loop
  (`assignment2_part3/group_chat.py:8`, `assignment2_part3/group_chat.py:150`);
  the README states outbound text goes through `transport.Transport.send`
  (`assignment2_part3/README.md:18`).
- **P3.5 rate limit + token-spend cap with real-time control:** `Budget.permit`
  blocks paused or over-cap requests (`assignment2_part3/budget.py:90`,
  `assignment2_part3/budget.py:94`, `assignment2_part3/budget.py:99`,
  `assignment2_part3/budget.py:103`); `ConsoleControl` exposes live `:limit`,
  `:budget`, `:pause`, and `:resume` commands
  (`assignment2_part3/console_control.py:12`, `assignment2_part3/README.md:19`).
- **P3.6 N x M reply-explosion handling:** `reply_policy.should_reply` is the
  central pure-function gate (`assignment2_part3/reply_policy.py:82`), and
  `group_chat.run_group_chat` applies it before any peer task is run
  (`assignment2_part3/group_chat.py:129`).
- **P3.7 unique agent name:** `agent.py` sets `AGENT_ID` and
  `AGENT_DISPLAY_NAME` defaults (`assignment2_part3/agent.py:15`,
  `assignment2_part3/agent.py:16`); Docker Compose gives the included agents
  distinct names (`assignment2_part3/docker-compose.yml:9`,
  `assignment2_part3/docker-compose.yml:36`); and the transport validates that a
  live hub display name is non-empty and not a placeholder
  (`assignment2_part3/transport.py:359`, `assignment2_part3/transport.py:363`).

## Verification Recorded From Latest Check

- `python -m pytest assignment2_part3 -q` -> `87 passed`
- `python -m pytest assignment2_part2 -q` -> `95 passed`

No code tests were re-run for this note-only update.

## Caveats

- P3.1 appears structurally supported by `peer_task.run_peer_task`,
  per-agent workspaces, and `group_chat.run_group_chat`, but final credit is
  best defended by a live demo of agents collaborating on one shared task.
- P3.3 likewise has prompt/README support, but the final call depends on the
  grader's judgement of the observed behaviour.
- README/demo test-count comments are stale: `assignment2_part3/README.md:149`
  still says the Part 3 suite has 59 tests, and `assignment2_part3/demo.md:294`
  still says 76 tests. Those comments should be refreshed later, but they do
  not invalidate Part 3 itself.
