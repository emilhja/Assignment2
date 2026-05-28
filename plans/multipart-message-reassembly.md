# Plan: reassemble multi-part inbound messages

## Context

The new `transport.send` auto-splitter emits oversized payloads as separate hub posts with `(part i/N)\n` headers (`assignment2_part3/transport.py:308`). LLMs sometimes do the same thing voluntarily — Josef's snake-game answer in the recent chat arrived as two messages, `#55 "(part 1/2)"` and `#56 "(part 2/2)"`. Today the receiver treats each part as an independent `PeerMessage`: each one goes through `should_reply` and may trigger its own `run_peer_task` round, so the agent reacts to half a payload, double-acks, or wastes budget answering "(part 1/2)" before "(part 2/2)" arrives.

Goal: collapse a `(part i/N)` sequence from one sender back into a single logical `PeerMessage` before the reply gate sees it. Single-message traffic (the common case) must be unaffected.

## Approach

New pure module `assignment2_part3/message_assembler.py` holding a `MultipartAssembler` class with one entry point:

```
feed(message: PeerMessage, now: float) -> list[PeerMessage]
```

Each call returns zero, one, or more `PeerMessage`s that are ready to deliver downstream:

- Message has no `(part i/N)` header → returned as-is (fast path, no buffering).
- Message has a header and is *not* the final part → buffered, return `[]`.
- Message is the last missing part → all parts joined and returned as one synthetic `PeerMessage`.
- A separate `flush_expired(now)` call is invoked from the idle path; orphan groups older than `MULTIPART_TIMEOUT_SECONDS` (default 60 s) are flushed with an `[incomplete multi-part message: missing parts X, Y]` prefix so the agent at least sees the partial content rather than silently losing it.

Wire it into `group_chat.run_group_chat` between `transport.recv` and `_process_message` at `assignment2_part3/group_chat.py:1432`:

```python
message = transport.recv(timeout=1.0)
if message is None:
    for stale in assembler.flush_expired(time.time()):
        _process_message(stale)
    time.sleep(idle_sleep)
    continue
for ready in assembler.feed(message, time.time()):
    _process_message(ready)
```

The same insertion is needed at the second `recv` site at `group_chat.py:1371` (the "wait for continuation" path) — flush only, no new buffering (we already have a peer message in hand and only want to keep order).

## Detection rules

Single regex, applied to the start of the inbound text:

```python
PART_HEADER = re.compile(r"^\s*\(part\s+(\d+)\s*/\s*(\d+)\)\s*\n?", re.IGNORECASE)
```

Strict — header must be at the start, parenthesised, in the form `(part i/N)`. This is what the new splitter emits and what Josef's `#55`/`#56` used, so both cases are covered. Free-form mid-text mentions like "see Part 3 below" are intentionally ignored to avoid false positives.

Sanity caps to keep a hostile peer from exhausting memory:

- `MAX_PARTS = 20` — header with `N > 20` is treated as a plain message (no buffering).
- `MAX_BUFFERED_GROUPS_PER_SENDER = 4` — older groups from the same sender flushed early when this is exceeded.
- `MULTIPART_TIMEOUT_SECONDS = 60`.

## Reassembled message shape

When a group is complete:

- `text` — bodies of each part with their headers stripped, joined with `"\n"`. Trailing whitespace on each part is preserved so code blocks stay intact across boundaries (the splitter cuts on `\n` boundaries, so concatenation reconstructs the original).
- `id` — the first part's `id`. Trace IDs already key on `inbound_message.id`, so keeping the first part's id makes `tools/audit.py trace <id>` show the reassembled turn coherently.
- `sender_id` — unchanged.
- `received_at` — timestamp of the *last* part (i.e. when the message actually became usable).
- `addressed_to` — unchanged (taken from the first part).

For incomplete flushes, prepend a single warning line:

```
[incomplete multi-part message from <sender>: received parts 1, 3 of 5]
<reassembled-so-far>
```

## State, per sender

Internal dict keyed by `(sender_id, total_parts)`. Each value:

```python
@dataclass
class _PendingGroup:
    sender_id: str
    total: int
    parts: dict[int, PeerMessage]    # part_index -> message
    first_seen_at: float
    last_seen_at: float
```

Indexed by `(sender, total)` rather than just `sender` so two concurrent splits from the same agent (rare but possible across project switches) don't collide.

## Edge cases / decisions

