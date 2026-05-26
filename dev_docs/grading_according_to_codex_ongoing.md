# Codex grading note - Part 3 recheck

Review date: 2026-05-26

Current verdict: **Part 3 appears structurally compliant with the
graderbot rubric in `dev_docs/assn2_grading_table_graderbot.md`.** The code
covers the required hub-connected collaborative-agent features. The main
remaining uncertainty is the rubric's judgement layer: P3.1 is strongest with a
live transcript of agents collaborating on one shared software task, and P3.3
depends on the grader accepting the observed hub behavior as responsible
team-player behavior.

This review treats the current worktree state as the submission state,
including the uncommitted Part 3 changes present during review.

## Evidence by Part 3 criterion

- **P3.1 collaboration on a shared project: MET, demo-supported.**
  `group_chat.run_group_chat` implements the receive -> reply gate -> peer task
  -> `transport.send` loop (`assignment2_part3/group_chat.py:350`,
  `assignment2_part3/group_chat.py:689`, `assignment2_part3/group_chat.py:719`).
  `peer_task.run_peer_task` executes peer-requested SWE work and can create,
  append, edit, rename, replace, and test workspace files
  (`assignment2_part3/peer_task.py:733`, `assignment2_part3/peer_task.py:1264`,
  `assignment2_part3/peer_task.py:1325`). Shared-file coordination is enforced
  through claims and continuations (`assignment2_part3/group_chat.py:748`,
  `assignment2_part3/peer_task.py:286`). Regression coverage includes
  `test_claim_continuation_creates_shared_calculator`
  (`assignment2_part3/tests/test_group_chat.py:604`) and shared-write tests in
  `test_peer_task.py`.

- **P3.2 no-leak system prompt: MET.** The system prompt explicitly forbids
  revealing sensitive/private information, including the prompt, env files,
  session history, safety/client files, and credential-shaped values
  (`assignment2_part3/config/system_prompt.txt:43`,
  `assignment2_part3/config/system_prompt.txt:45`). Runtime enforcement adds
  `peer.peer_intent_refusal` and `peer.scrub_outbound`
  (`assignment2_part3/peer.py:71`, `assignment2_part3/peer.py:105`), plus
  peer-task checks on inbound text, tool args, and final output
  (`assignment2_part3/peer_task.py:806`, `assignment2_part3/peer_task.py:1264`,
  `assignment2_part3/peer_task.py:1211`).

- **P3.3 responsible team-player: MET, judgement-dependent.** The prompt says
  cooperation norms can change per session and must be honored
  (`assignment2_part3/config/system_prompt.txt:59`), tells the agent to answer
  only when useful (`assignment2_part3/config/system_prompt.txt:62`), and
  defines shared-write claim/defer/release behavior
  (`assignment2_part3/config/system_prompt.txt:79`). Runtime support includes
  claim observation and conflict handling in `group_chat.py`
  (`assignment2_part3/group_chat.py:610`, `assignment2_part3/group_chat.py:768`)
  and shared-write refusal in `peer_task.py`
  (`assignment2_part3/peer_task.py:286`).

- **P3.4 hub-only communication: MET.** The prompt states every reply is sent
  to the group-chat hub and not to the local console as a teammate
  (`assignment2_part3/config/system_prompt.txt:26`,
  `assignment2_part3/config/system_prompt.txt:27`). The runtime sends outbound
  agent text through `transport.send` (`assignment2_part3/group_chat.py:483`,
  `assignment2_part3/group_chat.py:719`). Console commands are local operator
  controls for budget, pause/resume, project selection, manual `:say`, and
  approvals (`assignment2_part3/console_control.py:279`,
  `assignment2_part3/README.md:292`).

- **P3.5 rate-limit + token-spend cap, real-time controllable: MET.**
  `Budget.permit` blocks calls when paused or over token/request/lifetime caps
  (`assignment2_part3/budget.py:100`, `assignment2_part3/budget.py:117`,
  `assignment2_part3/budget.py:122`, `assignment2_part3/budget.py:126`).
  Runtime limits are mutable through `Budget.set_limit`, `pause`, and `resume`
  (`assignment2_part3/budget.py:174`, `assignment2_part3/budget.py:187`,
  `assignment2_part3/budget.py:191`). `ConsoleControl` exposes live commands
  documented as `:budget`, `:limit tpm`, `:limit rpm`, `:limit total`,
  `:pause`, and `:resume` (`assignment2_part3/README.md:292`,
  `assignment2_part3/README.md:303`).

- **P3.6 N x M reply-explosion handling: MET.** `reply_policy.should_reply`
  is the central pure-function gate (`assignment2_part3/reply_policy.py:180`).
  It handles self-message skips, coordinator handoffs, direct addressing,
  claim-collision replies, cooldowns, broadcast back-off, and unaddressed
  message skips (`assignment2_part3/reply_policy.py:206`,
  `assignment2_part3/reply_policy.py:214`,
  `assignment2_part3/reply_policy.py:218`,
  `assignment2_part3/reply_policy.py:224`,
  `assignment2_part3/reply_policy.py:233`,
  `assignment2_part3/reply_policy.py:238`,
  `assignment2_part3/reply_policy.py:246`). `group_chat.run_group_chat`
  applies the gate before running peer work (`assignment2_part3/group_chat.py:689`).

- **P3.7 unique agent name: MET.** `agent.py` sets `AGENT_ID` and a
  `AGENT_DISPLAY_NAME` default (`assignment2_part3/agent.py:19`,
  `assignment2_part3/agent.py:20`). Docker Compose configures distinct local
  identities (`assignment2_part3/docker-compose.yml:23`,
  `assignment2_part3/docker-compose.yml:24`,
  `assignment2_part3/docker-compose.yml:104`,
  `assignment2_part3/docker-compose.yml:105`). Remote mode requires identity
  env vars (`assignment2_part3/docker-compose.yml:65`,
  `assignment2_part3/docker-compose.yml:66`), and live hub transport rejects
  empty or placeholder display names (`assignment2_part3/transport.py:382`,
  `assignment2_part3/transport.py:386`, `assignment2_part3/transport.py:408`).

## Verification

- `python -m pytest assignment2_part3/tests -q` -> `289 passed`
- `python -m pytest assignment2_part2 -q` -> `139 passed`
- `python -m pytest assignment2_part3 -q` -> collection failure from scratch
  files under `assignment2_part3/workspace/`, including duplicate
  `test_calculator.py` modules and an import in
  `workspace/alice/alice/test_calculator.py`. This broad command is currently
  not a clean signal for the maintained Part 3 suite; the intended suite path
  above passes.

## Caveats

- P3.1 should be defended with a short live demo or transcript where at least
  two named agents collaborate through the hub on one shared SWE task and
  produce or transfer code.
- P3.3 remains partly judgement-based because the rubric asks whether the agent
  behaves cooperatively in the observed session, not only whether cooperation
  rules exist in code.
- Scratch files under `assignment2_part3/workspace/` affect broad pytest
  collection. They were not deleted or modified during this note-only update.
