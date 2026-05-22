import pytest

import llm_client


class FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = type("Message", (), {"content": "Thought: Done\nFinal Answer: ok"})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": FakeCompletions()})()


def test_groq_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        llm_client._groq_client()


def test_complete_chat_uses_groq_model(monkeypatch):
    fake_client = FakeClient()
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_MODEL", "test-model")
    monkeypatch.setattr(llm_client, "_groq_client", lambda: fake_client)

    content = llm_client.complete_chat([{"role": "user", "content": "hello"}])

    assert content == "Thought: Done\nFinal Answer: ok"
    assert fake_client.chat.completions.calls == [
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    ]


def test_complete_chat_uses_local_provider_without_api_key(monkeypatch):
    fake_client = FakeClient()
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "local")
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen-local")
    monkeypatch.setattr(llm_client, "_local_client", lambda: fake_client)

    content = llm_client.complete_chat([{"role": "user", "content": "hello"}])

    assert content == "Thought: Done\nFinal Answer: ok"
    assert fake_client.chat.completions.calls == [
        {
            "model": "qwen-local",
            "messages": [{"role": "user", "content": "hello"}],
        }
    ]


def test_local_base_url_accepts_server_root(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080")

    assert llm_client._local_base_url() == "http://127.0.0.1:8080/v1"
