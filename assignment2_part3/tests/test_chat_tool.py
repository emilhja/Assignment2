import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import chat  # noqa: E402


def test_default_hub_url_uses_local_hub_port(monkeypatch):
    monkeypatch.delenv("LOCAL_HUB_URL", raising=False)
    monkeypatch.setenv("LOCAL_HUB_PORT", "8090")

    assert chat._default_hub_url() == "http://localhost:8090"


def test_default_hub_url_prefers_explicit_url(monkeypatch):
    monkeypatch.setenv("LOCAL_HUB_URL", "http://hub.example")
    monkeypatch.setenv("LOCAL_HUB_PORT", "8090")

    assert chat._default_hub_url() == "http://hub.example"
