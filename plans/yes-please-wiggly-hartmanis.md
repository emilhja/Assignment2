# Unified shared-workspace projects (local mode)

## Context

In a local docker-compose session the operator broadcast `build a calculator in /workspace/shared/calc/calculator.py — alice owns add/subtract, bob owns multiply/divide`. Both agents instead wrote to their own private dirs (`workspace/<AGENT_ID>/project2/...`), skipped the agreement-on-signatures step, and never emitted `CLAIM` lines. Alice's tests then failed with `ModuleNotFoundError: No module named 'calc'`.

Three root causes:

1. `config/system_prompt.txt:32-33` actively tells agents *"the chat is the only reliable channel for sharing code with peers, even when /workspace/shared appears available."* That stanza is injected universally and defeats every operator instruction that names a shared path.
2. `code_share.process_shared_code` + the `projectN` allocator (`code_share.py:170-184, 280-315`) only run in `mode == "runpod"` and only ever allocate under each agent's *private* workspace (`workspace/<AGENT_ID>/projectN/`), so local-mode agents have no project structure at all.
3. There is no path from "operator named `/workspace/shared/calc/...`" to "the active project is `calc`" — `_ProjectState` (`group_chat.py:103-105`) is remote-only and number-named.

Local-hub `docker-compose.yml:45,107` already bind-mounts `./workspace:/workspace` for both agents, so `/workspace/shared/` is genuinely co-visible — the infrastructure is ready; the runtime and prompt aren't using it.

Intended outcome: in local mode, projects live at `/workspace/shared/<project-name>/`, where the name is parsed from the operator's path or a coordinator `PROJECT:` line (fallback: auto-incremented `projectN` under shared). Remote (RunPod) mode is unchanged because there is no shared filesystem on that hub. CLAIM/RELEASE keep working unmodified because claims are already path-string-keyed.

## Critical files

- `assignment2_part3/code_share.py`
- `assignment2_part3/group_chat.py`
- `assignment2_part3/coordination.py`
- `assignment2_part3/config/system_prompt.txt`
- `assignment2_part3/tests/test_code_share.py`
- `assignment2_part3/tests/test_group_chat.py`
- `assignment2_part3/tests/test_peer.py`
- `assignment2_part3/tests/test_coordination.py`

No changes to `docker-compose.yml`, `claims.py`, `peer_task.py`, `transport.py`, `tools/audit.py`, or `tools/chat.py`.

## Implementation

### 1. `code_share.py` — named-project allocator + auto-pytest gate

- Add `named_project_dir(root: Path, name: str) -> Path | None`. Sanitize name to alnum + `-_`, lowercase, trim. Refuse names containing `/`, `..`, or those that resolve outside `root` (verified via `.resolve().relative_to(root.resolve())`). Use idempotent `mkdir(parents=True, exist_ok=True)` so two agents can race safely.
- Wrap the existing `mkdir(exist_ok=False)` at `code_share.py:183` in a 5-attempt retry that re-reads `_existing_project_indices` and tries `max+1` again on `FileExistsError`. Keeps monotonic semantics under contention.
- Extend `process_shared_code` with `auto_pytest: bool = True` kwarg. When `False`, skip the `maybe_run_pytest` call and emit the saved-file guidance without the `Auto-pytest: ...` line. Existing remote call site passes nothing → behavior unchanged.

### 2. `group_chat.py` — mode-aware project lifecycle

- Extend `_ProjectState` (`group_chat.py:103-106`) with two fields: `root: Path | None = None`, `is_shared: bool = False`.
- Add `_project_root(mode: str, agent_workspace: Path) -> tuple[Path, bool]`:
  - `mode == "runpod"` → `(agent_workspace, False)`.
  - else → `(Path(os.environ.get("SHARED_WORKSPACE", "/workspace/shared")), True)`.
