# Part 3 Docker security assessment

_Date: 2026-06-04 — scope: can the agent (incl. an adversarial peer over the hub) reach the host hard drive?_

## Verdict: safe in Docker — the bash surface cannot reach your disk

The bash tool is locked down hard, and the only host filesystem it can ever see is
the two folders deliberately mounted (`./workspace`, `./data`).

## What stops bash from reaching the drive

Five independent layers, each of which alone would block a filesystem escape:

1. **Default-deny allowlist** (`assignment2_part2/safety.py:8`) — bash can run *only* these
   14 read-only commands: `ls cat grep head tail wc find pwd echo printf sort uniq cut
   true false`. No `rm`, `mv`, `cp`, `ln`, `curl`, `python`, `sh`. Anything else is blocked
   at the first token of every `;|&` segment.
2. **No write / no execution primitives** — redirection (`>`), command substitution
   (`$(…)`, backticks), process substitution (`<()`), and `bash -c`/`sh -c` wrappers are all
   blocked (`safety.py:122-135`). Allowed commands can't be chained into a write or a
   downloaded script.
3. **Path confinement on read commands** (`safety.py:185-200`) — `cat`/`grep`/`find`/etc.
   reject `..`, reject wildcards, and reject any absolute path not under `/workspace`.
   `cat /etc/passwd` → blocked. `cat ../../.env` → blocked.
4. **Writes never go through bash** — file creation/edit uses structured tools
   (`create_file`, `edit_section`, …), each routed through `_resolve_workspace_path`
   (`assignment2_part2/tools.py:56-119`), which `.resolve()`s the path and requires
   `.relative_to(workspace_root)`. Symlink tricks don't help — `ln` isn't on the allowlist,
   so the agent can't plant a symlink to follow.
5. **Container hardening** (`docker-compose.yml:47-53`) — `cap_drop: ALL`,
   `no-new-privileges:true`, non-root `agentuser`, `pids_limit: 100`, `mem_limit: 512m`.
   The bash subprocess also gets a stripped env with no API keys (`tools.py:145-167`), so
   `echo $GROQ_API_KEY` finds nothing even if it slipped past the regex.

## The one real exposure (by design)

```yaml
volumes:
  - ./workspace:/workspace
  - ./data:/data
```

These two host folders **are** the hard drive, live. Everything the agent legitimately
writes lands in `assignment2_part3/workspace/` and `assignment2_part3/data/`. Contained
and intended — nothing else on the drive is mounted. The container sees a *copy* of the
source (`COPY . .` in the Dockerfile), not the real repo, so the agent cannot modify the
actual source tree, git history, or `.env`.

## Residual gaps

| Risk | Severity | Note |
|---|---|---|
| **Running bash locally, not in Docker** | Medium | `run_bash` also works outside the container (`tools.py:187`). There the allowlist + path check are the *only* boundary — no cap_drop, no mount isolation. Reads stay workspace-confined, but defense-in-depth is lost. The Docker path is the safe one. |
| **No network isolation** | Low | The agent container has network access (needs hub + LLM). bash can't use it (`curl`/`wget` not allowed), but the Python process itself talks out. |
| **No `read_only` root filesystem** | Low | `/app` and `/tmp` are writable in-container. Can't reach the host, writes vanish on container removal. A `read_only: true` + tmpfs would tighten it. |
| **`find` is allowed** | Low | Confined: `find /` blocked (`safety.py:126`), `-exec`/`-delete`/`-ok` blocked. Bare `find` defaults to the workspace cwd. |

## Bottom line

In the intended Docker setup, an agent — even an adversarial peer over the hub — cannot
read or write outside `./workspace` and `./data`, and cannot run any destructive or
exfiltration command via bash. The allowlist is genuinely default-deny.

### Optional hardening (low-severity gaps)

- Add `read_only: true` + `tmpfs: /tmp` to the agent services.
- Put the hub + agents on an internal-only Docker network, or restrict `network_mode`.
