from parser import parse_response


def test_parse_tool_call_json():
    result = parse_response(
        '{"type":"tool_call","tool":"bash","args":{"command":"ls -la /workspace"},"reason":"inspect"}',
        allowed_tools={"bash"},
    )

    assert result.kind == "tool_call"
    assert result.tool == "bash"
    assert result.args == {"command": "ls -la /workspace"}
    assert result.reason == "inspect"


def test_parse_final_json():
    result = parse_response('{"type":"final","answer":"Done"}')

    assert result.kind == "final"
    assert result.answer == "Done"


def test_rejects_malformed_json():
    result = parse_response("Thought: I know the answer")

    assert result.kind == "invalid"
    assert "valid JSON" in result.error


def test_rejects_non_object_json():
    result = parse_response('["not", "an", "object"]')

    assert result.kind == "invalid"


def test_rejects_unknown_tool():
    result = parse_response(
        '{"type":"tool_call","tool":"unknown","args":{}}',
        allowed_tools={"bash"},
    )

    assert result.kind == "invalid"
    assert "Unknown tool" in result.error


def test_rejects_missing_args_object():
    result = parse_response('{"type":"tool_call","tool":"bash"}', allowed_tools={"bash"})

    assert result.kind == "invalid"
    assert "args object" in result.error


def test_rejects_final_with_tool_fields():
    result = parse_response('{"type":"final","answer":"Done","tool":"bash","args":{}}')

    assert result.kind == "invalid"


def test_rejects_tool_call_with_answer_field():
    result = parse_response(
        '{"type":"tool_call","tool":"bash","args":{"command":"pwd"},"answer":"Done"}',
        allowed_tools={"bash"},
    )

    assert result.kind == "invalid"
