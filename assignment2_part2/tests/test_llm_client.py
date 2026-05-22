from types import SimpleNamespace

import pytest

import llm_client


def _response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _response(outcome)


def _client_with(outcomes):
    completions = FakeCompletions(outcomes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


class FakeProviderError(Exception):
    def __init__(self, body):
        super().__init__(f"Error code: 400 - {body}")
        self.body = body


class FakeRateLimitError(Exception):
    pass


def _rate_limit_error(message="rate limit", retry_after=None):
    exc = FakeRateLimitError(message)
    if retry_after is not None:
        exc.response = SimpleNamespace(headers={"Retry-After": str(retry_after)})
    return exc


def _use_openai_provider(monkeypatch, client):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)


def test_json_mode_is_sent_on_first_request(monkeypatch):
    client, completions = _client_with(["ok"])
    _use_openai_provider(monkeypatch, client)

    assert llm_client.complete_chat([{"role": "user", "content": "Return JSON"}]) == "ok"

    assert completions.calls == [
        {
            "model": llm_client.PROVIDERS["openai"]["default_model"],
            "messages": [{"role": "user", "content": "Return JSON"}],
            "response_format": {"type": "json_object"},
        }
    ]


def test_json_mode_success_uses_returned_content(monkeypatch):
    client, _completions = _client_with(['{"type":"final","answer":"done"}'])
    _use_openai_provider(monkeypatch, client)

    content = llm_client.complete_chat([{"role": "user", "content": "Return JSON"}])

    assert content == '{"type":"final","answer":"done"}'


def test_json_mode_rejection_retries_same_provider_without_response_format(monkeypatch):
    client, completions = _client_with(
        [
            RuntimeError("400 unsupported response_format json_object"),
            "plain ok",
        ]
    )
    _use_openai_provider(monkeypatch, client)

    assert llm_client.complete_chat([{"role": "user", "content": "Return JSON"}]) == "plain ok"

    assert len(completions.calls) == 2
    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in completions.calls[1]
    assert completions.calls[1]["messages"] == [{"role": "user", "content": "Return JSON"}]


def test_groq_failed_generation_tool_call_is_recovered_from_error_body(monkeypatch):
    body = {
        "error": {
            "message": "Tool choice is none, but model called a tool",
            "type": "invalid_request_error",
            "code": "tool_use_failed",
            "failed_generation": (
                '{"name":"tool_call_bash","arguments":{"type":"tool_call","tool":"bash",'
                '"args":{"command":"ls -la /workspace"},"reason":"list files"}}'
            ),
        }
    }
    client, completions = _client_with([FakeProviderError(body)])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    content = llm_client.complete_chat([{"role": "user", "content": "list all files"}])

    assert content == (
        '{"type": "tool_call", "tool": "bash", '
        '"args": {"command": "ls -la /workspace"}, "reason": "list files"}'
    )
    assert len(completions.calls) == 1


def test_groq_failed_generation_tool_call_is_recovered_from_error_text(monkeypatch):
    body = {
        "error": {
            "message": "Tool choice is none, but model called a tool",
            "type": "invalid_request_error",
            "code": "tool_use_failed",
            "failed_generation": '{"type":"final","answer":"done"}',
        }
    }
    client, _completions = _client_with([RuntimeError(f"Error code: 400 - {body}")])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    assert llm_client.complete_chat([{"role": "user", "content": "finish"}]) == (
        '{"type": "final", "answer": "done"}'
    )


def test_groq_failed_generation_tool_call_is_recovered_from_inner_error_body(monkeypatch):
    body = {
        "message": "Tool choice is none, but model called a tool",
        "type": "invalid_request_error",
        "code": "tool_use_failed",
        "failed_generation": (
            '{"name": "tool_call", "arguments": {"type":"tool_call","tool":"bash",'
            '"args":{"command":"ls -la /workspace"},"reason":"list workspace files"}}'
        ),
    }
    client, _completions = _client_with([FakeProviderError(body)])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    assert llm_client.complete_chat([{"role": "user", "content": "list all files"}]) == (
        '{"type": "tool_call", "tool": "bash", '
        '"args": {"command": "ls -la /workspace"}, "reason": "list workspace files"}'
    )


def test_groq_failed_generation_native_replace_text_tool_is_recovered(monkeypatch):
    body = {
        "error": {
            "message": "Tool choice is none, but model called a tool",
            "type": "invalid_request_error",
            "code": "tool_use_failed",
            "failed_generation": (
                '{"name": "replace_text", "arguments": {"path":"/workspace/demo.txt",'
                '"old_text":"done","new_text":"draft","all_occurrences":false}}'
            ),
        }
    }
    client, _completions = _client_with([FakeProviderError(body)])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    assert llm_client.complete_chat([{"role": "user", "content": "change done to draft"}]) == (
        '{"type": "tool_call", "tool": "replace_text", '
        '"args": {"path": "/workspace/demo.txt", "old_text": "done", '
        '"new_text": "draft", "all_occurrences": false}, '
        '"reason": "recovered provider tool call"}'
    )


