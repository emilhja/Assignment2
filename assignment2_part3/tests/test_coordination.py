from coordination import (
    assignment_guidance,
    followup_assignment_guidance,
    handoff_guidance,
    parse_coordination_plan,
)


def test_parse_multi_agent_writes_scopes():
    plan = parse_coordination_plan(
        "@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: "
        "alice writes add+subtract, bob writes multiply + division"
    )

    assert plan is not None
    assert plan.path == "/workspace/shared/calculator.py"
    assert [(item.agent, item.task, item.scope) for item in plan.assignments] == [
        ("alice", "add+subtract", "add-subtract"),
        ("bob", "multiply + division", "multiply-division"),
    ]


def test_parse_multi_agent_owns_scopes():
    plan = parse_coordination_plan(
        "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. "
        "Agree on function signatures in chat first (one message each), then split: "
        "alice owns add/subtract, bob owns multiply/divide."
    )

    assert plan is not None
    assert plan.path == "/workspace/shared/calculator.py"
    assert [(item.agent, item.task, item.scope) for item in plan.assignments] == [
        ("alice", "add/subtract", "add-subtract"),
        ("bob", "multiply/divide", "multiply-divide"),
    ]


def test_parse_shared_path_when_sentence_has_no_space_after_path():
    plan = parse_coordination_plan(
        "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py.First, "
        "each state agreement on signatures. Then split work: alice owns add/subtract, "
        "bob owns multiply/divide."
    )

    assert plan is not None
    assert plan.path == "/workspace/shared/calculator.py"


def test_parse_coordination_plan_strips_scope_from_runtime_continuation_path():
    plan = parse_coordination_plan(
        "Continue the active shared-file claim you already posted. "
        "Active claim target: /workspace/shared/calculator.py#add-subtract. "
        "Original request: @bob-swe @alice-swe build a calculator in "
        "/workspace/shared/calculator.py.First, each state agreement on signatures. "
        "Then split work: alice owns add/subtract, bob owns multiply/divide."
    )

    assert plan is not None
    assert plan.path == "/workspace/shared/calculator.py"


def test_assignment_guidance_only_mentions_own_scope_for_bob():
    guidance = assignment_guidance(
        "@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: "
        "alice writes add+subtract, bob writes multiply + division",
        agent_id="bob",
        display_name="bob-swe",
    )

    assert guidance is not None
    assert "Your assigned work: multiply + division" in guidance
    assert "Required CLAIM target: /workspace/shared/calculator.py#multiply-division" in guidance
    assert "Do not claim or write another agent's assigned scope" in guidance
    assert "@alice -> add+subtract (#add-subtract)" in guidance


def test_assignment_guidance_for_owns_signature_request():
    guidance = assignment_guidance(
        "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. "
        "Agree on function signatures in chat first (one message each), then split: "
        "alice owns add/subtract, bob owns multiply/divide. Each emit a CLAIM with "
        "the function names in the scope (e.g. #add-subtract).",
        agent_id="bob",
        display_name="bob-swe",
    )

    assert guidance is not None
    assert "Your assigned work: multiply/divide" in guidance
    assert "Required CLAIM target: /workspace/shared/calculator.py#multiply-divide" in guidance
    assert "@alice -> add/subtract (#add-subtract)" in guidance
    assert "Function-signature agreement was requested" in guidance
    assert "do not wait indefinitely" in guidance.lower()
    assert "def multiply(a, b)" in guidance
    assert "def divide(a, b)" in guidance


def test_assignment_guidance_mentions_pytest_sidecar_scope():
    guidance = assignment_guidance(
        "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. "
        "alice owns add/subtract, bob owns multiply/divide. Write pytest tests next to it.",
        agent_id="bob",
        display_name="bob-swe",
    )

    assert guidance is not None
    assert "Pytest coverage was requested" in guidance
    assert "/workspace/shared/test_calculator.py#multiply-divide-tests" in guidance


def test_assignment_guidance_matches_display_name_prefix():
    guidance = assignment_guidance(
        "@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: "
        "alice-swe writes add+subtract, bob-swe writes multiply + division",
        agent_id="alice",
        display_name="alice-swe",
    )

    assert guidance is not None
    assert "Your assigned work: add+subtract" in guidance
    assert "/workspace/shared/calculator.py#add-subtract" in guidance


def test_handoff_guidance_uses_recent_claim_when_present():
    guidance = handoff_guidance(
        "@alice-swe can you take over from @bob-swe instead",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[
            {
                "sender_id": "bob-swe",
                "text": "CLAIM /workspace/shared/calculator.py#multiply-division: Adding ops",
            }
        ],
    )

    assert guidance is not None
    assert "Handoff request detected" in guidance
    assert "/workspace/shared/calculator.py#multiply-division" in guidance
    assert "RELEASE /workspace/shared/calculator.py#multiply-division" in guidance
    assert "CLAIM /workspace/shared/calculator.py#multiply-division" in guidance


def test_followup_assignment_guidance_uses_recent_shared_path():
    guidance = followup_assignment_guidance(
        "@bob-swe you also write multiply + division",
        agent_id="bob",
        display_name="bob-swe",
        recent_context=[
            {
                "sender_id": "emil-user",
                "text": (
                    "@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: "
                    "alice writes add+subtract"
                ),
            }
        ],
    )

    assert guidance is not None
    assert "Coordinator follow-up assignment detected" in guidance
    assert "Your assigned work: multiply + division" in guidance
    assert "/workspace/shared/calculator.py#multiply-division" in guidance


def test_followup_assignment_guidance_infers_pytest_file_to_fix():
    guidance = followup_assignment_guidance(
        "@bob-swe can you fix the pytest",
        agent_id="bob",
        display_name="bob-swe",
        recent_context=[
            {
                "sender_id": "emil-user",
                "text": (
                    "@alice-swe and @bob-swe build a calculator in "
                    "/workspace/shared/calculator.py. Write pytest tests next to it."
                ),
            },
            {
                "sender_id": "bob-swe",
                "text": "CLAIM /workspace/shared/test_calculator.py#multiply-divide-tests: Add tests",
            },
        ],
    )

    assert guidance is not None
    assert "Pytest follow-up detected" in guidance
    assert "Shared test path: /workspace/shared/test_calculator.py" in guidance
    assert "call read_file" in guidance


def test_handoff_guidance_falls_back_to_recent_assignment():
    guidance = handoff_guidance(
        "@alice-swe can you take over from @bob-swe instead",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[
            {
                "sender_id": "emil-user",
                "text": (
                    "@alice-swe and @bob-swe collaborate on /workspace/shared/calculator.py: "
                    "alice writes add+subtract, bob writes multiply + division"
                ),
            }
        ],
    )

    assert guidance is not None
    assert "/workspace/shared/calculator.py#multiply-division" in guidance
