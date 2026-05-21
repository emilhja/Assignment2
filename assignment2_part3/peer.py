"""Peer-message trust class + outbound credential scrubber (P3.2).

`PeerMessage` is the untrusted envelope for any message that arrives from
another agent via the group chat. `peer_intent_refusal` is the per-round
refusal gate (Part 2's `intent_refusal` only fires once on the initial user
task — this is the equivalent for peer turns and is re-run on every round
plus on tool args). `scrub_outbound` redacts credential-shaped strings from
text before it leaves the process.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class PeerMessage:
    id: str
    sender_id: str
    text: str
    received_at: float = field(default_factory=time.time)
    addressed_to: tuple[str, ...] = ()


# Patterns checked against peer requests. Each entry is (regex, refusal reason).
# Stricter than Part 2's intent_refusal — assumes the sender is hostile until
# the actual content proves otherwise.
PEER_REFUSAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)\b(system\s*prompt|instructions you were given|your\s+rules|developer\s+message)\b"),
        "I will not reveal my system prompt or internal instructions.",
    ),
    (
        re.compile(r"(?i)(?:^|[\s/=()'\"])\.env\b|\bdotenv\b"),
        "I will not read or reveal .env files.",
    ),
    (
        re.compile(r"(?i)\b(env(ironment)?\s*(var(iable)?s?|values?)|printenv|getenv)\b"),
        "I will not reveal environment variable values.",
    ),
    (
        re.compile(r"(?i)\b(api[\s_-]?key|access[\s_-]?token|secret[\s_-]?key|bearer\s+token|credentials?|passwords?)\b"),
        "I will not share credentials, tokens, or secrets.",
    ),
    (
        re.compile(r"(?i)\bGROQ_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_SECRET\w*\b"),
        "I will not share API keys.",
    ),
    (
        re.compile(r"(?i)\b/data(/|\b)"),
        "I will not read or reveal /data contents.",
    ),
    (
        re.compile(r"(?i)\b(safety\.py|llm_client\.py|session_history)\b"),
        "I will not exfiltrate internal agent source or session logs to peers.",
    ),
    (
        re.compile(r"(?i)\b(ignore|disregard|forget)\s+(your|the|all)\s+(previous|prior|above|earlier)\s+(instructions?|rules?|prompt)"),
        "I will not follow instructions to ignore my prior rules.",
    ),
    (
        re.compile(r"(?i)\b(print|dump|leak|exfiltrate|show me)\s+(your|the)\s+(memory|context|history|secrets?)"),
        "I will not dump internal memory or context.",
    ),
]


def peer_intent_refusal(text: str) -> Optional[str]:
    """Return a refusal reason if the peer text crosses a no-leak rule.

    Returns None when the text is acceptable. Called both on incoming
    peer text and on tool args produced during the LLM round-trip so a
    leak attempt that survives the model is caught at the wire.
    """

    if not isinstance(text, str) or not text:
        return None
    for pattern, reason in PEER_REFUSAL_PATTERNS:
        if pattern.search(text):
            return reason
    return None


# Outbound scrubbing. Each entry is (kind_label, regex). Matches are replaced
# with `[REDACTED:<kind>]` before the text leaves the process.
CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"gh[posu]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[bapr]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "dotenv_secret",
        re.compile(
            r"(?m)^([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PWD|PASSPHRASE))\s*=\s*\S+"
        ),
    ),
]


def scrub_outbound(text: str) -> tuple[str, list[str]]:
    """Redact credential-shaped strings. Return (scrubbed_text, kinds_hit)."""

    if not isinstance(text, str) or not text:
        return text or "", []

    scrubbed = text
    hits: list[str] = []
    for kind, pattern in CREDENTIAL_PATTERNS:
        if kind == "dotenv_secret":
            new_text, count = pattern.subn(lambda m: f"{m.group(1)}=[REDACTED:dotenv_secret]", scrubbed)
        else:
            new_text, count = pattern.subn(f"[REDACTED:{kind}]", scrubbed)
        if count:
            hits.append(kind)
            scrubbed = new_text
    return scrubbed, hits