def test_groq_failed_generation_native_create_file_tool_is_recovered(monkeypatch):
    body = {
        "error": {
            "message": "Tool choice is none, but model called a tool",
            "type": "invalid_request_error",
            "code": "tool_use_failed",
            "failed_generation": (
                '{"name": "create_file", "arguments": {"path":"/workspace/hello.txt",'
                '"content":"Hello world!","overwrite":false}}'
            ),
        }
    }
    client, _completions = _client_with([FakeProviderError(body)])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    assert llm_client.complete_chat([{"role": "user", "content": "create hello.txt"}]) == (
        '{"type": "tool_call", "tool": "create_file", '
        '"args": {"path": "/workspace/hello.txt", "content": "Hello world!", '
        '"overwrite": false}, "reason": "recovered provider tool call"}'
    )


def test_groq_failed_generation_bash_tool_arguments_are_recovered(monkeypatch):
    body = {
        "error": {
            "message": "Tool choice is none, but model called a tool",
            "type": "invalid_request_error",
            "code": "tool_use_failed",
            "failed_generation": (
                '{"name": "tool_call_bash", '
                '"arguments": {"command":"cat /workspace/demo.txt"}}'
            ),
        }
    }
    client, _completions = _client_with([FakeProviderError(body)])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    assert llm_client.complete_chat([{"role": "user", "content": "read demo.txt"}]) == (
        '{"type": "tool_call", "tool": "bash", '
        '"args": {"command": "cat /workspace/demo.txt"}, '
        '"reason": "recovered provider tool call"}'
    )


def test_groq_failed_generation_tool_dot_bash_is_recovered(monkeypatch):
    body = {
        "error": {
            "message": "Tool choice is none, but model called a tool",
            "type": "invalid_request_error",
            "code": "tool_use_failed",
            "failed_generation": (
                '{"name": "tool.bash", '
                '"arguments": {"command":"ls -la /workspace"}}'
            ),
        }
    }
    client, _completions = _client_with([FakeProviderError(body)])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    assert llm_client.complete_chat([{"role": "user", "content": "list files"}]) == (
        '{"type": "tool_call", "tool": "bash", '
        '"args": {"command": "ls -la /workspace"}, '
        '"reason": "recovered provider tool call"}'
    )


def test_groq_failed_generation_prose_bash_tool_is_recovered(monkeypatch):
    body = {
        "error": {
            "message": "Parsing failed. See failed_generation.",
            "type": "invalid_request_error",
            "code": "output_parse_failed",
            "failed_generation": (
                "We need to read demo.txt. Should use bash tool: "
                "cat /workspace/demo.txt."
            ),
        }
    }
    client, _completions = _client_with([FakeProviderError(body)])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    assert llm_client.complete_chat([{"role": "user", "content": "read demo.txt"}]) == (
        '{"type": "tool_call", "tool": "bash", '
        '"args": {"command": "cat /workspace/demo.txt"}, '
        '"reason": "recovered provider tool call"}'
    )


def test_groq_failed_generation_prose_replace_text_is_recovered(monkeypatch):
    body = {
        "error": {
            "message": "Parsing failed. See failed_generation.",
            "type": "invalid_request_error",
            "code": "output_parse_failed",
            "failed_generation": (
                'We need to edit file /workspace/demo.txt. Replace "done" to "draft". '
                "We use replace_text."
            ),
        }
    }
    client, _completions = _client_with([FakeProviderError(body)])

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_client_for_provider", lambda _config: client)

    assert llm_client.complete_chat([{"role": "user", "content": "change done to draft"}]) == (
        '{"type": "tool_call", "tool": "replace_text", '
        '"args": {"path": "/workspace/demo.txt", "old_text": "done", '
        '"new_text": "draft", "all_occurrences": false}, '
        '"reason": "recovered provider tool call"}'
    )


def test_plain_retry_error_is_reported_if_both_calls_fail(monkeypatch):
    client, _completions = _client_with(
        [
            RuntimeError("400 unsupported response_format json_object"),
            RuntimeError("plain failed"),
        ]
    )
    _use_openai_provider(monkeypatch, client)

    with pytest.raises(RuntimeError) as excinfo:
        llm_client.complete_chat([{"role": "user", "content": "Return JSON"}])

    message = str(excinfo.value)
    assert "I could not get a reply from any LLM provider" in message
    assert "plain failed" in message
    assert "unsupported response_format" not in message


def test_provider_fallback_still_works_after_plain_retry_fails(monkeypatch):
    groq_client, groq_completions = _client_with(
        [
            RuntimeError("400 unsupported response_format json_object"),
            RuntimeError("plain failed"),
        ]
    )
    openai_client, openai_completions = _client_with(["openai ok"])
    clients = {
        "GROQ_API_KEY": groq_client,
        "OPENAI_API_KEY": openai_client,
    }

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq,openai")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(
        llm_client,
        "_client_for_provider",
        lambda config: clients[config["api_key_env"]],
    )

    assert llm_client.complete_chat([{"role": "user", "content": "Return JSON"}]) == "openai ok"

    assert len(groq_completions.calls) == 2
    assert len(openai_completions.calls) == 1
    assert openai_completions.calls[0]["response_format"] == {"type": "json_object"}


