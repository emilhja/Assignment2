"""Deterministic coordination hints for common group-chat task shapes."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

from claims import CLAIM_PATTERN, split_claim_target
from task_status import parse_task_status


SHARED_PATH_PATTERN = re.compile(
    r"(?P<path>/workspace/shared/[^\s:;,]+?)"
    r"(?=(?:\.(?:First|Then|Next|Each|After|Before|Finally)\b)|[\s:;,]|$)"
)
WRITES_PATTERN = re.compile(
    r"(?i)\b(?P<agent>[A-Za-z0-9_.-]+)\s+writes?\s+(?P<task>[^,.;\n]+)"
)
OWNS_PATTERN = re.compile(
    r"(?i)\b(?P<agent>[A-Za-z0-9_.-]+)\s+owns?\s+(?P<task>[^,.;\n]+)"
)
YOU_WRITES_PATTERN = re.compile(r"(?i)\byou\s+(?:also\s+)?writes?\s+(?P<task>[^,.;\n]+)")
TAKEOVER_PATTERN = re.compile(
    r"(?i)\b(?:take\s+over|handoff|hand\s+off)\b(?:\s+from\s+@?(?P<peer>[A-Za-z0-9_.-]+))?"
)
SIGNATURE_AGREEMENT_PATTERN = re.compile(
    r"(?i)\b(?:agree|agreement|state\s+agreement|propose|confirm)\s+"
    r"(?:on\s+)?(?:the\s+)?(?:function\s+)?signatures?\b"
)
PROJECT_DIRECTIVE_PATTERN = re.compile(
    r"(?im)^\s*PROJECT:\s*(?P<name>[A-Za-z0-9_\-]+)\s*$"
)
PYTEST_REQUEST_PATTERN = re.compile(r"(?i)\b(?:pytest|tests?)\b")
PYTEST_FIX_PATTERN = re.compile(r"(?i)\b(?:fix|repair|update|debug)\b.*\b(?:pytest|tests?)\b")
SHARED_TEST_PATH_PATTERN = re.compile(r"(?P<path>/workspace/shared/(?:test_[^\s:;,]+|[^\s:;,]+_test)\.py)")
STATUS_REQUEST_PATTERN = re.compile(
    r"(?i)(?:\bare\s+you\s+(?:done|finished)\b"
    r"|\b(?:did|have)\s+you\s+(?:finish|complete)(?:ed)?\b"
    r"|\bdone\s+yet\b|\bany\s+update\b"
    r"|\b(?:done|finished|status)\s*\?)"
)
FIX_REQUEST_PATTERN = re.compile(
    r"(?i)(?:"
    r"\bfix\s+(?:the\s+|these\s+|those\s+|that\s+|all\s+|your\s+|any\s+|my\s+)?"
    r"(?:failing\s+|broken\s+|failed\s+|red\s+|remaining\s+|open\s+)?"
    r"(?:blocker|failure|issue|error|bug|problem|test|broken)s?\b"
    r"|\baddress\s+(?:the\s+|your\s+|these\s+|those\s+|all\s+)?"
    r"(?:blocker|failure|issue|error)s?\b"
    r"|\bresolve\s+(?:the\s+|these\s+|those\s+|all\s+)?"
    r"(?:blocker|failure|issue|error)s?\b"
    r"|\bmake\s+(?:the\s+)?tests?\s+pass\b"
    r"|\bget\s+(?:the\s+)?tests?\s+(?:to\s+)?(?:pass|passing|green)\b"
    r")"
)
PRIVATE_WORKSPACE_PATH_PATTERN = re.compile(
    r"(?P<path>/workspace/(?P<agent>[A-Za-z0-9_-]+)/[^\s:;,]+)"
)


@dataclass(frozen=True)
class Assignment:
    agent: str
    task: str
    scope: str


@dataclass(frozen=True)
class CoordinationPlan:
    path: str
    assignments: tuple[Assignment, ...]


def _agent_aliases(agent_id: str, display_name: str) -> set[str]:
    aliases = {agent_id.lower(), display_name.lower()}
    if "-" in display_name:
        aliases.add(display_name.split("-", 1)[0].lower())
    return {alias for alias in aliases if alias}


def _mentions_current_agent(text: str, agent_id: str, display_name: str) -> bool:
    lowered = text.lower()
    return any(f"@{alias}" in lowered for alias in _agent_aliases(agent_id, display_name))


def _scope_from_task(task: str) -> str:
    normalized = task.lower().replace("&", " and ")
    normalized = normalized.replace("+", " ")
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or "task"


def _assignments_from_pattern(pattern: re.Pattern[str], text: str) -> list[Assignment]:
    assignments: list[Assignment] = []
    for match in pattern.finditer(text):
        agent = match.group("agent").strip().lstrip("@")
        task = match.group("task").strip()
        if not agent or not task:
            continue
        assignments.append(Assignment(agent=agent, task=task, scope=_scope_from_task(task)))
    return assignments


def parse_project_directive(text: str) -> str | None:
    """Return the project name from a ``PROJECT: <name>`` line in ``text``.

    Returns the first match's name as-typed (case preserved); the caller is
    responsible for the case-folding/sanitization done inside
    ``code_share.named_project_dir``. Returns ``None`` for non-strings,
    empty input, or when no directive line is present.
    """
    if not isinstance(text, str) or not text:
        return None
    match = PROJECT_DIRECTIVE_PATTERN.search(text)
    if match is None:
        return None
    name = match.group("name").strip()
    return name or None


def project_name_from_shared_path(path: str) -> str | None:
    """Return the first directory segment under ``/workspace/shared/``, or None.

    Returns ``None`` when the path is a file directly under ``/workspace/shared/``
    (no project subfolder), e.g. ``/workspace/shared/calculator.py``. In that
    case the runtime leaves the active project untouched — writes still happen
    under ``/workspace/shared/`` as usual.
    """

    if not isinstance(path, str):
        return None
    prefix = "/workspace/shared/"
    if not path.startswith(prefix):
        return None
    remainder = path[len(prefix):]
    if not remainder or "/" not in remainder:
        return None
    first = remainder.split("/", 1)[0].strip()
    return first or None


def parse_coordination_plan(text: str) -> CoordinationPlan | None:
    """Parse simple coordinator messages with a shared path and agent slices."""

    if not isinstance(text, str):
        return None
    path_match = SHARED_PATH_PATTERN.search(text)
    if not path_match:
        return None

    assignments = [
        *_assignments_from_pattern(WRITES_PATTERN, text),
        *_assignments_from_pattern(OWNS_PATTERN, text),
    ]

    if not assignments:
        return None
    path, _scope = split_claim_target(path_match.group("path").rstrip("."))
    return CoordinationPlan(path=path, assignments=tuple(assignments))


def _signature_agreement_guidance(text: str, own: Assignment) -> str:
    if SIGNATURE_AGREEMENT_PATTERN.search(text) is None:
        return ""
    operation_names = [
        part
        for part in re.split(r"[^A-Za-z0-9_]+", own.task.lower())
        if part
    ]
    examples = ", ".join(f"def {name}(a, b)" for name in operation_names)
    if examples:
        examples = f" Suggested signatures for your scope: {examples}."
    return (
        " Function-signature agreement was requested before implementation. "
        "Do not wait indefinitely: state agreement on simple two-argument Python "
        "function signatures, then emit the required CLAIM in the same final answer "
        "so the runtime continuation can implement the assigned scope."
        f"{examples}"
    )


def _test_path_for_source(path: str) -> str | None:
    if not path.startswith("/workspace/shared/") or not path.endswith(".py"):
        return None
    directory, _separator, filename = path.rpartition("/")
    stem = filename[:-3]
    if stem.startswith("test_"):
        return path
    return f"{directory}/test_{stem}.py"


def _pytest_sidecar_guidance(
    text: str,
    path: str,
    own: Assignment,
    peer_count: int = 0,
) -> str:
    if PYTEST_REQUEST_PATTERN.search(text) is None:
        return ""
    test_path = _test_path_for_source(path)
    if not test_path:
        return ""
    base = (
        " Pytest coverage was requested next to the shared file. After completing "
        f"the implementation write, use a separate CLAIM for {test_path}#{own.scope}-tests "
        "before creating or editing tests for your scope."
    )
    verify_instruction = (
        " After the test-file write succeeds, call run_tests with "
        f'{{"path": "{test_path}"}} in the same continuation before sending any '
        "final answer. Your Done line must report 'Tests: ran and passed' or "
        "'Tests: ran and failed' with the first failure summary — "
        "'Tests: not run' is not acceptable when pytest coverage was requested."
    )
    if peer_count <= 0:
        return base + verify_instruction
    return base + (
        f" If {test_path} already exists when you go to write, do not append-only. "
        "Call read_file on it first, then use replace_text on the "
        "`from <module> import ...` line to add the symbol(s) you are about to test "
        "before adding your test functions — otherwise pytest fails with NameError "
        "on the symbols your peer did not import."
    ) + verify_instruction


def assignment_guidance(
    text: str,
    *,
    agent_id: str,
    display_name: str,
) -> str | None:
    plan = parse_coordination_plan(text)
    if plan is None:
        return None

    aliases = _agent_aliases(agent_id, display_name)
    own = next(
        (assignment for assignment in plan.assignments if assignment.agent.lower() in aliases),
        None,
    )
    if own is None:
        return None

    peer_bits = [
        f"@{assignment.agent} -> {assignment.task} (#{assignment.scope})"
        for assignment in plan.assignments
        if assignment is not own
    ]
    peers = "; ".join(peer_bits) if peer_bits else "none"
    claim_target = f"{plan.path}#{own.scope}"
    race_hint = (
        f" A peer may create {plan.path} before you. If read_file shows the file already "
        "exists, do not retry create_file — call append_text for additive work or "
        "edit_section/replace_text for exact replacements so peer scopes are preserved."
        if peer_bits
        else ""
    )
    return (
        "Coordinator assignment detected. "
        f"Shared path: {plan.path}. "
        f"Your assigned work: {own.task}. "
        f"Required CLAIM target: {claim_target}. "
        f"Other assigned scopes: {peers}. "
        "Do not claim or write another agent's assigned scope."
        f"{race_hint}"
        f"{_signature_agreement_guidance(text, own)}"
        f"{_pytest_sidecar_guidance(text, plan.path, own, peer_count=len(peer_bits))}"
    )


def followup_assignment_guidance(
    text: str,
    *,
    agent_id: str,
    display_name: str,
    recent_context: list[dict[str, str]],
) -> str | None:
    """Parse direct follow-ups like "@bob-swe you also write multiply + division"."""

    if not isinstance(text, str) or not _mentions_current_agent(text, agent_id, display_name):
        return None
    match = YOU_WRITES_PATTERN.search(text)
    if not match:
        if PYTEST_FIX_PATTERN.search(text) is None:
            return None
        test_path = _latest_shared_test_path(recent_context)
        if not test_path:
            return None
        return (
            "Pytest follow-up detected. "
            f"Shared test path: {test_path}. "
            "Before asking for more context, call read_file on that path, then "
            "CLAIM the relevant test-file scope before editing it."
        )

    path_match = SHARED_PATH_PATTERN.search(text)
    path = path_match.group("path").rstrip(".") if path_match else ""
    if not path:
        plan = _latest_coordination_plan(recent_context)
        if plan is not None:
            path = plan.path
    if not path:
        return None

    task = match.group("task").strip()
    scope = _scope_from_task(task)
    return (
        "Coordinator follow-up assignment detected. "
        f"Shared path: {path}. "
        f"Your assigned work: {task}. "
        f"Required CLAIM target: {path}#{scope}. "
        "Do not claim or write another agent's assigned scope."
    )


def _latest_coordination_plan(recent_context: Iterable[dict[str, str]]) -> CoordinationPlan | None:
    for entry in reversed(list(recent_context)):
        plan = parse_coordination_plan(str(entry.get("text") or ""))
        if plan is not None:
            return plan
    return None


def _latest_shared_test_path(recent_context: Iterable[dict[str, str]]) -> str | None:
    for entry in reversed(list(recent_context)):
        text = str(entry.get("text") or "")
        match = SHARED_TEST_PATH_PATTERN.search(text)
        if match:
            return match.group("path").rstrip(".")

        for claim_match in CLAIM_PATTERN.finditer(text):
            path, _scope = split_claim_target(claim_match.group("path"))
            if SHARED_TEST_PATH_PATTERN.fullmatch(path):
                return path

    plan = _latest_coordination_plan(recent_context)
    if plan is None:
        return None
    return _test_path_for_source(plan.path)


def _claim_sender_matches(sender: str, peer: str) -> bool:
    sender_norm = sender.lower()
    peer_norm = peer.lower().lstrip("@")
    return sender_norm == peer_norm or sender_norm.split("-", 1)[0] == peer_norm.split("-", 1)[0]


def _latest_claim_for_peer(
    peer: str,
    recent_context: Iterable[dict[str, str]],
) -> tuple[str, str] | None:
    for entry in reversed(list(recent_context)):
        sender = str(entry.get("sender_id") or "")
        if not _claim_sender_matches(sender, peer):
            continue
        text = str(entry.get("text") or "")
        for match in CLAIM_PATTERN.finditer(text):
            path = match.group("path")
            base, scope = split_claim_target(path)
            return base, scope or ""
    return None


def handoff_guidance(
    text: str,
    *,
    agent_id: str,
    display_name: str,
    recent_context: list[dict[str, str]],
) -> str | None:
    """Return guidance for "take over from X" when recent context identifies X's work."""

    if not isinstance(text, str) or not recent_context:
        return None
    match = TAKEOVER_PATTERN.search(text)
    if not match:
        return None

    peer = (match.group("peer") or "").strip().lstrip("@")
    if not peer:
        return None

    path = ""
    scope = ""
    latest_claim = _latest_claim_for_peer(peer, recent_context)
    if latest_claim is not None:
        path, scope = latest_claim
    else:
        plan = _latest_coordination_plan(recent_context)
        if plan is not None:
            assignment = next(
                (
                    item
                    for item in plan.assignments
                    if item.agent.lower() == peer.lower()
                    or item.agent.lower().split("-", 1)[0] == peer.lower().split("-", 1)[0]
                ),
                None,
            )
            if assignment is not None:
                path = plan.path
                scope = assignment.scope

    if not path or not scope:
        return None

    aliases = _agent_aliases(agent_id, display_name)
    target_label = f"@{display_name}" if display_name else f"@{agent_id}"
    claim_target = f"{path}#{scope}"
    return (
        "Handoff request detected from recent context. "
        f"The requested takeover target is {claim_target}, previously associated with @{peer}. "
        f"Ask @{peer} to post `RELEASE {claim_target}` before writing. "
        f"After release, {target_label} should post `CLAIM {claim_target}: take over handoff scope`. "
        f"Current agent aliases: {', '.join(sorted(aliases))}."
    )