| Case | Behavior |
| --- | --- |
| Duplicate part index | Last one wins, log `multipart_duplicate` event. |
| Part index out of range (`i > N` or `i < 1`) | Treated as a regular message, no buffering (`multipart_invalid`). |
| Different `N` from the same sender for the same group | Different key → second sequence buffered independently. |
| `(part 1/1)` | Stripped of header and delivered immediately as a single message. |
| Sender sends a non-part message while a group is pending | Pending group is flushed as incomplete (we assume the sender abandoned it). |
| Module imported by tests without group_chat | The class has no I/O — pure logic, easy to unit-test. |

## Files

**New**

- `assignment2_part3/message_assembler.py` — class + constants + regex. Pure, no transport or store deps.
- `assignment2_part3/tests/test_message_assembler.py` — see test list below.

**Modified**

- `assignment2_part3/group_chat.py` — construct one `MultipartAssembler` near the top of `run_group_chat`, route both `transport.recv` sites through `feed`, call `flush_expired` from the idle branch. Search anchors: `transport.recv(timeout=1.0)` at line 1432, `transport.recv(timeout=min(remaining, 0.5))` at line 1371.
- `assignment2_part3/tests/test_group_chat.py` — one integration test that injects a two-part sequence through a stub transport and asserts `_process_message` is called once with the joined text.

**Reused (no change)**

- `peer.PeerMessage` — already a frozen dataclass with the fields we need.
- `transport.RunPodTransport` — unchanged; this is purely a receiver-side feature.
- `_split_for_hub` (`transport.py`) — the sender side that produces the `(part i/N)` headers.

## Test list

In `tests/test_message_assembler.py`:

1. `test_passthrough_for_non_part_message` — plain message returns `[message]`.
2. `test_buffers_first_part_returns_empty` — `(part 1/2)` returns `[]`, group registered.
3. `test_delivers_reassembled_on_last_part` — `(part 1/2)` then `(part 2/2)` returns the joined message; headers stripped.
4. `test_out_of_order_parts_assemble_correctly` — `(part 2/2)` arriving before `(part 1/2)` still produces ordered text.
5. `test_part_1_of_1_strips_header_and_delivers` — degenerate single-part case.
6. `test_separate_senders_do_not_interfere` — alice and bob each in mid-split, each completes independently.
7. `test_timeout_flushes_incomplete_with_marker` — `flush_expired` past the timeout emits a warning-prefixed partial.
8. `test_unrelated_message_from_same_sender_flushes_pending` — alice sends `(part 1/3)` then a normal message; pending group flushed as incomplete, normal message also delivered.
9. `test_duplicate_part_index_keeps_last` — `(part 1/2)` then `(part 1/2)` then `(part 2/2)` still completes; only the latest part-1 content is kept.
10. `test_invalid_index_or_out_of_range_treated_as_plain` — header `(part 5/2)` is *not* buffered.
11. `test_max_parts_cap_rejects_huge_n` — `(part 1/9999)` is treated as a plain message.
12. `test_assembled_message_uses_first_part_id_and_last_part_timestamp` — exact field-level check.

In `tests/test_group_chat.py`:

13. `test_group_chat_assembles_multipart_inbound_before_reply_gate` — feed two parts via a stub transport, assert `should_reply` runs exactly once with the joined text.

## Verification

1. `python -m pytest assignment2_part3/tests/test_message_assembler.py -q` — new module tests green.
2. `python -m pytest assignment2_part3/tests -q` — full suite stays at 370+ passing (no regressions in reply policy, peer_task, transport).
3. End-to-end with the local hub:
   ```bash
   cd assignment2_part3
   docker compose up -d
   docker attach assignment2_part3-agent-alice-1   # Ctrl-P Ctrl-Q to detach
   python tools/chat.py live --as emil-user
   # In the chat, paste a 8 KB code block addressed to alice.
   ```
   Expected: hub log shows `(part 1/2)` and `(part 2/2)` from the sender, but alice's `should_reply`/`run_peer_task` fires only once with the full payload visible in `tools/audit.py trace <id>`.
4. Backwards-compat smoke: send a normal short message → still arrives as one `PeerMessage`, no `multipart_*` events in the trace.

## Non-goals

- No change to the sender side (`transport.send` auto-split already works).
- No reassembly of LLM-formatted-but-not-headered splits like `**Part 1/2**` embedded mid-message. Only the strict `(part i/N)` header at the start of the message body is recognised; loosening that would mean false positives on conversational mentions of "part 2 of the design".
- No cross-session persistence. If the agent process dies mid-split, the partial buffer is lost — acceptable because the trace is still in the sender's SQLite log.
