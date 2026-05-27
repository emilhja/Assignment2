from dataclasses import dataclass


THOUGHT = "Thought:"
ACTION = "Action:"
COMMAND = "Command:"
FINAL = "Final Answer:"


@dataclass
class ParsedResponse:
    kind: str
    action: str | None = None
    command: str | None = None
    answer: str | None = None
    error: str | None = None


def _prefixed_line(lines: list[str], prefix: str) -> tuple[int | None, str | None]:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(prefix):
            return index, stripped[len(prefix) :].strip()
    return None, None


def parse_model_response(text: str) -> ParsedResponse:
    """Parse the raw text protocol used by the Part 1 agent."""
    stripped = text.strip()
    if not stripped:
        return ParsedResponse(kind="invalid", error="empty response")
    if "```" in stripped:
        return ParsedResponse(kind="invalid", error="do not use markdown fences")
    if stripped.startswith("{") or stripped.startswith("["):
        return ParsedResponse(kind="invalid", error="do not use JSON")

    lines = stripped.splitlines()
    first = lines[0].strip()
    if not first.startswith(THOUGHT) or not first[len(THOUGHT) :].strip():
        return ParsedResponse(kind="invalid", error="missing Thought line")

    final_index, final_text = _prefixed_line(lines, FINAL)
    action_index, action_name = _prefixed_line(lines, ACTION)
    command_index, command = _prefixed_line(lines, COMMAND)

    if final_index is not None and action_index is not None:
        return ParsedResponse(kind="invalid", error="use either Final Answer or Action, not both")

    if final_index is not None:
        answer_lines = [final_text or ""]
        answer_lines.extend(lines[final_index + 1 :])
        answer = "\n".join(answer_lines).strip()
        if not answer:
            return ParsedResponse(kind="invalid", error="blank Final Answer")
        return ParsedResponse(kind="final", answer=answer)

    if action_index is None:
        return ParsedResponse(kind="invalid", error="expected Final Answer or Action")
    if action_name != "bash":
        return ParsedResponse(kind="invalid", error="only Action: bash is supported")
    if command_index is None:
        return ParsedResponse(kind="invalid", error="Action: bash requires Command")
    if command_index < action_index:
        return ParsedResponse(kind="invalid", error="Command must follow Action")
    if not command:
        return ParsedResponse(kind="invalid", error="empty Command")
    if len(command) >= 2 and command[0] == command[-1] and command[0] in {"'", '"'}:
        return ParsedResponse(kind="invalid", error="write raw shell text, not one quoted string")

    return ParsedResponse(kind="action", action="bash", command=command)


def format_protocol_guidance(error: str | None = None) -> str:
    message = (
        "Your response did not match the required raw text protocol.\n\n"
        "Use exactly one of these formats:\n\n"
        "Thought: <brief reason>\n"
        "Action: bash\n"
        "Command: <one safe local bash command>\n\n"
        "or:\n\n"
        "Thought: <brief reason>\n"
        "Final Answer: <answer>\n\n"
        "Do not use JSON, markdown fences, or built-in tool syntax."
    )
    if error:
        message += f"\nParser error: {error}"
    return message
