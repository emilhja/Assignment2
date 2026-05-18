class ParsedResponse:
    def __init__(self, kind, action=None, command=None, answer=None, error=None):
        self.kind = kind
        self.action = action
        self.command = command
        self.answer = answer
        self.error = error


def parse_response(text):
    stripped = text.strip()
    lines = stripped.splitlines()

    if not lines:
        return ParsedResponse(kind="invalid", error="Response was empty.")

    first_line = lines[0].strip()
    if not first_line.startswith("Thought:") or not first_line[len("Thought:") :].strip():
        return ParsedResponse(kind="invalid", error="Response must begin with non-empty 'Thought:'.")

    has_final = False
    has_action = False
    final_line_number = None
    action_line_number = None
    command_line_number = None

    for line_number, line in enumerate(lines):
        clean_line = line.strip()
        if clean_line.startswith("Final Answer:"):
            has_final = True
            if final_line_number is None:
                final_line_number = line_number
        if clean_line.startswith("Action:"):
            has_action = True
            if action_line_number is None:
                action_line_number = line_number
        if clean_line.startswith("Command:") and command_line_number is None:
            command_line_number = line_number

    if has_final and has_action:
        return ParsedResponse(
            kind="invalid",
            error="Use either 'Final Answer:' or 'Action: bash', not both.",
        )

    if final_line_number is not None:
        final_line = lines[final_line_number].strip()
        answer_parts = [final_line[len("Final Answer:") :].strip()]
        answer_parts.extend(lines[final_line_number + 1 :])
        answer = "\n".join(answer_parts).strip()
        if answer:
            return ParsedResponse(kind="final", answer=answer)
        return ParsedResponse(kind="invalid", error="Final Answer was present but empty.")

    if action_line_number is None:
        return ParsedResponse(
            kind="invalid",
            error="Expected either 'Final Answer:' or 'Action: bash' with 'Command: ...'.",
        )

    action_line = lines[action_line_number].strip()
    action_name = action_line[len("Action:") :].strip()
    if action_name != "bash":
        return ParsedResponse(
            kind="invalid",
            error="Action line must be exactly 'Action: bash'. Put the command on the Command line.",
        )

    if command_line_number is None:
        return ParsedResponse(kind="invalid", error="Action was bash but no Command was found.")

    command_line = lines[command_line_number].strip()
    command = command_line[len("Command:") :].strip()
    if not command:
        return ParsedResponse(kind="invalid", error="Command was present but empty.")
    if len(command) >= 2 and command[0] == command[-1] and command[0] in {"'", '"'}:
        return ParsedResponse(
            kind="invalid",
            error="Command must not be wrapped as one quoted shell string.",
        )

    return ParsedResponse(kind="action", action="bash", command=command)