def test_rate_limited_provider_waits_and_eventually_returns(monkeypatch):
    client, completions = _client_with(
        [_rate_limit_error(retry_after=2) for _ in range(7)] + ["ok after wait"]
    )
    _use_openai_provider(monkeypatch, client)
    monkeypatch.setattr(llm_client, "RateLimitError", FakeRateLimitError)
    monkeypatch.setattr(llm_client.random, "uniform", lambda start, end: 0.25)
    sleeps = []
    monkeypatch.setattr(llm_client.time, "sleep", sleeps.append)

    assert llm_client.complete_chat([{"role": "user", "content": "Return JSON"}]) == "ok after wait"

    assert sleeps == [2.25] * 7
    assert len(completions.calls) == 8


def test_rate_limit_retry_wait_uses_groq_body_hint_plus_jitter(monkeypatch):
    exc = _rate_limit_error(
        "Error code: 429 - {'error': {'message': 'Rate limit ... "
        "Please try again in 11.64s. Need more tokens? ...'}}"
    )
    monkeypatch.setattr(llm_client.random, "uniform", lambda start, end: 0.36)

    assert llm_client._rate_limit_retry_wait(exc) == 12.5


def test_finite_rate_limit_wait_raises_clear_error_after_timeout(monkeypatch):
    client, completions = _client_with([
        _rate_limit_error(retry_after=3),
        _rate_limit_error(retry_after=3),
    ])
    _use_openai_provider(monkeypatch, client)
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_WAIT_SECONDS", "1")
    monkeypatch.setattr(llm_client, "RateLimitError", FakeRateLimitError)
    monkeypatch.setattr(llm_client.random, "uniform", lambda start, end: 0)
    sleeps = []
    monkeypatch.setattr(llm_client.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError) as excinfo:
        llm_client.complete_chat([{"role": "user", "content": "Return JSON"}])

    message = str(excinfo.value)
    assert "RateLimitRetryTimeout" in message
    assert "stayed rate-limited" in message
    assert sleeps == [1.0]
    assert len(completions.calls) == 2


def test_provider_fallback_still_works_for_non_rate_limit_errors(monkeypatch):
    groq_client, groq_completions = _client_with([RuntimeError("network unavailable")])
    openai_client, openai_completions = _client_with(["openai ok"])
    clients = {
        "GROQ_API_KEY": groq_client,
        "OPENAI_API_KEY": openai_client,
    }

    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq,openai")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(
        llm_client,
        "_client_for_provider",
        lambda config: clients[config["api_key_env"]],
    )

    assert llm_client.complete_chat([{"role": "user", "content": "Return JSON"}]) == "openai ok"

    assert len(groq_completions.calls) == 1
    assert len(openai_completions.calls) == 1


def test_retry_after_seconds_uses_response_header():
    exc = RuntimeError("rate limit")
    exc.response = SimpleNamespace(headers={"Retry-After": "7.5"})
    assert llm_client._retry_after_seconds(exc) == 7.5


def test_retry_after_seconds_caps_at_max_wait():
    exc = RuntimeError("rate limit")
    exc.response = SimpleNamespace(headers={"Retry-After": "9999"})
    assert llm_client._retry_after_seconds(exc) == llm_client.RATE_LIMIT_MAX_WAIT


def test_retry_after_seconds_parses_groq_body_hint():
    exc = RuntimeError(
        "Error code: 429 - {'error': {'message': 'Rate limit ... "
        "Please try again in 11.64s. Need more tokens? ...'}}"
    )
    # +0.5s slack so we don't bounce off the same window.
    assert llm_client._retry_after_seconds(exc) == 12.14


def test_retry_after_seconds_falls_back_to_default():
    exc = RuntimeError("opaque error without hints")
    assert llm_client._retry_after_seconds(exc) == float(llm_client.RATE_LIMIT_RETRY_WAIT)


def test_rate_limit_retry_wait_adds_jitter(monkeypatch):
    exc = RuntimeError("opaque error without hints")
    monkeypatch.setattr(llm_client.random, "uniform", lambda start, end: 3.25)

    assert llm_client._rate_limit_retry_wait(exc) == (
        float(llm_client.RATE_LIMIT_RETRY_WAIT) + 3.25
    )


def test_rate_limit_retry_wait_caps_after_jitter(monkeypatch):
    exc = RuntimeError("rate limit")
    exc.response = SimpleNamespace(headers={"Retry-After": "59"})
    monkeypatch.setattr(llm_client.random, "uniform", lambda start, end: 3)

    assert llm_client._rate_limit_retry_wait(exc) == llm_client.RATE_LIMIT_MAX_WAIT
