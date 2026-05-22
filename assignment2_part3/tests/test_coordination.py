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