def status_request_guidance(
    text: str,
    *,
    agent_id: str,
    display_name: str,
    recent_context: list[dict[str, str]] | None = None,
    open_claim_targets: list[str] | None = None,
) -> str | None:
    """Return guidance when the operator asks for completion status.

    The reply gate decides whether to respond at all; this helper only
    shapes the response so the agent emits a structured Done/Tests/Blockers
    line instead of free-form prose.

    When `open_claim_targets` is non-empty, the caller is signalling that the
    agent still holds unsatisfied claim(s) — fold them into the Blockers
    sentence so the operator sees them, and stop the caller from layering a
    separate stale-claim nudge that pushes the agent toward RELEASE.
    """

    if not isinstance(text, str) or STATUS_REQUEST_PATTERN.search(text) is None:
        return None
    test_path = _latest_shared_test_path(recent_context or [])
    test_hint = (
        f" If you have not yet run pytest for your scope, call run_tests on {test_path} first."
        if test_path
        else " If you wrote tests but have not run them, call run_tests on the shared test file first."
    )
    if open_claim_targets:
        targets = ", ".join(open_claim_targets)
        blocker_hint = (
            f" You still hold unsatisfied CLAIM(s): {targets}. List them in the Blockers field "
            "instead of posting RELEASE just because status was requested."
        )
    else:
        blocker_hint = ""
    return (
        "The operator is asking for completion status. Reply with one short message in this exact "
        "shape (substitute the bracketed parts): "
        "'Done: <one-line summary of what you implemented> at </workspace/shared/...>. "
        "Tests: <ran and passed | ran and failed | not run> (include the test file path if you ran them). "
        "Blockers: <none | brief description>.' "
        "Do not invent results — only report tests as ran if a successful run_tests observation exists "
        f"in this round.{test_hint}{blocker_hint}"
    )


