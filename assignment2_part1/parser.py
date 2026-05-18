from dataclasses import dataclass


@dataclass
class ParsedResponse:
    kind: str
    action: str | None = None
    command: str | None = None
    answer: str | None = None
    error: str | None = None


def _find_prefixed_line(lines, prefix):
    for i, line in enumerate(lines):
        clean = line.strip()
        if clean.startswith(prefix):
            return i, clean[len(prefix):].strip()
    return None, None


def parse_response(text):
    lines = text.strip().splitlines()

    if not lines:
        return ParsedResponse(kind="invalid", error="empty response")

    first_line = lines[0].strip()
    if not first_line.startswith("Thought:") or not first_line[len("Thought:"):].strip():
        return ParsedResponse(kind="invalid", error="missing Thought")

    final_idx, first_answer_line = _find_prefixed_line(lines, "Final Answer:")

    action_line_number = None
    action_name = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Action:"):
            action_name = line.strip()[len("Action:"):].strip()
            action_line_number = i
            break

    command_line_number = None
    command = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Command:"):
            command = line.strip()[len("Command:"):].strip()
            command_line_number = i
            break

    # shouldn't have both at once
    if final_idx is not None and action_line_number is not None:
        return ParsedResponse(kind="invalid", error="use Final Answer or Action: bash, not both")

    if final_idx is not None:
        answer_parts = [first_answer_line]
        answer_parts.extend(lines[final_idx + 1:])
        answer = "\n".join(answer_parts).strip()
        if answer:
            return ParsedResponse(kind="final", answer=answer)
        return ParsedResponse(kind="invalid", error="blank Final Answer")

    if action_line_number is None:
        return ParsedResponse(kind="invalid", error="expected Final Answer or Action: bash")

    if action_name != "bash":
        return ParsedResponse(kind="invalid", error="only 'Action: bash' is supported")

    if command_line_number is None:
        return ParsedResponse(kind="invalid", error="Action: bash with no Command line")

    if not command:
        return ParsedResponse(kind="invalid", error="Command is empty")

    return ParsedResponse(kind="action", action="bash", command=command)
