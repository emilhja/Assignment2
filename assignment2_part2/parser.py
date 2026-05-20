import json


class ParsedResponse:
    def __init__(self, kind, tool=None, args=None, answer=None, reason=None, error=None):
        self.kind = kind
        self.tool = tool
        self.args = args or {}
        self.answer = answer
        self.reason = reason
        self.error = error


def parse_response(text, allowed_tools=None):
    """Parse one structured model reply into a final answer or tool call."""

    stripped = text.strip()
    if not stripped:
        return ParsedResponse(kind="invalid", error="The reply was empty.")

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return ParsedResponse(kind="invalid", error=f"The reply was not valid JSON: {exc.msg}.")

    if not isinstance(payload, dict):
        return ParsedResponse(kind="invalid", error="The JSON reply must be an object.")

    response_type = payload.get("type")
    if response_type == "final":
        if "tool" in payload or "args" in payload:
            return ParsedResponse(kind="invalid", error="A final answer must not include tool fields.")
        answer = payload.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return ParsedResponse(kind="invalid", error="Final replies need a non-empty string answer.")
        return ParsedResponse(kind="final", answer=answer.strip())

    if response_type == "tool_call":
        if "answer" in payload:
            return ParsedResponse(kind="invalid", error="A tool call must not include an answer field.")
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            return ParsedResponse(kind="invalid", error="Tool calls need a non-empty string tool name.")
        tool = tool.strip()
        if allowed_tools is not None and tool not in allowed_tools:
            return ParsedResponse(kind="invalid", error=f"Unknown tool: {tool}.")
        args = payload.get("args")
        if not isinstance(args, dict):
            return ParsedResponse(kind="invalid", error="Tool calls need an args object.")
        reason = payload.get("reason", "")
        if reason is not None and not isinstance(reason, str):
            return ParsedResponse(kind="invalid", error="Tool call reason must be a string when present.")
        return ParsedResponse(kind="tool_call", tool=tool, args=args, reason=(reason or "").strip())

    return ParsedResponse(kind="invalid", error="JSON field 'type' must be either 'final' or 'tool_call'.")