def _latest_self_blockers_line(
    recent_context: Iterable[dict[str, str]],
    agent_aliases: set[str],
) -> str | None:
    """Return the most recent non-trivial 'Blockers: ...' text the agent itself
    posted, so a follow-up fix-request can surface the prior self-reported
    failure without relying on the per-task session carrying it forward.
    """

    for entry in reversed(list(recent_context)):
        sender = str(entry.get("sender_id") or "").lower()
        if sender not in agent_aliases:
            continue
        text = str(entry.get("text") or "")
        match = re.search(r"(?i)Blockers:\s*(?P<blockers>[^\n]+)", text)
        if not match:
            continue
        blockers = match.group("blockers").strip().rstrip(".").strip()
        if blockers and blockers.lower() != "none":
            return blockers
    return None


def fix_blockers_guidance(
    text: str,
    *,
    agent_id: str,
    display_name: str,
    recent_context: list[dict[str, str]] | None = None,
) -> str | None:
    """Return guidance when the operator asks the agent to fix prior blockers.

    Each peer turn is a fresh per-task session, so the agent typically does not
    have the prior round's `run_tests` observation in context. Without this
    helper the model tends to emit a bare `type:"final"` refusal ("I'm unable
    to fix without knowing the exact issues") instead of re-running tests to
    fetch the failure. This guidance forbids that path and forces the agent
    to call `run_tests` or `read_file` before any final answer this turn.
    """

    if not isinstance(text, str) or FIX_REQUEST_PATTERN.search(text) is None:
        return None

    test_path = _latest_shared_test_path(recent_context or [])
    aliases = _agent_aliases(agent_id, display_name)
    prior_blockers = _latest_self_blockers_line(recent_context or [], aliases)

    test_step = (
        f"call run_tests on {test_path}"
        if test_path
        else "call run_tests on the latest shared test file"
    )
    prior_note = (
        f" Your last status reply listed Blockers: {prior_blockers}."
        if prior_blockers
        else ""
    )

    return (
        "The operator is asking you to fix the prior blocker(s)."
        f"{prior_note}"
        " Do not emit a final answer that refuses for lack of context — first "
        f"{test_step} (or read_file on the relevant source) to recover the "
        "actual failure this turn, then make the fix in your scope and re-run "
        "run_tests. Your final answer must report either green tests with the "
        "change you made, or — if still red — the exact error from this turn's "
        "run_tests observation and the next concrete step."
    )


