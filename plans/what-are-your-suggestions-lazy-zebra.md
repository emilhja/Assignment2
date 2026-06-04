# Fix: agent shares fabricated / incomplete file contents on "share the files" requests

## Context

In the `project71` calculator session the `emil-hjaertfors-agent` was asked four
times to "share the contents of the files" and never cleanly produced all four
files. Root causes (traced through `peer_task.run_peer_task`):

1. **No cross-turn memory.** Each peer turn rebuilds history from scratch
   (`peer_task.py:987-991`); the contents the agent created earlier are gone, so
   "share" can only be satisfied by re-reading from disk *this* turn — but
   nothing forces a read.
2. **Reply is the LLM re-typing, not raw tool output.** After `read_file` the
   model is called again and retypes the content into its `final` — the point
   where it drops files, summarizes, or fabricates (weak backend:
   `gemini-2.5-flash`).
3. **No enumerate-then-read.** The agent read only the two files it "remembered"
   and forgot the two CSS files. Its own 13:17 message listing all four files
   fell outside the `recent_context` window (`MAX_CONTEXT_CHARS = 2000`,
   `peer_task.py:180-182`).
4. **Hallucination ships straight to the hub (worst).** At 13:21 the first CSS
   reply was `step=1`, no `read_file`, fabricated CSS that did not match the real
   file. The stall guards only fire on intro/vague finals
   (`_looks_like_non_action_final`, `peer_task.py:813-845`); a confident fake
   code block is none of those, so it passes through. **There is no guard
   requiring a `read_file` this round when the user asked to share file
   contents.** That is the gap.

Intended outcome: when asked to share/show file contents, the agent must
actually read the file(s) this round before pasting them — eliminating the
fabricated-content path — and is nudged to enumerate and read *all* requested
files, not just the ones it recalls.

## Changes — all in `assignment2_part3/peer_task.py`

### 1. Hard gate: pasting file contents requires a read this round (primary fix)

New module-level helpers (near `_action_was_requested`, `peer_task.py:497`):

- `_SHARE_REQUEST_RE` / `_share_was_requested(text)` — narrower than
  `_ACTION_REQUEST_RE`: matches share/show/paste/display/print/post/send +
  file|content|code|`.js`/`.css`/`.py` references, plus "contents of" and
  Swedish `dela`/`visa`/`klistra`/`innehåll`. Purpose: detect "share the file
  contents" without matching generic implement/fix verbs.
- `_answer_pastes_file_contents(answer)` — True when the answer contains a
  fenced code block (```` ``` ````) or a `# file:` marker.

Track a new flag alongside the existing `saw_*` flags (`peer_task.py:993-997`):
`saw_successful_read`, set `True` in the `tool_call` branch when
`parsed.tool == "read_file"` and the observation does **not** start with
`Edit blocked:` (set it next to the `run_tests` tracking at
`peer_task.py:1542-1547`).

New guard in the `final` branch, placed with the other reprompt guards (before
the scrub/return at `peer_task.py:1420`):

```python
if (
    not _is_claim_continuation(message)
    and _share_was_requested(message.text)
    and _answer_pastes_file_contents(answer)
    and not saw_successful_read
    and not saw_any_successful_write   # write-then-paste flow stays legitimate
):
    guidance = (
        "You pasted file contents but did not read any file this round. Do not "
        "reconstruct file contents from memory — they are not reliable across "
        "turns. Call read_file on the exact workspace path for each file you "
        "intend to share, then paste each file from the read_file observation in "
        "its own fenced block with `# file: <path>` as the first line. If you are "
        "unsure which files exist, run `ls -R` on the project directory first."
    )
    stopped = _continuation_reprompt_or_stop(
        "share_requires_read_reprompt",
        guidance,
        "I had to stop because I kept pasting file contents without reading the files first.",
    )
    if stopped is not None:
        return stopped
    continue
```

Reuses the existing `_continuation_reprompt_or_stop` machinery
(`peer_task.py:1000-1031`, capped at `MAX_CONTINUATION_REPROMPTS_PER_REASON`),
so it self-limits and falls back to silence under `SUPPRESS_STALL_REPLIES`.

The `and not saw_any_successful_write` clause is important: the existing
`user_action_no_write` guidance already instructs agents to paste contents
*after* a successful write (`peer_task.py:1386-1388`); that legitimate
write-then-paste must not be blocked.

**Known limitation (acceptable):** the gate requires *a* read, not a read of
every pasted file. It fully closes the zero-read hallucination case (what
happened at 13:21); per-file matching is deliberately out of scope.

### 2. Up-front enumerate-and-read nudge (secondary)

When `_share_was_requested(message.text)`, inject one runtime-guidance message
before the loop, reusing `_runtime_guidance_message` and the existing injection
block (`peer_task.py:1047-1051`):

> "To share file contents: list the project directory first if you are unsure
> which files exist (`ls -R`), then call read_file on EACH file and paste every
> one in its own fenced block (`# file: <path>` first line). Share all requested
> files, not only the ones you remember. Never reconstruct contents from memory."

This addresses cause 3 (only-2-of-4) by steering toward enumeration instead of
relying on the truncated transcript — so no change to `MAX_CONTEXT_CHARS` is
needed.

## Out of scope (noted, not changed)

- **Path display mismatch** (`_display_workspace_path` echoes
  `/workspace/project71/...` while `AGENT_WORKSPACE=/workspace/emil_hjaertfors_bot`,
  `tools.py:122-142`). A real confound but both forms resolve correctly;
  changing the display format risks Part 2 tests and audit tooling. Leave as a
  separate follow-up.
- **bash `cat` as a read signal** — only `read_file` success sets
  `saw_successful_read`. The system prompt steers to `read_file`, so this is an
  acceptable minor gap.

## Tests — add to `assignment2_part3/tests/test_peer_task.py`

Follow the existing scripted-`chat_fn` pattern (see
`test_create_file_exists_guidance_tells_agent_to_read_existing_file`,
`peer_task.py` tests at lines 916 and 1123). Use `monkeypatch` to point
`AGENT_WORKSPACE` at `tmp_path` and create the real file so `read_file`
succeeds.

1. `test_share_request_without_read_is_reprompted_then_reads` — message
   "share the contents of App.css"; scripted responses: (a) `final` with a
   ```` ```css ```` fence and no read, (b) `tool_call` `read_file`, (c) `final`
   with the real fenced content. Assert the returned answer is the read-backed
   one and that a `share_requires_read_reprompt` event was logged (via
   `_events(store)`).
2. `test_share_request_after_read_passes` — (a) `read_file`, (b) `final` with
   fence → returned directly, no reprompt event.
3. `test_share_guidance_injected_for_file_share_request` — assert the up-front
   enumerate/read guidance appears in the messages passed to `chat_fn`.
4. `test_write_then_paste_not_gated_as_share` — successful `create_file`
   followed by a `final` that pastes the contents is returned unchanged (guards
   against regressing the `user_action_no_write` paste flow).

## Verification

```bash
python -m pytest assignment2_part3/tests/test_peer_task.py -q
python -m pytest assignment2_part3/tests -q        # full Part 3 suite
python -m pytest assignment2_part2 -q              # Part 3 edits often regress Part 2
```

Manual (optional, 4-terminal hub per CLAUDE.md): start a project, create a few
files, then from the chat CLI send "@<agent> share the contents of the files"
and confirm (a) every file is pasted, each from a `read_file` observation, and
(b) `python tools/audit.py tail --agent <id> --kind tool` shows a `read_file`
for each shared file with no fabricated content.
