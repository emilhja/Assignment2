"""Deterministic coordination hints for common group-chat task shapes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from claims import CLAIM_PATTERN, split_claim_target


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
    r"(?i)\bagree\s+on\s+(?:the\s+)?function\s+signatures?\b"
)
PYTEST_REQUEST_PATTERN = re.compile(r"(?i)\b(?:pytest|tests?)\b")
PYTEST_FIX_PATTERN = re.compile(r"(?i)\b(?:fix|repair|update|debug)\b.*\b(?:pytest|tests?)\b")
SHARED_TEST_PATH_PATTERN = re.compile(r"(?P<path>/workspace/shared/(?:test_[^\s:;,]+|[^\s:;,]+_test)\.py)")


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


def _pytest_sidecar_guidance(text: str, path: str, own: Assignment) -> str:
    if PYTEST_REQUEST_PATTERN.search(text) is None:
        return ""
    test_path = _test_path_for_source(path)
    if not test_path:
        return ""
    return (
        " Pytest coverage was requested next to the shared file. After completing "
        f"the implementation write, use a separate CLAIM for {test_path}#{own.scope}-tests "
        "before creating or editing tests for your scope."
    )


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
    return (
        "Coordinator assignment detected. "
        f"Shared path: {plan.path}. "
        f"Your assigned work: {own.task}. "
        f"Required CLAIM target: {claim_target}. "
        f"Other assigned scopes: {peers}. "
        "Do not claim or write another agent's assigned scope."
        f"{_signature_agreement_guidance(text, own)}"
        f"{_pytest_sidecar_guidance(text, plan.path, own)}"
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