PROACTIVE_WRITE_VERB_PATTERN = re.compile(
    r"(?i)\b("
    r"create|build|implement|write|add|skapa|bygg(?:er|a)?|"
    r"implementera|skriv(?:a)?|lägg\s+till|distribute\s+roles|"
    r"share\s+code|collaborate"
    r")\b"
)


def _agent_already_engaged(
    agent_id: str,
    display_name: str,
    recent_context: Iterable[dict[str, str]],
) -> bool:
    """Return True if the agent already CLAIMed or accepted a task recently."""

    aliases = _agent_aliases(agent_id, display_name)
    for entry in recent_context or []:
        sender = str(entry.get("sender_id") or "").lower()
        if sender not in aliases and sender != display_name.lower():
            continue
        text = str(entry.get("text") or "")
        if CLAIM_PATTERN.search(text):
            return True
        status = parse_task_status(text)
        if status is not None and status.kind in {"taking", "accepted", "done"}:
            return True
    return False


def proactive_assignment_guidance(
    text: str,
    *,
    agent_id: str,
    display_name: str,
    recent_context: list[dict[str, str]] | None = None,
    has_open_claim: bool = False,
) -> str | None:
    """Nudge a SWE-role agent to volunteer for an unclaimed sub-task.

    Gated by `AGENT_PROACTIVE_SUBTASKS=1` because the system prompt's reply
    discipline ("let peers reply first") is intentionally cautious — turning
    this on trades that caution for a more active SWE agent in collaborative
    sessions. The hint only fires when:

      * the gate env var is set,
      * the broadcast contains a write/build verb (creating, implementing, …),
      * the agent has no active claim and has not recently posted a
        `Jag tar mig an:` / `Confirmed, I'll take:` style acceptance,
      * the message is not personally addressed (those already trigger the
        stricter assignment_guidance / followup_assignment_guidance helpers).

    Returns a single guidance string the runtime injects before the LLM call.
    """

    if os.environ.get("AGENT_PROACTIVE_SUBTASKS", "0") != "1":
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    if has_open_claim:
        return None
    if _mentions_current_agent(text, agent_id, display_name):
        return None
    if PROACTIVE_WRITE_VERB_PATTERN.search(text) is None:
        return None
    if _agent_already_engaged(agent_id, display_name, recent_context or []):
        return None
    return (
        "Proactivity hint: a multi-agent write task is in progress and you "
        "have not claimed a sub-task yet. If an unclaimed sub-task is "
        "visible, volunteer with one short line — `Jag tar mig an: <task>` "
        "or `I'm taking on: <task>` for remote-hub mode, or "
        "`CLAIM /workspace/shared/<path>#<scope>: <reason>` for the local "
        "hub — then make the actual write tool call. Do not post an empty "
        "acknowledgment; either commit to a slice or stay silent."
    )


def private_workspace_guidance(
    text: str,
    *,
    agent_id: str,
    display_name: str,
) -> str | None:
    """Return guidance when the operator explicitly names this agent's private
    workspace path. The system prompt nudges agents toward `/workspace/shared/`
    for joint work; this helper overrides that nudge when the operator picked
    a specific path under `/workspace/<this agent id>/...`.

    Returns `None` for shared paths and for other agents' private paths so the
    existing shared-coordination helpers stay authoritative there.
    """

    if not isinstance(text, str):
        return None
    agent_id_norm = (agent_id or "").lower()
    if not agent_id_norm or agent_id_norm == "shared":
        return None

    paths: list[str] = []
    for match in PRIVATE_WORKSPACE_PATH_PATTERN.finditer(text):
        if match.group("agent").lower() != agent_id_norm:
            continue
        path = match.group("path").rstrip(".,;:")
        if path not in paths:
            paths.append(path)

    if not paths:
        return None

    path_list = ", ".join(paths)
    return (
        f"Operator explicitly named your private workspace path: {path_list}. "
        "Write to that exact path — do not redirect to /workspace/shared/. "
        "You may still post `CLAIM <path>` for the registry, but private-"
        "workspace writes do not require shared coordination."
    )
