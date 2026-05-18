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
