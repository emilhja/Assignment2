import json

from tools import MAX_OUTPUT_CHARS, TOOL_REGISTRY


def json_dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def truncate_observation(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [output truncated]"


def tool_observation_message(tool: str, observation: str) -> str:
    return json_dump({"type": "tool_observation", "tool": tool, "observation": observation})


def invalid_response_guidance(error: str | None) -> str:
    tool_payload = {
        "type": "tool_call",
        "tool": "bash",
        "args": {"command": "pwd"},
        "reason": "brief reason",
    }
    final_payload = {"type": "final", "answer": "answer to the user"}
    guidance = (
        "Your previous response was invalid. Respond with exactly one JSON object and no prose.\n"
        f"Valid tool-call example: {json_dump(tool_payload)}\n"
        f"Valid final-answer example: {json_dump(final_payload)}"
    )
    if error:
        guidance += f"\nParser error: {error}"
    return guidance


def workspace_mutation_tools() -> frozenset[str]:
    return frozenset(
        name for name, spec in TOOL_REGISTRY.items() if spec.mutates_workspace
    )


def tool_succeeded(tool: str, observation: str) -> bool:
    spec = TOOL_REGISTRY.get(tool)
    if spec is None or not spec.mutates_workspace:
        return False
    return bool(spec.success_prefixes) and observation.startswith(spec.success_prefixes)
