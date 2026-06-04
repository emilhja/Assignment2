"""Part 3 main loop.

Tiny by design: every concern lives in its own module. This file only:

  1. Reads env (AGENT_ID, AGENT_DISPLAY_NAME, AGENT_MODE, budget limits).
  2. Builds Budget, Transport, ConsoleControl, SessionStore.
  3. Loads `config/system_prompt.txt` and templates the identity in.
  4. Loops: recv → should_reply → run_peer_task → transport.send.

Local console only handles operator commands (`:budget`, `:limit`,
`:pause`, `:resume`, `:continue`, `:approve`, `:deny`, `:project`,
`:stop`). It never carries ordinary agent conversation — that goes through
the transport only (P3.4).
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import part2_bridge  # noqa: F401 — sys.path side effect

from thread_safe_store import ThreadSafeSessionStore as SessionStore

import colors
from budget import Budget, format_usage_summary
from claims import CLAIM_PATTERN, DEFER_PATTERN, RELEASE_PATTERN, Claim, ClaimRegistry, split_claim_target
from code_share import (
    MAX_PROJECTS,
    extract_code_blocks,
    most_recent_project_dir,
    named_project_dir,
    next_project_dir,
    process_shared_code,
)
from coordination import (
    SHARED_PATH_PATTERN,
    assignment_guidance,
    contract_first_guidance,
    fix_blockers_guidance,
    followup_assignment_guidance,
    handoff_guidance,
    parse_project_directive,
    private_workspace_guidance,
    proactive_assignment_guidance,
    project_name_from_shared_path,
    schema_stability_guidance,
    status_request_guidance,
)
from console_control import ConsoleControl
from message_assembler import MultipartAssembler
from peer import PeerMessage
from peer_task import _STALL_SILENT, run_peer_task
from reply_policy import CollisionInfo, should_reply
from task_status import TaskStatus, looks_like_empty_acknowledgment, parse_task_status
from transport import Transport, build_transport


CONFIG_DIR = Path(__file__).resolve().parent / "config"
DATA_DIR = Path(__file__).resolve().parent / "data"
SYSTEM_PROMPT_FILE = CONFIG_DIR / "system_prompt.txt"
DEFAULT_TPM = 100_000
DEFAULT_RPM = 30
DEFAULT_TOTAL = 2_000_000
DEFAULT_CLAIM_CONTINUATION_GRACE_SECONDS = 1.5
DEFAULT_PENDING_FOLLOWUP_SECONDS = 120.0
MAX_RECENT_CONTEXT_ENTRIES = 64

CONFIRMATION_REPLIES = {
    "yes",
    "y",
    "yep",
    "yeah",
    "ok",
    "okay",
    "sure",
    "yes please",
    "please do",
    "go ahead",
    "do it",
    "sounds good",
}
REJECTION_REPLIES = {
    "no",
    "nope",
    "not now",
    "cancel",
    "don't",
    "do not",
}
CONFIRMATION_REQUEST_PATTERN = re.compile(
    r"(?i)\b("
    r"would you like me to|do you want me to|should i|shall i|"
    r"want me to|can i proceed|should we proceed"
    r")\b"
)
TEST_REQUEST_PATTERN = re.compile(r"(?i)\b(?:pytest|tests?|tester)\b")
MISSING_PART_REQUEST_PATTERN = re.compile(
    r"(?is)("
    r"\b(?:where\s+is|missing|lost|send|resend|provide|show)\b.{0,120}\bpart\s+\d+\s*/\s*\d+\b"
    r"|"
    r"\bpart\s+\d+\s*/\s*\d+\b.{0,120}\b(?:missing|lost|where|send|resend|provide|show)\b"
    r")"
)
RESEND_CODE_REQUEST_PATTERN = re.compile(
    r"(?is)\b(?:send|resend|provide|show|paste)\b.{0,80}\b(?:full|complete|current)?\s*"
    r"(?:code|file|contents?)\b"
)
DIRECT_ACTION_REQUEST_PATTERN = re.compile(
    r"(?i)\b(?:share|shares|sharing|send|sends|sending|show|shows|showing|"
    r"paste|pastes|pasting|read|reads|reading|run|runs|running|test|testing|"
    r"review|reviews|reviewing|create|creates|creating|update|updates|updating|"
    r"fix|fixes|fixing)\b"
)
DIRECT_FILE_SHARE_PATTERN = re.compile(
    r"(?is)\b(?:share|send|show|paste|read)\b.{0,80}\b"
    r"(?P<filename>[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12})\b"
)
WORKSPACE_PATH_PATTERN = re.compile(r"(/workspace/[^\s`'\"<>),;]+)")
# Matches the canonical one-line intro the system prompt requires (P3.7).
# Used to suppress a second intro in the same session. Keep this exact to the
# configured display name so presence replies like "Hej, jag är här" are not
# mistaken for a repeated introduction.
INTRO_LINE_TEMPLATE = r"^\s*hej,?\s*jag\s+(?:är|ar)\s+@?{display_name}\s*[\.!]?\s*$"
COORDINATOR_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:you\s+are|act\s+as|be)\s+(?:the\s+)?"
    r"(?:manager|coordinator|lead|samordnare|projektledare)\b"
)


@dataclass
class PendingFollowup:
    timestamp: float
    message_id: str
    text: str


@dataclass
class _ProjectState:
    active: Path | None = None
    root: Path | None = None
    is_shared: bool = False


def _project_root(mode: str, agent_workspace: Path | None) -> tuple[Path | None, bool]:
    """Return (root, is_shared) for the project allocator.

    Remote (runpod) keeps the per-agent private workspace; local docker-compose
    sets ``SHARED_WORKSPACE=/workspace/shared`` so alice and bob co-write into
    the same project directory. The local branch only activates when the env
    var is explicitly set, so dev tests imported on a host without
    ``/workspace`` do not accidentally try to ``mkdir /workspace/shared``.
    """
    if mode == "runpod":
        return (agent_workspace, False)
    shared_env = os.environ.get("SHARED_WORKSPACE")
    if shared_env:
        return (Path(shared_env), True)
    return (None, False)


def _remote_workspace_guidance(agent_id: str, project_name: str) -> str:
    return (
        f"Remote hub mode (no shared filesystem). Your active project is "
        f"/workspace/{agent_id}/{project_name}/. Write every file you create or "
        f"edit under /workspace/{agent_id}/{project_name}/<filename>. Never "
        "say you wrote to /sandbox or /workspace/shared in remote hub mode; "
        "those are wrong here. Do NOT write to /workspace/shared/ on this hub "
        "— peers cannot see it and there is no point in claiming it. When you "
        "share code in chat, put `# file: <filename>` as the first line inside "
        "each Python/Markdown code block so peers save it under the intended "
        "name. If the runtime says peer code was saved, reference the exact "
        "saved /workspace/<agent>/<project>/<filename> path it reports, not a "
        "filename you expected. Do NOT emit CLAIM, RELEASE, or DEFER protocol "
        "lines on this hub; the system prompt's P3.9 protocol applies only to "
        "the local docker-compose demo. Use visible task-status phrases instead: "
        "for a direct assignment, first reply `Bekräftat, jag tar: <short task>` "
        "or `Confirmed, I'll take: <short task>`; for a self-selected task, use "
        "`Jag tar mig an: <short task>` or `I'm taking on: <short task>`. When "
        "the work is actually complete, use `Klar med: <task>. Filer: ... "
        "Tester: ...` or `Done with: <task>. Files: ... Tests: ...`. The "
        "operator can switch active project "
        "at any time with :project new or :project use N. "
        "Truthful-completion rule: never say 'Done', 'Implemented', 'Created', "
        "'Wrote', or 'Saved' unless a successful create_file/append_text/"
        "edit_section/replace_text/rename_file tool observation for the target "
        "file was returned in this round. Saying you 'will' or 'need to' do the "
        "work is not enough — the runtime will reprompt you until you make the "
        "real tool call. "
        "Show-your-work rule: after a successful write, your final answer MUST "
        "(1) name the exact path you wrote (full /workspace/<agent>/<project>/"
        "<filename>) and (2) paste the file contents in a fenced code block "
        "whose first line inside the fence is `# file: <filename>`. This is how "
        "peers see and reuse what you produced on a no-shared-filesystem hub. "
        "When you have run pytest, also include the pytest result line so "
        "everyone knows what was verified."
    )


def _no_project_conversation_guidance() -> str:
    """Runtime guidance shown to runpod agents that have no active project.

    The agent is still expected to converse with the hub — answer questions,
    plan, ask for clarification — but the runtime will refuse any
    create_file/append_text/edit_section/replace_text/rename_file call until
    `:project new` is run or a broadcast carries `PROJECT: <name>`. This
    string tells the model both halves so it does not assert "done" or "wrote"
    for work that has not been performed.
    """

    return (
        "Remote hub mode, but no active project is allocated yet. You may "
        "converse, plan, ask clarifying questions, and read existing files. "
        "Do NOT call create_file, append_text, edit_section, replace_text, "
        "or rename_file in this round — the runtime will refuse them with "
        "\"refused: no active project ...\". If the task requires writing "
        "files, ask the operator (in chat) to type `:project new` in the "
        "agent console, or to include `PROJECT: <name>` in their next "
        "broadcast. Truthful-completion rule still applies: do not claim "
        "'done', 'created', 'wrote', or 'saved' for work that has not been "
        "performed."
    )


def _local_workspace_guidance(agent_id: str, project_dir: Path) -> str:
    """Workspace guidance for local docker-compose mode.

    Local hub bind-mounts a single ``./workspace`` host directory into every
    container, so ``/workspace/shared/<project>/`` is genuinely co-visible.
    This helper is the *sole* source of truth for the local-mode CLAIM/
    RELEASE/DEFER protocol — the system prompt is mode-agnostic and no longer
    carries P3.8/P3.9. Anything the model needs to know about shared writes
    in local mode lives here.
    """
    project_name = project_dir.name
    shared_root = f"/workspace/shared/{project_name}/"
    return (
        # Path / mode
        f"Local hub mode (shared filesystem). Your active project is "
        f"{shared_root}. Peers can read and write to the same directory. "
        f"Write every file you create or edit under {shared_root}<filename> "
        f"— do not redirect to /workspace/{agent_id}/ when the operator "
        "named a shared path. You cannot read or write another agent's "
        "private workspace; if a peer asks about a file you cannot access, "
        "say so and ask them to move it under /workspace/shared/.\n"
        # CLAIM contract
        "Claim/defer protocol for shared writes:\n"
        "- Before any tool that creates, edits, or renames a file under "
        "/workspace/shared/, your final answer for that round MUST be a "
        "CLAIM line, not the tool call. Format: \"CLAIM "
        "/workspace/shared/<path>#<scope>: <one-line reason>\".\n"
        "- Protocol lines (CLAIM, RELEASE, DEFER) are still final answers "
        "and MUST be wrapped in the JSON envelope: "
        "{\"type\":\"final\",\"answer\":\"CLAIM /workspace/shared/<path>#<scope>: <reason>\"}. "
        "A bare \"CLAIM ...\" line outside the envelope is rejected by the "
        "parser and never reaches the hub.\n"
        "- Use a scope when work can be split inside one file, such as "
        "\"#add-subtract\" or \"#multiply-divide\". Claims for different "
        "scopes of the same file can proceed in parallel; a claim without "
        "\"#scope\" means the whole file and conflicts with every scope.\n"
        "- On the runtime continuation after your claim, invoke the write "
        "tool. If the file exists and your work is additive, use "
        "append_text. Do not post another CLAIM in the continuation.\n"
        "- If you observe another agent's CLAIM for the same path but a "
        "different scope, continue your own non-overlapping scoped work. "
        "Do not DEFER for different scopes.\n"
        "- If you observe another agent's CLAIM for the same path and same "
        "scope, or for the whole file you were about to write, do not call "
        "the write tool. Reply with \"DEFER to @<claimant>\" and offer "
        "review.\n"
        "- A peer's \"DEFER to @you\" line is a one-way acknowledgment, "
        "not a question. Do not reply to it with another DEFER. Continue "
        "your own non-overlapping scoped work.\n"
        "- The runtime enforces this: a shared write without your active "
        "claim returns \"refused: no active claim ...\"; a write targeting "
        "a conflicting peer claim returns \"refused: deferred: ...\". Do "
        "not retry — emit the DEFER line or explain the missing claim and "
        "stop.\n"
        "- If your scoped claim targets an existing shared file, do not "
        "recreate or overwrite it with create_file. Read the current file, "
        "then use append_text for additive new code/tests, or edit_section/"
        "replace_text for exact replacements, so other agents' sections are "
        "preserved.\n"
        "- Before posting a CLAIM for a scope on an existing shared file, "
        "call read_file on that path so you reason from current contents, "
        "not memory.\n"
        "- Before asserting in a final answer what a shared file contains, "
        "lacks, or how it changed, you MUST have a read_file or successful "
        "create_file/append_text/edit_section/replace_text tool_observation "
        "for that path in the current round. If you do not have one, say "
        "\"I need to re-read /workspace/shared/<path>\" and call read_file "
        "instead of asserting.\n"
        "- When your scoped work is finished, post \"RELEASE "
        "/workspace/shared/<path>#<scope>\". If you claimed the whole "
        "file, release the whole file path.\n"
        "- Do not post RELEASE in the same exchange as your CLAIM unless a "
        "successful create_file/append_text/edit_section/rename_file/"
        "replace_text tool_observation for that path has already been "
        "returned in this round. RELEASE without a successful write "
        "observation is rejected by the runtime and you will be reprompted "
        "to either call the write tool or explain why you cannot.\n"
        "- Only report that a shared file was created, added, updated, "
        "renamed, written, or implemented after a successful create_file/"
        "append_text/edit_section/rename_file/replace_text observation "
        "naming /workspace/shared/.\n"
        # Tie-break
        "Tie-break for racing CLAIMs:\n"
        "- If you observe a peer's CLAIM for the SAME "
        "/workspace/shared/<path>#<scope> you just claimed, do not race. "
        "Compare your AGENT_ID to the peer's sender_id character-by-"
        "character: the lexicographically smaller AGENT_ID keeps the "
        "claim. The other agent MUST post these two lines as its next "
        "reply, in this order: first \"DEFER to @<peer-display-name>\", "
        "then \"RELEASE /workspace/shared/<path>#<scope>\", and propose a "
        "non-overlapping scope instead.\n"
        "- Example: AGENT_ID \"alice\" < \"bob\", so alice keeps the same "
        "scoped claim; bob defers and releases.\n"
        "- After deferring, do not re-claim the same path in this session "
        "unless the original claimant posts RELEASE first.\n"
        # Project mgmt + closer
        "Do not auto-allocate a private /workspace/<agent>/projectN/ path "
        "on this hub — write to the shared project directory the runtime "
        "named. Truthful-completion rule: never say 'Done', 'Implemented', "
        "'Created', 'Wrote', or 'Saved' unless a successful create_file/"
        "append_text/edit_section/replace_text/rename_file tool observation "
        "for the target file was returned in this round. The operator can "
        "switch the active project at any time with :project use <name> or "
        ":project new."
    )


def _build_project_handler(project_state: _ProjectState):
    def _root() -> Path | None:
        return project_state.root

    def handler(action: str, rest: list[str]) -> str:
        action = (action or "info").lower()
        root = _root()
        if root is None:
            return "[project error] no project root configured"
        if action == "info":
            if project_state.active is None:
                return "active=<none>"
            return f"active={project_state.active.name}"
        if action == "new":
            nxt = next_project_dir(root)
            if nxt is None:
                return f"[project error] cap reached ({MAX_PROJECTS} projects)"
            project_state.active = nxt
            return f"active={nxt.name} (new)"
        if action == "use":
            if not rest:
                return "usage: :project use <N|name>"
            raw = rest[0]
            # Numeric form keeps the legacy projectN lookup; named form lets
            # local-mode operators jump to a co-visible shared project.
            try:
                n = int(raw)
            except ValueError:
                named = named_project_dir(root, raw)
                if named is None:
                    return (
                        f"[project error] invalid project name {raw!r} "
                        "(allowed: A-Z a-z 0-9 _ -)"
                    )
                project_state.active = named
                return f"active={named.name}"
            if n < 1 or n > MAX_PROJECTS:
                return f"[project error] N must be 1..{MAX_PROJECTS}, got {n}"
            target = root / f"project{n}"
            if not target.is_dir():
                return f"[project error] {target.name} does not exist"
            project_state.active = target
            return f"active={target.name}"
        if action == "list":
            entries = []
            for entry in sorted(
                root.iterdir() if root.exists() else [],
                key=lambda p: p.name,
            ):
                if not entry.is_dir():
                    continue
                marker = "*" if (
                    project_state.active is not None
                    and entry.name == project_state.active.name
                ) else " "
                entries.append(f"  {marker} {entry.name}")
            if not entries:
                return "[no projects]"
            return "\n".join(entries)
        return "usage: :project [info|new|use <N|name>|list]"

    return handler


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_system_prompt(agent_id: str, display_name: str) -> str:
    template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    return template.replace("{AGENT_ID}", agent_id).replace("{AGENT_DISPLAY_NAME}", display_name)


def _log(store: SessionStore, kind: str, content: str) -> None:
    store.record("system", kind, content)


def _render_no_project_prompt(project_root: Path) -> str:
    """Multi-line `[project?]` block for the docker-attach reconnect flow.

    Shown at startup (when existing projects are found, no active project is
    set yet). The agent still talks to the hub in this state, but file-write
    tools are refused at dispatch until the operator picks a project — so the
    banner spells out both halves: chat works, writes don't.
    """
    existing: list[str] = []
    if project_root.exists():
        existing = sorted(
            (
                e.name
                for e in project_root.iterdir()
                if e.is_dir() and re.fullmatch(r"project\d+", e.name)
            ),
            key=lambda n: int(n[len("project"):]),
        )
    header = (
        "[project?] no active project — replying to chat is enabled, "
        "but file-write tools are refused until you choose one."
    )
    return colors.dim(
        header
        + "\n[project?] existing: "
        + (", ".join(existing) if existing else "(none)")
        + "\n[project?] type `:project new` for a fresh project, "
        "`:project use <N>` to reconnect, or include `PROJECT: <name>` "
        "in the next broadcast to auto-start."
    )


def _claimed_targets(text: str) -> set[str]:
    targets: set[str] = set()
    for match in CLAIM_PATTERN.finditer(text or ""):
        path, scope = split_claim_target(match.group("path"))
        targets.add(f"{path}#{scope}" if scope else path)
    return targets


def _claim_continuation_message(original: PeerMessage, claim: Claim) -> PeerMessage:
    text = (
        "Continue the active shared-file claim you already posted. "
        f"Active claim target: {claim.target}. "
        f"Original request: {original.text}\n"
        "Use tools now; do not post another CLAIM. "
        "Only report a shared-file change after a successful tool observation for /workspace/shared/."
    )
    return PeerMessage(
        id=f"{original.id}:claim-continuation:{claim.target}",
        sender_id="runtime",
        text=text,
    )


def _task_status_continuation_message(original: PeerMessage, status: TaskStatus) -> PeerMessage:
    verification = ""
    if TEST_REQUEST_PATTERN.search(original.text or ""):
        verification = (
            " The original request asked for tests or pytest, so call run_tests "
            "before reporting done, unless a concrete blocker prevents it."
        )
    text = (
        "Continue the accepted task now. "
        f"Accepted task: {status.task}. "
        f"Original request: {original.text}\n"
        "Use tools now; do not only describe the work. Do not answer with a "
        "future-tense plan. Only report `Klar med:` or `Done with:` after "
        "successful tool observations for the actual work."
        f"{verification}"
    )
    return PeerMessage(
        id=f"{original.id}:task-status-continuation:{status.kind}",
        sender_id="runtime",
        text=text,
        addressed_to=(original.sender_id,),
    )


def _operator_continue_message(original: PeerMessage, active_project: str | None) -> PeerMessage:
    project_hint = (
        f"The active project is now {active_project}. "
        if active_project
        else "There is still no active project. "
    )
    text = (
        "Operator typed :continue in the local console. "
        "Continue or retry the last actionable hub request now. "
        f"{project_hint}"
        "If you previously stopped because no project was active, do not repeat "
        "that blocker when an active project is available; use the appropriate "
        "tools now. "
        f"Original sender: {original.sender_id}. "
        f"Original request: {original.text}"
    )
    return PeerMessage(
        id=f"{original.id}:operator-continue:{int(time.time())}",
        sender_id="runtime",
        text=text,
        addressed_to=(original.sender_id,),
    )


def _is_claim_continuation_message(message: PeerMessage) -> bool:
    return message.sender_id == "runtime" and ":claim-continuation:" in message.id


def _context_entry(sender_id: str, text: str, message_id: str | None = None) -> dict[str, str]:
    entry = {
        "sender_id": sender_id,
        "text": text,
    }
    if message_id is not None:
        entry["message_id"] = message_id
    return entry


def _latest_self_workspace_path(
    recent_context: list[dict[str, str]] | None,
    *,
    agent_id: str,
    display_name: str,
) -> str | None:
    """Return the newest workspace path this agent reported to chat."""

    self_names = {
        name.lower()
        for name in (agent_id, display_name)
        if isinstance(name, str) and name.strip()
    }
    for entry in reversed(recent_context or []):
        sender = str(entry.get("sender_id") or "").lower()
        if sender not in self_names:
            continue
        text = str(entry.get("text") or "")
        matches = [match.rstrip(".:") for match in WORKSPACE_PATH_PATTERN.findall(text)]
        if matches:
            return matches[-1]
    return None


def _is_directly_addressed(text: str, agent_id: str, display_name: str) -> bool:
    lowered = (text or "").lower()
    return (
        f"@{display_name.lower()}" in lowered
        or f"@{agent_id.lower()}" in lowered
        or re.search(rf"(?i)^\s*{re.escape(display_name)}\b\s*[:,\-]", text or "") is not None
        or re.search(rf"(?i)^\s*{re.escape(agent_id)}\b\s*[:,\-]", text or "") is not None
    )


def _direct_file_share_filename(text: str) -> str | None:
    match = DIRECT_FILE_SHARE_PATTERN.search(text or "")
    if match is None:
        return None
    filename = match.group("filename").strip()
    # Only accept a basename. The guidance path must come from runtime state,
    # not from untrusted prose that may smuggle traversal or a private path.
    if filename in {"", ".", ".."} or "/" in filename or "\\" in filename:
        return None
    return filename


def _active_project_tool_path(
    filename: str,
    *,
    active_project: Path | None,
    is_shared: bool,
    runpod: bool,
    agent_id: str,
) -> str | None:
    if active_project is None:
        return None
    candidate = active_project / filename
    if not candidate.is_file():
        return None
    if is_shared:
        return f"/workspace/shared/{active_project.name}/{filename}"
    if runpod:
        return f"/workspace/{agent_id}/{active_project.name}/{filename}"
    return None


def _resend_request_guidance(
    text: str,
    *,
    agent_id: str,
    display_name: str,
    recent_context: list[dict[str, str]] | None = None,
    active_project: Path | None = None,
    project_is_shared: bool = False,
    runpod: bool = False,
) -> str | None:
    if not isinstance(text, str):
        return None
    requested_filename = _direct_file_share_filename(text)
    if requested_filename is not None:
        path = _active_project_tool_path(
            requested_filename,
            active_project=active_project,
            is_shared=project_is_shared,
            runpod=runpod,
            agent_id=agent_id,
        )
        if path is None:
            return (
                f"The operator is asking you to share {requested_filename}, but the "
                "runtime cannot find that file in the active project. Do not regenerate "
                "it from chat memory, do not create a replacement, and do not go idle. "
                "Reply with a concrete blocker and ask for the exact workspace file path."
            )
        return (
            f"The operator is asking you to share {requested_filename} with a peer. "
            f"Treat {path} as the source of truth. Call read_file on that exact path "
            "before answering. After read_file, paste the current contents or summarize "
            "them to the requested @agent. Do not regenerate the file from chat memory, "
            "do not create a new sibling file, and do not overwrite the existing file."
        )
    if (
        MISSING_PART_REQUEST_PATTERN.search(text) is None
        and RESEND_CODE_REQUEST_PATTERN.search(text) is None
    ):
        return None
    path = _latest_self_workspace_path(
        recent_context,
        agent_id=agent_id,
        display_name=display_name,
    )
    if path is None:
        return (
            "The operator is asking for missing or resent code, but no prior "
            "workspace file path from your own replies is visible in recent context. "
            "Do not regenerate from memory or overwrite files. Reply with the current "
            "status and ask for the exact path if a file resend is required."
        )
    return (
        "The operator is asking for a missing part or a resend of code you already "
        f"reported. Treat {path} as the source of truth. Call read_file on that exact "
        "path before answering. Do not regenerate the file from memory, do not create "
        "a new sibling file, and do not overwrite the existing file. After read_file, "
        "paste the current file contents; if the hub splits the reply, rely on the "
        "runtime multipart sender."
    )


def _normalized_short_reply(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return normalized.strip(" .!?")


def _followup_reply_kind(text: str) -> str | None:
    normalized = _normalized_short_reply(text)
    if not normalized or len(normalized.split()) > 3:
        return None
    if normalized in CONFIRMATION_REPLIES:
        return "confirmation"
    if normalized in REJECTION_REPLIES:
        return "rejection"
    return None


def _looks_like_operator_sender(sender_id: str, agent_id: str) -> bool:
    sender = (sender_id or "").strip().lower()
    return bool(sender) and sender != agent_id.lower() and not sender.endswith("-swe")


def _answer_invites_followup(answer: str) -> bool:
    text = (answer or "").strip()
    return bool(text) and (text.endswith("?") or CONFIRMATION_REQUEST_PATTERN.search(text) is not None)


def _is_intro_line(text: str, display_name: str) -> bool:
    intro_pattern = INTRO_LINE_TEMPLATE.format(display_name=re.escape(display_name))
    return bool(re.match(intro_pattern, text or "", re.IGNORECASE))


def _intro_seen_in_store(store: SessionStore, display_name: str) -> bool:
    connection = getattr(store, "connection", None)
    if connection is None:
        return False
    try:
        rows = connection.execute(
            "SELECT content FROM events ORDER BY id DESC LIMIT 256"
        ).fetchall()
    except Exception:
        return False
    return any(_is_intro_line(str(row[0] or ""), display_name) for row in rows)


def _direct_actionable_request_guidance(message: PeerMessage, agent_id: str, display_name: str) -> str | None:
    text = message.text or ""
    if not _is_directly_addressed(text, agent_id, display_name):
        return None
    if COORDINATOR_ASSIGNMENT_PATTERN.search(text) is not None:
        return _direct_coordinator_assignment_guidance(message, agent_id, display_name)
    if DIRECT_ACTION_REQUEST_PATTERN.search(text) is None:
        return None
    return (
        "Direct actionable request detected. Do not answer with an intro, a readiness "
        "note, or vague coordination. Perform the requested action using tools, or "
        "state the concrete blocker that prevents the tool call. For share/send/show/"
        "paste/read requests about a file, call read_file on the current workspace "
        "path before answering."
    )


def _direct_coordinator_assignment_guidance(message: PeerMessage, agent_id: str, display_name: str) -> str | None:
    text = message.text or ""
    if not _is_directly_addressed(text, agent_id, display_name):
        return None
    if COORDINATOR_ASSIGNMENT_PATTERN.search(text) is None:
        return None
    return (
        "Direct coordinator assignment detected. Do not answer with an intro, "
        "a readiness note, or an invitation for volunteers. Your first visible "
        "reply must begin exactly `Confirmed, I'll take: coordination` and then "
        "assign concrete tasks with explicit `TASK @agent-name: <task>` lines. "
        "As coordinator, do not implement or test unless the operator explicitly "
        "reassigns you; collect status, enforce no duplicate work, verify the "
        "final test result, and summarize final run instructions."
    )


def _direct_actionable_reprompt_message(original: PeerMessage, reason: str, *, coordinator: bool) -> PeerMessage:
    if coordinator:
        text = (
            "Your last reply was suppressed because it was a "
            f"{reason}. The human directly assigned you the coordinator role. "
            "Reply now with a concrete status message that starts exactly "
            "`Confirmed, I'll take: coordination`, then provide explicit "
            "`TASK @agent-name: <task>` assignment lines. Do not introduce "
            "yourself, say you are merely ready, or ask agents to volunteer."
        )
    else:
        text = (
            "Your last reply was suppressed because it was a "
            f"{reason}. The human directly asked you to perform an action. "
            "Perform the requested action using tools, or state the concrete "
            "blocker. Do not introduce yourself, say you are ready, or post "
            "vague coordination."
        )
    return PeerMessage(
        id=f"{original.id}:direct-action-reprompt",
        sender_id="runtime",
        text=text,
    )


def _stale_claim_guidance(active_claims: list[Claim]) -> str | None:
    """Build a nudge for active claims that weren't satisfied on a prior turn.

    The runtime's `_continue_claims` only fires for CLAIMs in the just-sent
    reply, so an agent that posted CLAIM but then deferred or said "I will…"
    has no built-in reminder. This guidance string is appended to
    `runtime_guidance` before each task run so the model sees the open
    obligation alongside the new inbound message.
    """

    if not active_claims:
        return None
    targets = ", ".join(claim.target for claim in active_claims)
    return (
        "You have unsatisfied active CLAIM(s) from a previous turn: "
        f"{targets}. On this turn either complete the write with "
        "create_file/append_text/edit_section/rename_file/replace_text for each, or post "
        "`RELEASE <target>` to give it up. Do not re-post the same CLAIM."
    )


def _released_without_write_guidance(released_claims: list[Claim]) -> str | None:
    """Build a nudge for claims this agent RELEASEd before completing the write.

    `claims.recently_released_unsatisfied_for` returns claims that were
    released without ever being marked satisfied by a shared write. This
    surfaces on the next inbound turn so the model knows it abandoned work
    and should re-claim + write, rather than silently dropping the task.
    """

    if not released_claims:
        return None
    targets = ", ".join(claim.target for claim in released_claims)
    return (
        "You previously CLAIMed and then RELEASEd without a successful "
        f"write: {targets}. The work was abandoned. If you still intend "
        "to do it, post a fresh CLAIM for the target and then complete the "
        "write on the runtime continuation. Otherwise explain in chat why "
        "you are dropping the task."
    )


def run_group_chat(
    *,
    transport: Transport | None = None,
    budget: Budget | None = None,
    console: ConsoleControl | None = None,
    store: SessionStore | None = None,
    stop_event: threading.Event | None = None,
    idle_sleep: float = 0.05,
    claims: ClaimRegistry | None = None,
) -> None:
    """Main loop. Arguments are injection points for the tests."""

    DATA_DIR.mkdir(exist_ok=True)
    agent_id = os.environ.get("AGENT_ID", "local")
    display_name = os.environ.get("AGENT_DISPLAY_NAME", f"{agent_id}-swe")
    aliases = tuple(
        a.strip()
        for a in os.environ.get("AGENT_ALIASES", "").split(",")
        if a.strip()
    )
    mode = os.environ.get("AGENT_MODE", "stub").lower()
    runpod = mode == "runpod"

    owns_store = store is None
    if store is None:
        store = SessionStore(os.environ.get("AGENT_SESSION_DB", str(DATA_DIR / "session_history.sqlite3")))

    if budget is None:
        budget = Budget.load(
            DATA_DIR / f"budget_{agent_id}.json",
            tokens_per_minute=_env_int("AGENT_TPM_LIMIT", DEFAULT_TPM),
            requests_per_minute=_env_int("AGENT_RPM_LIMIT", DEFAULT_RPM),
            lifetime_tokens=_env_int("AGENT_TOTAL_TOKEN_LIMIT", DEFAULT_TOTAL),
        )

    if stop_event is None:
        stop_event = threading.Event()

    if claims is None:
        claims = ClaimRegistry()

    if transport is None:
        transport = build_transport(mode, agent_id, DATA_DIR)

    project_state = _ProjectState()
    project_handler = None
    continue_requests: queue.Queue[float] = queue.Queue()
    last_wakeable_message: PeerMessage | None = None

    multipart_assembler = MultipartAssembler()
    multipart_ready: list[PeerMessage] = []

    def _recv_assembled(timeout: float) -> PeerMessage | None:
        """Receive next downstream-ready message through the multi-part assembler.

        May return None even when the transport delivered a message — that
        means the message was a part that's still waiting for its siblings.
        The caller should re-poll. Also surfaces any incomplete groups that
        have timed out since the last call.
        """
        if multipart_ready:
            return multipart_ready.pop(0)
        for stale in multipart_assembler.flush_expired(time.time()):
            multipart_ready.append(stale)
        if multipart_ready:
            return multipart_ready.pop(0)
        raw = transport.recv(timeout=timeout)
        if raw is None:
            return None
        for ready in multipart_assembler.feed(raw, time.time()):
            multipart_ready.append(ready)
        if multipart_ready:
            return multipart_ready.pop(0)
        return None

    def _queue_continue() -> str:
        if last_wakeable_message is None:
            return "[continue] no prior actionable hub message to continue"
        if runpod and project_state.active is None:
            return "[continue] no active project; type :project new or :project use N first"
        continue_requests.put(time.time())
        return f"[continue queued] retrying msg {last_wakeable_message.id}"

    ws_env = os.environ.get("AGENT_WORKSPACE")
    agent_workspace = Path(ws_env) if ws_env else None
    project_root, is_shared = _project_root(mode, agent_workspace)
    if project_root is not None:
        project_root.mkdir(parents=True, exist_ok=True)
        project_state.root = project_root
        project_state.is_shared = is_shared
        project_handler = _build_project_handler(project_state)
        if runpod:
            most_recent = most_recent_project_dir(project_root)
            if most_recent is None:
                # First boot in this private workspace — no choice to make.
                initial = next_project_dir(project_root)
                project_state.active = initial
                active_name = initial.name if initial is not None else "<none>"
                print(
                    colors.dim(
                        f"[project] active={active_name} (new) — :project new for fresh, "
                        ":project use N to switch, :project list to enumerate."
                    ),
                    flush=True,
                )
            else:
                # Existing projects found — defer to operator so reconnect is explicit.
                # Render via the same prompt the skip-path uses so a late
                # `docker attach` sees a consistent message.
                print(_render_no_project_prompt(project_root), flush=True)
        else:
            # Local-hub shared mode: defer allocation. The first inbound message
            # that names /workspace/shared/<name>/ or includes a PROJECT: line
            # sets the active project lazily; operator can also use :project.
            print(
                colors.dim(
                    f"[project] shared root={project_root} — active project will be set "
                    "from an inbound /workspace/shared/<name>/ path or PROJECT: directive."
                ),
                flush=True,
            )

    if console is None:
        console = ConsoleControl(
            budget=budget,
            stop_event=stop_event,
            send_fn=transport.send,
            project_handler=project_handler,
            continue_handler=_queue_continue,
        )
        console.start()
    else:
        if project_handler is not None and console.project_handler is None:
            console.project_handler = project_handler
        if console.continue_handler is None:
            console.continue_handler = _queue_continue

    system_prompt = load_system_prompt(agent_id, display_name)
    recent_replies: list[tuple[float, str]] = []
    recent_context: list[dict[str, str]] = []
    claim_grace_seconds = max(
        0.0,
        _env_float("CLAIM_CONTINUATION_GRACE_SECONDS", DEFAULT_CLAIM_CONTINUATION_GRACE_SECONDS),
    )
    pending_followup_seconds = max(
        0.0,
        _env_float("PENDING_FOLLOWUP_SECONDS", DEFAULT_PENDING_FOLLOWUP_SECONDS),
    )
    pending_followup: PendingFollowup | None = None
    intro_sent: bool = _intro_seen_in_store(store, display_name)
    _log(
        store,
        "session_start",
        f"agent_id={agent_id} display={display_name} aliases={','.join(aliases)} mode={mode}",
    )
    alias_note = f" aliases=[{', '.join(aliases)}]" if aliases else ""
    print(
        colors.dim(
            f"[part3] {display_name} (id={agent_id}){alias_note} listening via {mode}. "
            f"Type :help for console commands."
        ),
        flush=True,
    )

    def _hub_echo(arrow: str, sender: str, text: str) -> None:
        snippet = text[:160].replace("\n", " ")
        tag = colors.dim(f"[hub{arrow}]")
        print(f"{colors.ts()} {tag} {colors.agent_label(sender)}: {snippet}", flush=True)

    def _peer_console_log(kind: str, detail: str) -> None:
        """Live-attach trace of LLM/tool/refusal events. Runpod mode only."""
        if not runpod:
            return
        print(f"{colors.ts()} {colors.dim(f'[{kind}]')} {detail}", flush=True)

    def _send_answer(answer: str, msg_id: str, *, suppress_intro: bool = False) -> str | None:
        nonlocal pending_followup, intro_sent
        is_intro = _is_intro_line(answer or "", display_name)
        if is_intro and (intro_sent or suppress_intro):
            # Defense-in-depth for the system prompt's "post intro at most once"
            # rule: if the model regresses and posts another intro after a peer
            # broadcast, drop it on the floor instead of sending duplicates to
            # the hub. The send-attempt is still counted against the
            # broadcast-reply window so the next broadcast doesn't get a free
            # turn.
            _log(store, "intro_suppressed", f"msg_id={msg_id}")
            recent_replies.append((time.time(), msg_id))
            if len(recent_replies) > 64:
                del recent_replies[:-64]
            pending_followup = None
            budget.save()
            reason = "intro" if suppress_intro and not intro_sent else "duplicate intro"
            _peer_console_log("suppress", f"{reason} msg_id={msg_id}")
            return reason
        if not is_intro and looks_like_empty_acknowledgment(answer):
            # Reply-discipline runtime gate: "Okej, jag förstår... Jag avvaktar"
            # carries no peer-unique value, so spend nothing on it. Counted
            # against the broadcast window for the same reason.
            _log(store, "acknowledgment_suppressed", f"msg_id={msg_id}")
            recent_replies.append((time.time(), msg_id))
            if len(recent_replies) > 64:
                del recent_replies[:-64]
            pending_followup = None
            budget.save()
            _peer_console_log("suppress", f"empty acknowledgment msg_id={msg_id}")
            return "empty acknowledgment"
        send_ok = transport.send(answer)
        if send_ok is False:
            _log(store, "send_failed", f"msg_id={msg_id}")
            pending_followup = None
            budget.save()
            _peer_console_log("hub!", f"send failed msg_id={msg_id}")
            return "send failed"
        if is_intro:
            intro_sent = True
        if not runpod:
            _hub_echo("->", display_name, answer)
        recent_context.append(_context_entry(display_name, answer, msg_id))
        if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
            del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
        recent_replies.append((time.time(), msg_id))
        if len(recent_replies) > 64:
            del recent_replies[:-64]
        if _answer_invites_followup(answer):
            pending_followup = PendingFollowup(timestamp=time.time(), message_id=msg_id, text=answer)
            _log(store, "pending_followup", f"msg_id={msg_id}")
        else:
            pending_followup = None
        budget.save()
        return None

    def _run_task_for_message(
        message: PeerMessage,
        prior_context: list[dict[str, str]] | None = None,
        collision: CollisionInfo | None = None,
        code_guidance: str | None = None,
    ) -> str | None:
        runtime_guidance = []
        if not _is_claim_continuation_message(message):
            guidance = assignment_guidance(
                message.text,
                agent_id=agent_id,
                display_name=display_name,
            )
            if guidance:
                runtime_guidance.append(guidance)
        guidance = _direct_coordinator_assignment_guidance(
            message,
            agent_id=agent_id,
            display_name=display_name,
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = followup_assignment_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = handoff_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = _resend_request_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
            active_project=project_state.active,
            project_is_shared=project_state.is_shared,
            runpod=runpod,
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = _direct_actionable_request_guidance(
            message,
            agent_id=agent_id,
            display_name=display_name,
        )
        if guidance:
            runtime_guidance.append(guidance)
        unsatisfied = claims.unsatisfied_claims_for(agent_id)
        status_guidance = status_request_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
            open_claim_targets=[claim.target for claim in unsatisfied] or None,
        )
        if status_guidance:
            runtime_guidance.append(status_guidance)
            # Status guidance already folds open claims into the Blockers line,
            # so skip the separate stale-claim nudge that would otherwise push
            # the agent toward RELEASE (the collision the previous run hit).
        else:
            stale_guidance = _stale_claim_guidance(unsatisfied)
            if stale_guidance:
                runtime_guidance.append(stale_guidance)
        guidance = _released_without_write_guidance(
            claims.recently_released_unsatisfied_for(agent_id)
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = fix_blockers_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = private_workspace_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = proactive_assignment_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
            has_open_claim=bool(unsatisfied),
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = contract_first_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
        )
        if guidance:
            runtime_guidance.append(guidance)
        guidance = schema_stability_guidance(
            message.text,
            agent_id=agent_id,
            display_name=display_name,
            recent_context=prior_context or [],
        )
        if guidance:
            runtime_guidance.append(guidance)
        if project_state.active is not None:
            if code_guidance:
                runtime_guidance.append(code_guidance)
            if runpod:
                runtime_guidance.append(
                    _remote_workspace_guidance(agent_id, project_state.active.name)
                )
            else:
                runtime_guidance.append(
                    _local_workspace_guidance(agent_id, project_state.active)
                )
        elif runpod:
            runtime_guidance.append(_no_project_conversation_guidance())
        try:
            result = run_peer_task(
                message,
                store=store,
                budget=budget,
                system_prompt=system_prompt,
                console=console,
                claims=claims,
                agent_id=agent_id,
                recent_context=prior_context,
                absorb_claims=False,
                collision=collision,
                runtime_guidance=runtime_guidance,
                console_log=_peer_console_log,
                # Only the runpod path gates writes on the active project.
                # Local-shared mode lets writes go to /workspace/shared/ directly
                # (CLAIM/claim-gate handles serialization), so `project_active`
                # stays True when not running against runpod.
                project_active=((not runpod) or project_state.active is not None),
            )
            if result is _STALL_SILENT:
                # Anti-stall/step-budget fallback fired with SUPPRESS_STALL_REPLIES
                # on: the diagnostic is already logged inside run_peer_task. Send
                # nothing to the hub rather than posting internal coaching text.
                _log(store, "stall_reply_suppressed", f"msg_id={message.id}")
                return None
            return result
        except RuntimeError as exc:
            # Most often: every LLM provider was rate-limited or unreachable.
            # Logging the failure and continuing means the agent stays online
            # and will retry on the next inbound message.
            print(
                colors.paint(
                    f"[llm!] {display_name} failed on msg {message.id}: {exc}",
                    colors.BRIGHT_RED,
                ),
                file=sys.stderr,
                flush=True,
            )
            _log(store, "llm_failure", f"msg_id={message.id} error={exc}")
            return None

    def _send_with_direct_assignment_reprompt(
        message: PeerMessage,
        answer: str,
        prior_context: list[dict[str, str]] | None,
    ) -> str:
        coordinator_assignment = (
            _looks_like_operator_sender(message.sender_id, agent_id)
            and _direct_coordinator_assignment_guidance(
                message,
                agent_id=agent_id,
                display_name=display_name,
            )
            is not None
        )
        direct_action = (
            _looks_like_operator_sender(message.sender_id, agent_id)
            and _direct_actionable_request_guidance(
                message,
                agent_id=agent_id,
                display_name=display_name,
            )
            is not None
        )
        suppression = _send_answer(
            answer,
            message.id,
            suppress_intro=direct_action,
        )
        if suppression is None:
            return answer
        if suppression == "send failed":
            return ""
        if not direct_action:
            return answer

        continuation = _direct_actionable_reprompt_message(
            message,
            suppression,
            coordinator=coordinator_assignment,
        )
        _log(
            store,
            "direct_assignment_reprompt" if coordinator_assignment else "direct_action_reprompt",
            f"reason={suppression} from_msg={message.id}",
        )
        reprompt_context = list(prior_context or [])
        reprompt_context.append(_context_entry(message.sender_id, message.text, message.id))
        reprompt_answer = _run_task_for_message(continuation, reprompt_context)
        if reprompt_answer is None:
            return answer
        _send_answer(reprompt_answer, continuation.id)
        return reprompt_answer

    def _remember_inbound(message: PeerMessage) -> list[dict[str, str]]:
        nonlocal intro_sent
        prior_context = list(recent_context)
        recent_context.append(_context_entry(message.sender_id, message.text, message.id))
        sender = (message.sender_id or "").strip().lower()
        self_names = {agent_id.lower(), display_name.lower()}
        if sender in self_names and _is_intro_line(message.text or "", display_name):
            intro_sent = True
        if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
            del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
        return prior_context

    def _absorb_inbound_claims(message: PeerMessage) -> None:
        for claim in claims.absorb_text(message.sender_id, message.text):
            _log(
                store,
                "claim_observed",
                (
                    f"claimant={claim.claimant} path={claim.path} "
                    f"scope={claim.scope or ''} target={claim.target}"
                ),
            )

    def _peer_claim_blocking_save(message: PeerMessage) -> str | None:
        """Return the first candidate path a peer holds an active CLAIM on.

        Only meaningful in shared-workspace mode: auto-saving peer code into
        ``/workspace/shared/<project>/`` while another agent has the same
        file claimed would overwrite their in-flight work. In private/remote
        mode the auto-save target is per-agent isolated, so we never block.

        The candidate compared against the claim registry is the canonical
        ``/workspace/shared/<project>/<filename>`` agent-facing path, because
        that is the form peers actually emit in CLAIM lines. The on-disk
        location (a tmpdir under tests, ``/workspace/shared`` in docker) is
        irrelevant for the gate check.
        """
        if not project_state.is_shared or project_state.active is None:
            return None
        blocks = extract_code_blocks(message.text or "")
        if not blocks:
            return None
        project_name = project_state.active.name
        for block in blocks:
            candidate = f"/workspace/shared/{project_name}/{block.filename}"
            conflicting = claims.is_claimed_by_other(candidate, agent_id)
            if conflicting is not None:
                return candidate
        return None

    def _process_message(message: PeerMessage, *, allow_claim_continuation: bool = True) -> None:
        nonlocal pending_followup, last_wakeable_message
        if not runpod:
            _hub_echo("<-", message.sender_id, message.text)

        prior_context = _remember_inbound(message)

        # Local shared mode: lazily set the active project from a `PROJECT:`
        # directive or the first `/workspace/shared/<name>/...` path in the
        # inbound. A new name in a later message switches the active project.
        if project_state.is_shared and project_state.root is not None:
            inferred_name: str | None = None
            directive_name = parse_project_directive(message.text)
            if directive_name:
                inferred_name = directive_name
            else:
                path_match = SHARED_PATH_PATTERN.search(message.text or "")
                if path_match:
                    inferred_name = project_name_from_shared_path(
                        path_match.group("path")
                    )
            if inferred_name:
                current_name = (
                    project_state.active.name if project_state.active is not None else None
                )
                if current_name != inferred_name.strip().lower():
                    new_dir = named_project_dir(project_state.root, inferred_name)
                    if new_dir is not None:
                        project_state.active = new_dir
                        _log(
                            store,
                            "project_set_from_inbound",
                            f"name={new_dir.name} msg_id={message.id} sender={message.sender_id}",
                        )

        # Remote mode: auto-allocate the next numeric project on an explicit
        # `PROJECT: <name>` directive, OR on the first inbound that carries at
        # least one markdown code fence. The directive name (or sender id, for
        # the code-share path) is logged but the dir keeps the numeric
        # `projectN` form (remote workspaces are private, so project names
        # don't need to match across agents). Plain text without a directive
        # or fences still falls through to the skip path below — that's the
        # reconnect-safety brake against stale broadcasts.
        if (
            runpod
            and project_state.active is None
            and project_state.root is not None
        ):
            directive_name = parse_project_directive(message.text or "")
            has_shared_code = (
                not directive_name and bool(extract_code_blocks(message.text or ""))
            )
            if directive_name or has_shared_code:
                new_dir = next_project_dir(project_state.root)
                if new_dir is not None:
                    project_state.active = new_dir
                    if directive_name:
                        _log(
                            store,
                            "project_auto_allocated",
                            (
                                f"name={new_dir.name} reason=directive "
                                f"directive_name={directive_name} msg_id={message.id}"
                            ),
                        )
                        print(
                            colors.dim(
                                f"[project] auto-allocated active={new_dir.name} "
                                f"from PROJECT: {directive_name}"
                            ),
                            flush=True,
                        )
                    else:
                        _log(
                            store,
                            "project_auto_allocated_from_code",
                            (
                                f"name={new_dir.name} reason=code_share "
                                f"sender={message.sender_id} msg_id={message.id}"
                            ),
                        )
                        print(
                            colors.dim(
                                f"[project] auto-allocated active={new_dir.name} "
                                f"from peer-shared code by {message.sender_id}"
                            ),
                            flush=True,
                        )

        # Runpod mode no longer parks inbound on missing project: the agent
        # still converses with the hub, but file-write tools are refused at
        # dispatch (see peer_task._maybe_no_project_refusal) until the
        # operator runs `:project new` or a broadcast carries `PROJECT:
        # <name>`. We log a one-line advisory so the operator still sees the
        # state in the container console.
        if runpod and project_state.active is None:
            _log(
                store,
                "no_active_project_advisory",
                f"msg_id={message.id} sender={message.sender_id}",
            )
            print(
                f"{colors.ts()} "
                + colors.dim(
                    "[project?] no active project — replying without writes; "
                    "type :project new or :project use N to enable file tools"
                ),
                flush=True,
            )

        # Save peer-shared code blocks to the active project even when the
        # reply gate will skip this message. Otherwise broadcasts of code
        # (e.g. peers posting `hangman.py` without addressing this agent)
        # never land on disk and we can't read_file them later.
        code_guidance: str | None = None
        if project_state.active is not None:
            block_path: str | None = _peer_claim_blocking_save(message)
            if block_path is not None:
                _log(
                    store,
                    "code_save_skipped_claim_conflict",
                    f"path={block_path} msg_id={message.id} sender={message.sender_id}",
                )
                code_guidance = (
                    "A peer message contained a code block for a file currently "
                    f"covered by another agent's active CLAIM ({block_path}). The "
                    "runtime did not auto-save it. read_file the path the peer "
                    "names; do not overwrite their in-flight work."
                )
            else:
                code_guidance = process_shared_code(
                    message.text,
                    agent_id,
                    project_state.active,
                    auto_pytest=not project_state.is_shared,
                    shared_root=project_state.root if project_state.is_shared else None,
                )
                if code_guidance:
                    _log(
                        store,
                        "code_saved_on_arrival",
                        f"msg_id={message.id} sender={message.sender_id}",
                    )

        now = time.time()
        followup_kind = _followup_reply_kind(message.text)
        if (
            followup_kind is not None
            and pending_followup is not None
            and _looks_like_operator_sender(message.sender_id, agent_id)
            and now - pending_followup.timestamp <= pending_followup_seconds
        ):
            reason = f"follow-up {followup_kind}"
            _log(
                store,
                "reply_decision",
                f"respond=True reason={reason} msg_id={message.id} sender={message.sender_id}",
            )
            pending_followup = None
            last_wakeable_message = message
            answer = _run_task_for_message(
                message, prior_context, code_guidance=code_guidance
            )
            if answer is None:
                _absorb_inbound_claims(message)
                return
            _absorb_inbound_claims(message)
            sent_answer = _send_with_direct_assignment_reprompt(
                message,
                answer,
                prior_context,
            )

            if allow_claim_continuation:
                _continue_claims(message, sent_answer)
            return

        if pending_followup is not None and now - pending_followup.timestamp > pending_followup_seconds:
            _log(store, "pending_followup_expired", f"msg_id={pending_followup.message_id}")
            pending_followup = None

        decision = should_reply(
            message, agent_id, display_name, recent_replies,
            claims=claims, aliases=aliases,
        )
        _log(
            store,
            "reply_decision",
            f"respond={decision.respond} reason={decision.reason} msg_id={message.id} sender={message.sender_id}",
        )
        if not decision.respond:
            _absorb_inbound_claims(message)
            if runpod:
                print(
                    f"{colors.ts()} {colors.dim(f'[skip] {decision.reason}')}",
                    flush=True,
                )
            return

        if decision.delay_seconds > 0:
            time.sleep(decision.delay_seconds)

        last_wakeable_message = message
        answer = _run_task_for_message(
            message, prior_context, decision.collision, code_guidance=code_guidance
        )
        if answer is None:
            _absorb_inbound_claims(message)
            return
        _absorb_inbound_claims(message)
        sent_answer = _send_with_direct_assignment_reprompt(
            message,
            answer,
            prior_context,
        )

        if allow_claim_continuation:
            _continue_task_status(message, sent_answer)
            _continue_claims(message, sent_answer)

    def _code_guidance_for_active_project(message: PeerMessage) -> str | None:
        if project_state.active is None:
            return None
        block_path = _peer_claim_blocking_save(message)
        if block_path is not None:
            _log(
                store,
                "code_save_skipped_claim_conflict",
                f"path={block_path} msg_id={message.id} sender={message.sender_id}",
            )
            return (
                "A peer message contained a code block for a file currently "
                f"covered by another agent's active CLAIM ({block_path}). The "
                "runtime did not auto-save it. read_file the path the peer "
                "names; do not overwrite their in-flight work."
            )
        code_guidance = process_shared_code(
            message.text,
            agent_id,
            project_state.active,
            auto_pytest=not project_state.is_shared,
            shared_root=project_state.root if project_state.is_shared else None,
        )
        if code_guidance:
            _log(
                store,
                "code_saved_on_continue",
                f"msg_id={message.id} sender={message.sender_id}",
            )
        return code_guidance

    def _continue_from_console() -> None:
        original = last_wakeable_message
        if original is None:
            _log(store, "operator_continue_skipped", "no prior actionable message")
            return
        active_name = project_state.active.name if project_state.active is not None else None
        continuation = _operator_continue_message(original, active_name)
        _log(
            store,
            "operator_continue",
            f"from_msg={original.id} active={active_name or '<none>'}",
        )
        prior_context = list(recent_context)
        recent_context.append(
            _context_entry(continuation.sender_id, continuation.text, continuation.id)
        )
        if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
            del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
        answer = _run_task_for_message(
            continuation,
            prior_context,
            code_guidance=_code_guidance_for_active_project(original),
        )
        if answer is None:
            return
        _send_answer(answer, continuation.id)
        _continue_task_status(continuation, answer)
        _continue_claims(continuation, answer)

    def _continue_task_status(original: PeerMessage, answer: str, depth: int = 0) -> None:
        if depth >= 3:
            _log(store, "task_status_continuation_skipped", "maximum nested task continuation depth reached")
            return
        status = parse_task_status(answer)
        if status is None or status.kind not in {"taking", "accepted"}:
            return
        if any(
            marker in status.task.lower()
            for marker in ("coordination", "coordinator", "manager", "samordn")
        ):
            _log(
                store,
                "task_status_continuation_skipped",
                f"coordination role status from_msg={original.id}",
            )
            return

        continuation = _task_status_continuation_message(original, status)
        _log(
            store,
            "task_status_continuation",
            f"kind={status.kind} language={status.language} task={status.task} from_msg={original.id}",
        )
        prior_context = list(recent_context)
        recent_context.append(_context_entry(continuation.sender_id, continuation.text, continuation.id))
        if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
            del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
        continuation_answer = _run_task_for_message(continuation, prior_context)
        if continuation_answer is None:
            return
        _send_answer(continuation_answer, continuation.id)
        _continue_task_status(continuation, continuation_answer, depth + 1)
        _continue_claims(continuation, continuation_answer, depth + 1)

    def _continue_claims(original: PeerMessage, answer: str, depth: int = 0) -> None:
        if depth >= 3:
            _log(store, "claim_continuation_skipped", "maximum nested claim continuation depth reached")
            return
        targets = _claimed_targets(answer)
        if not targets:
            return

        active = [
            claim for claim in claims.active_claims_for(agent_id)
            if claim.target in targets
        ]
        for claim in active:
            deadline = time.time() + claim_grace_seconds
            while time.time() < deadline and not stop_event.is_set():
                remaining = max(0.0, deadline - time.time())
                peer_message = _recv_assembled(timeout=min(remaining, 0.5))
                if peer_message is None:
                    time.sleep(min(remaining, idle_sleep))
                    continue
                _process_message(peer_message, allow_claim_continuation=False)
                if claims.is_claimed_by_other(claim.target, agent_id) is not None:
                    _log(store, "claim_continuation_skipped", f"conflict on {claim.target}")
                    return
                if claims.own_claim_for_write(claim.path, agent_id) is None:
                    _log(store, "claim_continuation_skipped", f"claim released for {claim.target}")
                    return

            if claims.is_claimed_by_other(claim.target, agent_id) is not None:
                _log(store, "claim_continuation_skipped", f"conflict on {claim.target}")
                return
            if claims.own_claim_for_write(claim.path, agent_id) is None:
                _log(store, "claim_continuation_skipped", f"claim released for {claim.target}")
                return

            continuation = _claim_continuation_message(original, claim)
            _log(store, "claim_continuation", f"target={claim.target} from_msg={original.id}")
            prior_context = list(recent_context)
            recent_context.append(_context_entry(continuation.sender_id, continuation.text, continuation.id))
            if len(recent_context) > MAX_RECENT_CONTEXT_ENTRIES:
                del recent_context[:-MAX_RECENT_CONTEXT_ENTRIES]
            continuation_answer = _run_task_for_message(continuation, prior_context)
            if continuation_answer is not None:
                _send_answer(continuation_answer, continuation.id)
                _continue_claims(continuation, continuation_answer, depth + 1)
            # If the agent still holds this claim AND it hasn't been satisfied by
            # a successful write, AND the continuation answer was neither a
            # legitimate DEFER/RELEASE nor a follow-on CLAIM, the continuation
            # just died silently — the peer-task reprompt loop didn't fire
            # (e.g. prose final that didn't match the pending-write detector).
            # Surface that in audit so the stall is visible without timeline
            # cross-referencing.
            answer_text = continuation_answer or ""
            silent_final = not (
                CLAIM_PATTERN.search(answer_text)
                or RELEASE_PATTERN.search(answer_text)
                or DEFER_PATTERN.search(answer_text)
            )
            still_unsatisfied = any(
                c.target == claim.target for c in claims.unsatisfied_claims_for(agent_id)
            )
            if still_unsatisfied and silent_final:
                _log(
                    store,
                    "claim_continuation_ended_without_progress",
                    f"target={claim.target} from_msg={original.id}",
                )

    try:
        while not stop_event.is_set():
            try:
                continue_requests.get_nowait()
            except queue.Empty:
                pass
            else:
                _continue_from_console()
                continue
            message = _recv_assembled(timeout=1.0)
            if message is None:
                time.sleep(idle_sleep)
                continue
            _process_message(message)
    except KeyboardInterrupt:
        print(
            colors.dim("\n[part3] keyboard interrupt — shutting down"),
            file=sys.stderr,
        )
    finally:
        _log(store, "session_end", f"agent_id={agent_id}")
        budget.save()
        print(format_usage_summary(display_name, budget.snapshot()), flush=True)
        try:
            transport.close()
        except Exception:
            pass
        console.stop()
        if owns_store:
            store.close()
