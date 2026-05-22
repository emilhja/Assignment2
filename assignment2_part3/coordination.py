"""Deterministic coordination hints for common group-chat task shapes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from claims import CLAIM_PATTERN, split_claim_target


SHARED_PATH_PATTERN = re.compile(r"(?P<path>/workspace/shared/[^\s:;,]+)")
WRITES_PATTERN = re.compile(
    r"(?i)\b(?P<agent>[A-Za-z0-9_.-]+)\s+writes?\s+(?P<task>[^,.;\n]+)"
)
YOU_WRITES_PATTERN = re.compile(r"(?i)\byou\s+(?:also\s+)?writes?\s+(?P<task>[^,.;\n]+)")
TAKEOVER_PATTERN = re.compile(
    r"(?i)\b(?:take\s+over|handoff|hand\s+off)\b(?:\s+from\s+@?(?P<peer>[A-Za-z0-9_.-]+))?"
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


def parse_coordination_plan(text: str) -> CoordinationPlan | None:
    """Parse simple coordinator messages: shared path + "alice writes X" slices."""

    if not isinstance(text, str):
        return None
    path_match = SHARED_PATH_PATTERN.search(text)
    if not path_match:
        return None

    assignments: list[Assignment] = []
    for match in WRITES_PATTERN.finditer(text):
        agent = match.group("agent").strip().lstrip("@")
        task = match.group("task").strip()
        if not agent or not task:
            continue
        assignments.append(Assignment(agent=agent, task=task, scope=_scope_from_task(task)))

    if not assignments:
        return None
    return CoordinationPlan(path=path_match.group("path").rstrip("."), assignments=tuple(assignments))


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
        return None

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
