"""Parser for the simple Thought/Action/Command/Final Answer format."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedResponse:
    """Structured result from one assistant message."""

    kind: str
    action: str | None = None
    command: str | None = None
    answer: str | None = None
    error: str | None = None


def parse_response(text: str) -> ParsedResponse:
    """Turn assistant text into a final answer, Bash command, or error."""

    stripped = text.strip()
    # A valid reply must start with Thought: and include text after it.
    if not re.match(r"(?is)^Thought:\s*\S+", stripped):
        return ParsedResponse(kind="invalid", error="Response must begin with non-empty 'Thought:'.")

    # The reply can give a final answer or request a command, but not both.
    has_final = re.search(r"(?im)^\s*Final Answer:", stripped) is not None
    has_action = re.search(r"(?im)^\s*Action:", stripped) is not None
    if has_final and has_action:
        return ParsedResponse(
            kind="invalid",
            error="Use either 'Final Answer:' or 'Action: bash', not both.",
        )

    # Final Answer: must include actual answer text.
    final_match = re.search(r"(?is)^\s*Final Answer:\s*(.*)", stripped, flags=re.MULTILINE)
    if final_match:
        answer = final_match.group(1).strip()
        if answer:
            return ParsedResponse(kind="final", answer=answer)
        return ParsedResponse(kind="invalid", error="Final Answer was present but empty.")

    # Action: must only say bash; the shell command belongs on Command:.
    action_line_match = re.search(r"(?im)^\s*Action:\s*(.*)$", stripped)
    if action_line_match and action_line_match.group(1).strip() != "bash":
        return ParsedResponse(
            kind="invalid",
            error="Action line must be exactly 'Action: bash'. Put the command on the Command line.",
        )

    # If there is no final answer, the reply must request Action: bash.
    action_match = re.search(r"(?im)^\s*Action:\s*bash\s*$", stripped)
    if not action_match:
        return ParsedResponse(
            kind="invalid",
            error="Expected either 'Final Answer:' or 'Action: bash' with 'Command: ...'.",
        )

    command_match = re.search(r"(?im)^\s*Command:\s*(.*)$", stripped)
    if not command_match:
        return ParsedResponse(kind="invalid", error="Action was bash but no Command was found.")

    command = command_match.group(1).strip()
    if not command:
        return ParsedResponse(kind="invalid", error="Command was present but empty.")
    if len(command) >= 2 and command[0] == command[-1] and command[0] in {"'", '"'}:
        # Do not accept one quoted command string; commands should be raw shell text.
        return ParsedResponse(
            kind="invalid",
            error="Command must not be wrapped as one quoted shell string.",
        )

    return ParsedResponse(kind="action", action="bash", command=command)