- Lift the startup project init out of the `if runpod:` block at `group_chat.py:402-440`:
  - Resolve `(root, is_shared)` via `_project_root`.
  - `root.mkdir(parents=True, exist_ok=True)` (pre-create the shared root; today it's never created).
  - In local mode, do **not** auto-allocate `project1` at startup. Leave `project_state.active = None` and rely on path/`PROJECT:` parsing in `_process_message` to set it lazily on the first qualifying inbound message. Operator can also force a fallback via `:project new`.
  - In remote mode, preserve today's behavior (`next_project_dir` on first boot, or wait for `:project use N`).
- Add `_local_workspace_guidance(agent_id: str, project_dir: Path) -> str` parallel to `_remote_workspace_guidance` (`:108-143`). Content:
  - Active project is `/workspace/shared/<name>/`; peers can read it.
  - Before any write under `/workspace/shared/`, post a `CLAIM /workspace/shared/<path>#<scope>: <reason>` final answer; do not auto-redirect to private.
  - Use `read_file` on the shared file before claiming an existing one (matches `peer_task` behavior).
  - Do not invent `projectN` paths — write to the project dir reported by the runtime.
- In `_run_task_for_message`, branch the guidance injection (`group_chat.py:611-616`): append `_remote_workspace_guidance(...)` when `runpod`, else `_local_workspace_guidance(...)`.
- In `_process_message` (`group_chat.py:665-700`), before the existing `process_shared_code` call:
  - In local mode, parse the inbound for a `PROJECT:` directive (see step 3), then for `SHARED_PATH_PATTERN`. If either yields a project name, set `project_state.active = named_project_dir(shared_root, name)` (and `project_state.is_shared = True`). Log `project_set_from_inbound name=<name>`.
  - If neither is present and `project_state.active is None`, leave it None and `return` early with a `skip` log (operator must broadcast a path or `:project new`).
- Drop the `if runpod and` gate on `process_shared_code` (`group_chat.py:690`). Always call when `project_state.active is not None`. Pass `auto_pytest=False` when `project_state.is_shared` is True, else `True`.
- Before saving peer code in shared mode, compute candidate file paths via `code_share.extract_code_blocks(message.text)` and check `claims.is_claimed_by_other(str(project_state.active / block.filename), self_id)` for each. If any conflict, skip the save call, log `code_save_skipped_claim_conflict path=<...>`, and inject runtime guidance "peer code arrived during their active claim; read_file the path they post, do not auto-overwrite". This closes the TOCTOU surface between save and any later read.
- Extend the `:project` handler (`group_chat.py:146-195`) so `:project use` accepts either a project name (local mode) or `N` (remote mode). Sanitize the name through the same allowlist used in `named_project_dir`. Add `:project list` rendering for shared-mode entries (no numeric prefix required).

### 3. `coordination.py` — project parsing + signature-agreement loosening

- Add `PROJECT_DIRECTIVE_PATTERN = re.compile(r"(?im)^\s*PROJECT:\s*([A-Za-z0-9_\-]+)\s*$")` and `parse_project_directive(text) -> str | None`.
- Add `project_name_from_shared_path(path: str) -> str | None`: strip `/workspace/shared/`, take the first non-empty segment.
- Loosen `SIGNATURE_AGREEMENT_PATTERN` (`coordination.py:28`) to also match `state agreement on (the) function signatures`, `propose signatures`, `confirm signatures`. Suggested regex: `\b(?:agree|agreement|state\s+agreement|propose|confirm)\s+(?:on\s+)?(?:the\s+)?(?:function\s+)?signatures?\b`.
- `assignment_guidance` (`coordination.py:186-228`) already constructs `claim_target = f"{plan.path}#{own.scope}"` from `SHARED_PATH_PATTERN`. No change there — once the local-mode `_process_message` sets the project, the CLAIM target the LLM is told to emit becomes `/workspace/shared/calc/calculator.py#multiply-divide` automatically.

### 4. `config/system_prompt.txt` — remove the universal anti-shared stanzas

- Delete the second sentence at lines 32-33 (`Other agents cannot read files in your private workspace ... assume the chat is the only reliable channel for sharing code with peers, even when /workspace/shared appears available.`). Keep the 4096-char split rule.
- In the P3.8 stanza (lines 47-50), reword to neutral: `Your private workspace lives under /workspace/{AGENT_ID}. The shared workspace lives at /workspace/shared. Use the path the runtime tells you; do not invent a private path when the operator named a shared one.` Drop the "Always write joint output there using the explicit path" line since the runtime guidance now states the active project explicitly per mode.
- The mode-specific contradictions previously baked into the prompt now live in `_remote_workspace_guidance` (already in place) and the new `_local_workspace_guidance`.

### 5. Tests

**Update:**

- `tests/test_code_share.py:147-181` — keep existing allocator tests, add cases pinned to a shared-style root.
- `tests/test_code_share.py:269-308` — change asserted agent-facing path strings when caller passes a shared root.
- `tests/test_peer.py:78-94` — keep the `<self>` scrubber test (private paths still get redacted on the wire); add a sibling assertion that `/workspace/shared/calc/...` is left untouched.
- `tests/test_group_chat.py:36-42` — assert `_local_workspace_guidance` injection in local mode; keep the existing remote assertion guarded by mode.
- `tests/test_group_chat.py:665-723` — the `<self>` assertion (line 716) still holds because the test stays in local-mode-with-private-path edge cases; verify after refactor that the scrubber still fires for private paths agents might emit by mistake.

**New:**

- `test_named_project_dir_creates_and_is_idempotent`: second call returns the same dir; rejects `..`, `/abs`, `name/sub`.
- `test_named_project_inferred_from_broadcast`: feeding `_process_message` an inbound with `/workspace/shared/calc/calculator.py` sets `project_state.active.name == "calc"`.
- `test_project_directive_overrides_path`: `PROJECT: foo` in the same message wins over `/workspace/shared/calc/`.
- `test_next_project_dir_retries_on_race`: pre-create `project1`; monkeypatch `mkdir(exist_ok=False)` to raise `FileExistsError` once; assert retry lands on `project2`.
- `test_process_shared_code_auto_pytest_false_skips_subprocess`: monkeypatch `maybe_run_pytest` to flag if called; assert it isn't when `auto_pytest=False`.
- `test_peer_claim_blocks_auto_save`: peer has active CLAIM on `calculator.py#multiply`; their next message includes a fenced code block named `calculator.py`; assert save is skipped and a `code_save_skipped_claim_conflict` log is emitted.
- `test_signature_agreement_pattern_widened`: each of `state agreement on signatures`, `propose signatures`, `confirm signatures` triggers `_signature_agreement_guidance`.

## Verification

1. **Unit tests** from `assignment2_part3/`:
   ```
   python -m pytest tests/test_code_share.py tests/test_group_chat.py \
                    tests/test_peer.py tests/test_coordination.py -q
   ```
2. **End-to-end repro of the original failing session** (the chat in the user's review):
   - `docker compose -f assignment2_part3/docker-compose.yml build agent`
   - `docker compose -f assignment2_part3/docker-compose.yml up -d`
   - `python tools/chat.py say --as emil-user "@bob-swe @alice-swe build a calculator in /workspace/shared/calc/calculator.py. First, state agreement on signatures: add(a,b), subtract(a,b), multiply(a,b), divide(a,b). Then split: alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM. Write pytest tests next to the source."`
   - `python tools/chat.py live --as emil-user` → `everyone continue`
   - **Expect**: both agents emit `CLAIM /workspace/shared/calc/calculator.py#add-subtract` and `#multiply-divide`, write under `/workspace/shared/calc/`, then `RELEASE`. After the run, `workspace/shared/calc/calculator.py` and `workspace/shared/calc/test_calculator.py` exist on the host; `workspace/alice-swe/` and `workspace/bob-swe/` contain no `projectN/calculator.py`.
3. **Race verification**: re-run the same broadcast 3× back-to-back; confirm no `FileExistsError` traceback in either agent's logs.
4. **Auto-pytest gate**: send a chat message containing a fenced `calculator.py` code block while alice holds an active CLAIM on it; verify the receiving agent logs `code_save_skipped_claim_conflict` and the file on disk is unchanged.
5. **Remote regression**: set `AGENT_MODE=runpod` against the local stub transport (or actual RunPod), rerun the calculator scenario; verify behavior matches today's transcripts (files under `workspace/<agent>/projectN/`, no CLAIMs).
6. **Both legacy suites still green**:
   ```
   python -m pytest assignment2_part2 -q
   python -m pytest assignment2_part3/tests -q
   ```

---

# Phase 2 — PROJECT-directive auto-allocate + system-prompt cleanup

## Context

Two follow-ups emerged after the unified-workspace work landed:

1. **Reconnect friction (Q1).** Reconnecting agents on an existing local-hub session, the operator broadcasts a task and gets `[skip] no active project — :project new or :project use N`. After `:project new` the agents are silent because the skipped inbound is not replayed — the operator must broadcast `everyone continue` to retrigger. Observed in the user's 09:16 session.

2. **System prompt still owns folder + protocol content (Q2).** `config/system_prompt.txt` lines 47-50 (P3.8 Workspace layout) and 79-98 (P3.9 Claim/defer + tie-break) duplicate content that now lives in `_remote_workspace_guidance` and `_local_workspace_guidance`. The Phase 1 work already had to fix one drift (the anti-shared stanza). Confirmed scope: pull P3.8 *and* P3.9 fully out, so the prompt becomes mode-agnostic and the two runtime guidance helpers are the sole source of truth for paths and the claim protocol.

3. **Bonus (operator note).** When the operator runs `docker attach` *after* an inbound was skipped, they should immediately see what to do — a richer "no active project — pick one" prompt that also names existing projects + the dropped msg_id.

Intended outcome:
- A `PROJECT: <name>` directive in any inbound auto-allocates a project (in local: `named_project_dir`; in remote: `next_project_dir`). No `:project new`, no `everyone continue`. Plain @-mentions, broadcasts, and path-only mentions still fall through to the existing skip → preserves the reconnect-safety brake against stale broadcasts (per user's Q1 answer: only `PROJECT:` triggers auto-allocate).
- System prompt has zero `/workspace/...` paths and zero `CLAIM`/`RELEASE`/`DEFER` content. The full P3.9 contract lives in `_local_workspace_guidance`; `_remote_workspace_guidance` keeps its existing "do not emit CLAIM here" disclaimer.
- The skip print becomes a multi-line `[project?]` block listing existing projects so a late `docker attach` self-explains.

## Critical files

- `assignment2_part3/group_chat.py` — `_process_message` (PROJECT-directive auto-allocate, richer skip print); `_remote_workspace_guidance` (small reinforcement); `_local_workspace_guidance` (absorb full P3.9 + tie-break contract).
- `assignment2_part3/config/system_prompt.txt` — delete lines 47-50, 79-98; rewrite line 74's path example.
- `assignment2_part3/tests/test_group_chat.py` — new auto-allocate + skip-print + guidance-content tests; sweep prompt-string assertions.
- `assignment2_part3/tests/test_peer.py`, `tests/test_coordination.py` — re-check any prompt-text assertions, drop dead ones.

No changes to `coordination.py` (`parse_project_directive` already exposed), `code_share.py`, `claims.py`, `transport.py`, `docker-compose.yml`.

## Implementation

### 1. `group_chat.py` — `PROJECT:`-driven auto-allocate

In `_process_message`, before the existing `if runpod and project_state.active is None: return ...` guard:

```python
# Auto-allocate only on explicit operator intent. A `PROJECT: <name>`
# directive is treated as an opt-in; plain @-mentions or path-only mentions
# still fall through to the skip path. This preserves the reconnect-safety
# brake against stale broadcasts while removing the `:project new` + `everyone
# continue` two-step for the common case.
if project_state.active is None and project_state.root is not None:
    directive_name = parse_project_directive(message.text or "")
    if directive_name:
        if project_state.is_shared:
            new_dir = named_project_dir(project_state.root, directive_name)
        else:
            new_dir = next_project_dir(project_state.root)
        if new_dir is not None:
            project_state.active = new_dir
            _log(
                store,
                "project_auto_allocated",
                f"name={new_dir.name} reason=directive directive_name={directive_name} msg_id={message.id}",
            )
            print(
                colors.dim(
                    f"[project] auto-allocated active={new_dir.name} from PROJECT: {directive_name}"
                ),
                flush=True,
            )
```

The existing local-mode lazy-inference block (which also reads `parse_project_directive`) becomes a no-op when active is already set; its current contract already permits that. No reordering of the rest of `_process_message`.

### 2. `group_chat.py` — richer skip print for `docker attach` reconnect

Replace the current single-line skip print inside the `if runpod and project_state.active is None:` block with a multi-line `[project?]` block:

```python
existing = []
if project_state.root and project_state.root.exists():
    existing = sorted(
        (e.name for e in project_state.root.iterdir()
         if e.is_dir() and re.fullmatch(r"project\d+", e.name)),
        key=lambda n: int(n[len("project"):]),
    )
print(
    colors.dim(
        f"[project?] no active project — inbound msg {message.id} from "
        f"{message.sender_id} skipped.\n"
        f"[project?] existing: {', '.join(existing) if existing else '(none)'}\n"
        f"[project?] type `:project new` for a fresh project, "
        f"`:project use <N>` to reconnect, or include `PROJECT: <name>` "
        "in the next broadcast to auto-start."
    ),
    flush=True,
)
```

Also call this same renderer once during startup when `runpod` mode finds existing projects (currently `:512-519`) so the operator sees the same prompt whether they attach before or after the first inbound.

### 3. `_local_workspace_guidance` — absorb the full P3.9 contract

Today the helper has the CLAIM/RELEASE one-paragraph. Extend it to carry the full protocol that lives in the prompt today:

- JSON envelope reminder: protocol lines must be wrapped in `{"type":"final","answer":"CLAIM ..."}`.
- Scoped vs whole-file conflict rules (scoped claims can run in parallel; whole-file claims conflict with every scope).
- DEFER mechanics for same-path-same-scope races.
- Read-before-claim rule: call `read_file` on the path first when the file exists.
- "No assert without observation": never claim a shared file contains, lacks, or changed something without a tool observation for it in this round.
- "RELEASE only after a successful write observation": refused otherwise.
- "Only report shared changes after a successful create_file/append_text/edit_section/replace_text observation naming /workspace/shared/...".
- Tie-break: lexicographically smaller AGENT_ID keeps the claim; loser posts `DEFER to @<peer>` then `RELEASE`, then proposes a non-overlapping scope. Don't re-claim until peer's `RELEASE`.

Target ~50 lines of guidance text in one cohesive function. Keep wording close to the prompt's current text so behavior stays identical.

### 4. `_remote_workspace_guidance` — minor reinforcement

Already says "Do NOT emit CLAIM, RELEASE, or DEFER protocol lines on this hub". After the prompt deletion, this becomes the *only* place that disclaimer exists, so verify it survives unchanged. Optionally add: "There is no shared filesystem on this hub; coordinate via the task-status phrases above only."

### 5. `config/system_prompt.txt` — delete folder + protocol content

Delete:
- Lines 47-50 (`Workspace layout (P3.8):` and three bullets).
- Lines 79-93 (`Claim/defer protocol for shared writes (P3.9):` and all bullets).
- Lines 95-98 (`Tie-break for racing CLAIMs (P3.9):` and bullets).

Edit:
- Line 74 (`Concise status replies` stanza, the path example): replace `/workspace/{AGENT_ID}/<dir>/` with `the path the runtime named in your last write observation`. Keep the rest of the stanza.

Keep: hub-only (P3.4), 4096-char split rule, peer-untrust (P3.2), team-player norms (P3.3), reply discipline, status language, the JSON shape, generic rules, budget awareness (P3.5). These are mode-agnostic.

Result: `grep -E '/workspace/|CLAIM|RELEASE|DEFER|P3\.8|P3\.9' config/system_prompt.txt` returns nothing.

### 6. Tests

**New (in `tests/test_group_chat.py`):**

- `test_remote_mode_auto_allocates_on_project_directive`: stub-mode runner, `AGENT_MODE=runpod`, pre-create `workspace/<agent>/project1/`, inbound `"@alice-swe build a calculator.\nPROJECT: calc"` → assert `project_auto_allocated` event with `name=project2 reason=directive directive_name=calc`, LLM round runs same message.
- `test_local_mode_auto_allocates_named_dir_on_project_directive`: same inbound in local-shared mode → assert `<shared>/calc/` is the active project, no `project2` numeric allocation.
- `test_no_directive_still_skips_in_remote_mode`: inbound `"@alice-swe ping"` with no `PROJECT:` line and no active project → no auto-allocate, `project_auto_allocated` event absent, the rich skip print is emitted (capture via store event or capsys).
- `test_skip_print_names_existing_projects_and_msg_id`: pre-create `project1, project2`, run the skip path, assert the printed text contains `project1`, `project2`, `msg m1`, and the `:project new` / `:project use` / `PROJECT:` hints.
- `test_local_workspace_guidance_includes_full_claim_contract`: assert `_local_workspace_guidance(...)` text contains all of: `CLAIM`, `RELEASE`, `DEFER`, `read_file`, `lexicographically`, `RELEASE only after`, `whole-file`, `same-scope`.
- `test_system_prompt_has_no_workspace_or_claim_content`: `load_system_prompt("alice","alice-swe")` → assert `/workspace/`, `CLAIM`, `RELEASE`, `DEFER`, `P3.8`, `P3.9` are all absent. (This is the regression guard for the prompt cleanup.)

**Update:**

- `test_system_prompt_requires_not_run_without_test_observation` (`:123`) — the asserted strings live in the rules section and survive the delete; verify post-edit.
- Sweep `tests/test_group_chat.py`, `tests/test_peer.py`, `tests/test_coordination.py` for prompt assertions naming `P3.8`, `P3.9`, `Workspace layout`, `Claim/defer protocol`, `/workspace/shared/<path>#<scope>`, `Tie-break for racing CLAIMs`. Drop or relocate each to its corresponding guidance helper.

## Verification

1. **Unit tests:**
   ```
   python -m pytest assignment2_part3/tests -q
   python -m pytest assignment2_part2 -q
   ```

2. **End-to-end repro of the reported friction (remote mode via local stub hub):**
   - `docker compose --profile local up -d --build`
   - `docker attach assignment2_part3-agent-alice-1` (and bob) — expect `[project?]` block listing existing projects with the `:project new` / `:project use` / `PROJECT:` hints.
   - `python tools/chat.py live --as emil-user`, then send:
     `"@bob-swe @alice-swe build a calculator. PROJECT: calc4. Sigs: add(a,b), subtract(a,b), multiply(a,b), divide(a,b). alice owns add/subtract, bob owns multiply/divide. Write pytest."`
   - **Expect:** both agents log `project_auto_allocated reason=directive directive_name=calc4`, allocate the next free project, run the round immediately. No `:project new`, no `everyone continue`.

3. **Reconnect-brake regression:** restart agents, broadcast `"everyone good morning"` (no `PROJECT:`). Expect the rich `[project?]` skip block on every skipped inbound; nothing runs until either `:project new` is typed or the next broadcast carries `PROJECT: <name>`.

4. **Prompt-cleanup smoke test:**
   ```
   python -c "from group_chat import load_system_prompt; t = load_system_prompt('alice','alice-swe'); assert '/workspace/' not in t and 'CLAIM' not in t and 'DEFER' not in t and 'P3.8' not in t and 'P3.9' not in t, 'leftover content in prompt'"
   ```

5. **Local-shared-mode regression:** rerun Phase 1's calculator scenario (`/workspace/shared/calc/calculator.py`, two scoped CLAIMs, RELEASE, file lands under `workspace/shared/calc/`). The contract now reaches the LLM via `_local_workspace_guidance` instead of the prompt — behavior should be identical.

