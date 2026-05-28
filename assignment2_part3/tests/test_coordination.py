from coordination import (
    assignment_guidance,
    fix_blockers_guidance,
    followup_assignment_guidance,
    handoff_guidance,
    parse_coordination_plan,
    parse_project_directive,
    private_workspace_guidance,
    proactive_assignment_guidance,
    project_name_from_shared_path,
    status_request_guidance,
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


def test_pytest_sidecar_warns_about_extending_imports_when_peers_present():
    guidance = assignment_guidance(
        "@bob-swe @alice-swe build a calculator in /workspace/shared/calculator.py. "
        "alice writes add+subtract, bob writes multiply+divide. Add pytest tests.",
        agent_id="bob",
        display_name="bob-swe",
    )

    assert guidance is not None
    assert "/workspace/shared/test_calculator.py" in guidance
    assert "replace_text" in guidance
    assert "import" in guidance
    assert "NameError" in guidance


def test_pytest_sidecar_skips_import_warning_when_solo():
    guidance = assignment_guidance(
        "@bob-swe build a calculator in /workspace/shared/calculator.py. "
        "bob writes add+subtract+multiply+divide. Add pytest tests next to it.",
        agent_id="bob",
        display_name="bob-swe",
    )

    assert guidance is not None
    assert "Pytest coverage was requested" in guidance
    assert "NameError" not in guidance


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


def test_status_request_guidance_matches_are_you_done():
    guidance = status_request_guidance(
        "@alice-swe are you done?",
        agent_id="alice",
        display_name="alice-swe",
    )

    assert guidance is not None
    assert "completion status" in guidance.lower()
    assert "Done:" in guidance
    assert "Tests:" in guidance
    assert "Blockers:" in guidance


def test_status_request_guidance_matches_short_status_question():
    guidance = status_request_guidance(
        "status?",
        agent_id="bob",
        display_name="bob-swe",
    )

    assert guidance is not None
    assert "Done:" in guidance


def test_status_request_guidance_skips_unrelated_chatter():
    assert (
        status_request_guidance(
            "let's pick up tomorrow",
            agent_id="alice",
            display_name="alice-swe",
        )
        is None
    )


def test_status_request_guidance_folds_open_claims_into_blockers():
    """When the operator asks for status while the agent holds unsatisfied
    claims, the guidance must steer the agent to list them in Blockers — not
    to RELEASE just to get the status reply out the door."""

    guidance = status_request_guidance(
        "@alice-swe are you done?",
        agent_id="alice",
        display_name="alice-swe",
        open_claim_targets=["/workspace/shared/calculator.py#add-subtract"],
    )

    assert guidance is not None
    assert "Blockers" in guidance
    assert "/workspace/shared/calculator.py#add-subtract" in guidance
    assert "unsatisfied CLAIM" in guidance
    # The whole point: do not push RELEASE just because status was requested.
    assert "instead of posting RELEASE" in guidance


def test_status_request_guidance_omits_blocker_hint_when_no_open_claims():
    guidance = status_request_guidance(
        "are you done?",
        agent_id="alice",
        display_name="alice-swe",
        open_claim_targets=None,
    )

    assert guidance is not None
    assert "unsatisfied CLAIM" not in guidance


def test_status_request_guidance_recommends_recent_test_path():
    guidance = status_request_guidance(
        "are you done?",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[
            {
                "sender_id": "alice-swe",
                "text": "CLAIM /workspace/shared/test_calculator.py#add-subtract-tests: Add pytest",
            }
        ],
    )

    assert guidance is not None
    assert "run_tests on /workspace/shared/test_calculator.py" in guidance


def test_status_request_guidance_falls_back_to_self_private_test_path():
    """In runpod-private mode the agent writes under /workspace/<agent_id>/...
    Status guidance must surface that path so the model has a real file to
    run_tests against instead of the generic "shared test file" hint."""

    guidance = status_request_guidance(
        "everyone are done?",
        agent_id="alice-swe",
        display_name="alice-swe",
        recent_context=[
            {
                "sender_id": "alice-swe",
                "text": (
                    "Klar med: add and subtract. "
                    "Files: /workspace/alice-swe/project4/calculator.py, "
                    "/workspace/alice-swe/project4/test_calculator.py. "
                    "Tests: 2 passed."
                ),
            }
        ],
    )

    assert guidance is not None
    assert (
        "run_tests on /workspace/alice-swe/project4/test_calculator.py"
        in guidance
    )


def test_status_request_guidance_only_matches_own_sender_id():
    """A peer's private test path must not leak into this agent's hint."""

    guidance = status_request_guidance(
        "everyone are done?",
        agent_id="alice-swe",
        display_name="alice-swe",
        recent_context=[
            {
                "sender_id": "bob-swe",
                "text": "Done with: multiply/divide at /workspace/bob-swe/project4/test_calculator.py",
            }
        ],
    )

    assert guidance is not None
    assert "/workspace/bob-swe/project4/test_calculator.py" not in guidance
    # Falls through to the generic hint when nothing of this agent's matches.
    assert "shared test file" in guidance


def test_status_request_guidance_done_question_mark_still_matches():
    """Pin the 'everyone are done?' wording so the trailing `done?` clause
    in STATUS_REQUEST_PATTERN keeps catching plural status broadcasts."""

    guidance = status_request_guidance(
        "everyone are done?",
        agent_id="alice-swe",
        display_name="alice-swe",
    )

    assert guidance is not None
    assert "completion status" in guidance.lower()


def test_fix_blockers_guidance_matches_can_you_fix():
    guidance = fix_blockers_guidance(
        "@alice-swe can you fix the blockers?",
        agent_id="alice",
        display_name="alice-swe",
    )

    assert guidance is not None
    assert "run_tests" in guidance
    assert "must report" in guidance
    assert "fix the prior blocker" in guidance


def test_fix_blockers_guidance_skips_unrelated_chatter():
    assert (
        fix_blockers_guidance(
            "tomorrow we ship",
            agent_id="alice",
            display_name="alice-swe",
        )
        is None
    )


def test_fix_blockers_guidance_includes_latest_test_path():
    guidance = fix_blockers_guidance(
        "@alice-swe please fix the failing tests",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[
            {
                "sender_id": "alice-swe",
                "text": "CLAIM /workspace/shared/test_calc.py#add-subtract-tests: Add pytest",
            }
        ],
    )

    assert guidance is not None
    assert "call run_tests on /workspace/shared/test_calc.py" in guidance


def test_fix_blockers_guidance_surfaces_prior_blockers_line():
    guidance = fix_blockers_guidance(
        "@alice-swe fix the blockers",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[
            {
                "sender_id": "alice-swe",
                "text": (
                    "Done: Implemented add and subtract in /workspace/shared/calculator.py. "
                    "Tests: ran and failed. "
                    "Blockers: NameError on add/subtract — missing import in test_calculator.py."
                ),
            }
        ],
    )

    assert guidance is not None
    assert "Your last status reply listed Blockers:" in guidance
    assert "NameError on add/subtract" in guidance


def test_fix_blockers_guidance_forbids_refuse_only_finals():
    guidance = fix_blockers_guidance(
        "@bob-swe make the tests pass",
        agent_id="bob",
        display_name="bob-swe",
    )

    assert guidance is not None
    assert "Do not emit a final answer that refuses for lack of context" in guidance


def test_fix_blockers_guidance_ignores_self_blockers_none():
    guidance = fix_blockers_guidance(
        "@alice-swe fix the blockers",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[
            {
                "sender_id": "alice-swe",
                "text": "Done: shipped. Tests: ran and passed. Blockers: none.",
            }
        ],
    )

    assert guidance is not None
    # 'none' is not a real blocker — don't surface it as the prior issue.
    assert "Your last status reply listed Blockers:" not in guidance


def test_private_workspace_guidance_matches_explicit_path():
    guidance = private_workspace_guidance(
        "@alice-swe build it in /workspace/alice/calc.py with add and subtract",
        agent_id="alice",
        display_name="alice-swe",
    )

    assert guidance is not None
    assert "/workspace/alice/calc.py" in guidance
    assert "do not redirect to /workspace/shared/" in guidance


def test_private_workspace_guidance_ignores_other_agents_paths():
    assert (
        private_workspace_guidance(
            "@alice-swe peek at /workspace/bob/foo.py",
            agent_id="alice",
            display_name="alice-swe",
        )
        is None
    )


def test_private_workspace_guidance_ignores_shared_path():
    assert (
        private_workspace_guidance(
            "@alice-swe write /workspace/shared/calculator.py",
            agent_id="alice",
            display_name="alice-swe",
        )
        is None
    )


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


def test_proactive_hint_disabled_by_default():
    g = proactive_assignment_guidance(
        "Distribute roles between each other and create a calculator.py",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[],
    )
    assert g is None


def test_proactive_hint_fires_when_env_set(monkeypatch):
    monkeypatch.setenv("AGENT_PROACTIVE_SUBTASKS", "1")
    g = proactive_assignment_guidance(
        "Distribute roles between each other and create a calculator.py",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[],
    )
    assert g is not None
    assert "Proactivity hint" in g


def test_proactive_hint_silent_when_agent_already_engaged(monkeypatch):
    monkeypatch.setenv("AGENT_PROACTIVE_SUBTASKS", "1")
    recent = [
        {"sender_id": "alice-swe", "text": "Jag tar mig an: divisionsdelen"},
    ]
    g = proactive_assignment_guidance(
        "Anyone else want to implement a sub-task?",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=recent,
    )
    assert g is None


def test_proactive_hint_silent_when_directly_addressed(monkeypatch):
    monkeypatch.setenv("AGENT_PROACTIVE_SUBTASKS", "1")
    g = proactive_assignment_guidance(
        "@alice-swe please implement add and subtract",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[],
    )
    assert g is None


def test_proactive_hint_silent_when_no_write_verb(monkeypatch):
    monkeypatch.setenv("AGENT_PROACTIVE_SUBTASKS", "1")
    g = proactive_assignment_guidance(
        "How is everyone doing today?",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[],
    )
    assert g is None


def test_proactive_hint_silent_when_has_open_claim(monkeypatch):
    monkeypatch.setenv("AGENT_PROACTIVE_SUBTASKS", "1")
    g = proactive_assignment_guidance(
        "implement calc.py please everyone",
        agent_id="alice",
        display_name="alice-swe",
        recent_context=[],
        has_open_claim=True,
    )
    assert g is None


# ----------------------------- PROJECT directive + shared-path project name


def test_parse_project_directive_picks_up_name():
    assert parse_project_directive("PROJECT: calc") == "calc"
    assert parse_project_directive("hello\nPROJECT: my-thing\nthen more") == "my-thing"


def test_parse_project_directive_returns_none_when_absent():
    assert parse_project_directive("just chatter, no directive") is None
    assert parse_project_directive("") is None
    assert parse_project_directive(None) is None  # type: ignore[arg-type]


def test_parse_project_directive_rejects_unsafe_chars():
    # Pattern only captures [A-Za-z0-9_-]+; punctuation/slashes break the match.
    assert parse_project_directive("PROJECT: bad/name") is None
    assert parse_project_directive("PROJECT: ..") is None


def test_project_name_from_shared_path_first_segment():
    assert project_name_from_shared_path(
        "/workspace/shared/calc/calculator.py"
    ) == "calc"


def test_project_name_from_shared_path_returns_none_for_flat_file():
    # No subfolder under /workspace/shared/ → no project to create.
    assert project_name_from_shared_path("/workspace/shared/calculator.py") is None


def test_project_name_from_shared_path_returns_none_for_non_shared():
    assert project_name_from_shared_path("/workspace/alice/calc/x.py") is None
    assert project_name_from_shared_path("/elsewhere/calc/x.py") is None
    assert project_name_from_shared_path("") is None
    assert project_name_from_shared_path(None) is None  # type: ignore[arg-type]


# ------------------------------ widened signature-agreement pattern


def test_signature_agreement_widened_phrasings_trigger_guidance():
    """Operator phrasings beyond strict 'agree on function signatures' fire."""
    phrasings = [
        "first, state agreement on signatures: add(a,b)",
        "propose signatures before writing",
        "confirm the signatures everyone",
        "agree on signatures first",
        "agree on the function signatures together",
    ]
    for phrase in phrasings:
        text = (
            f"@alice-swe @bob-swe build a calculator in /workspace/shared/calc/calculator.py. "
            f"{phrase}. alice owns add/subtract, bob owns multiply/divide."
        )
        guidance = assignment_guidance(
            text, agent_id="alice", display_name="alice-swe"
        )
        assert guidance is not None and "signature" in guidance.lower(), (
            f"phrase {phrase!r} should trigger signature-agreement guidance"
        )
