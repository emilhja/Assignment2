import io
import json

import pytest

from transport import RunPodTransport, StubTransport, build_transport


def test_recv_parses_json_line():
    inbox = io.StringIO(
        json.dumps({"id": "m1", "sender_id": "bob", "text": "hi alice"}) + "\n"
    )
    outbox = io.StringIO()
    t = StubTransport("alice", inbox=inbox, outbox=outbox)
    msg = t.recv()
    assert msg is not None
    assert msg.id == "m1"
    assert msg.sender_id == "bob"
    assert msg.text == "hi alice"


def test_recv_returns_none_at_eof():
    t = StubTransport("alice", inbox=io.StringIO(""), outbox=io.StringIO())
    assert t.recv() is None


def test_recv_skips_invalid_json():
    inbox = io.StringIO("this is not json\n")
    t = StubTransport("alice", inbox=inbox, outbox=io.StringIO())
    assert t.recv() is None


def test_send_writes_json_line():
    outbox = io.StringIO()
    t = StubTransport("alice", inbox=io.StringIO(""), outbox=outbox)
    t.send("hello world")
    line = outbox.getvalue().strip()
    payload = json.loads(line)
    assert payload["sender_id"] == "alice"
    assert payload["text"] == "hello world"
    assert "ts" in payload


def test_close_blocks_send():
    t = StubTransport("alice", inbox=io.StringIO(""), outbox=io.StringIO())
    t.close()
    with pytest.raises(RuntimeError):
        t.send("nope")


def test_seen_dedup_skips_repeats(tmp_path):
    inbox = io.StringIO(
        json.dumps({"id": "dup", "sender_id": "bob", "text": "first"}) + "\n" +
        json.dumps({"id": "dup", "sender_id": "bob", "text": "second"}) + "\n"
    )
    seen_path = tmp_path / "seen.json"
    t = StubTransport("alice", inbox=inbox, outbox=io.StringIO(), seen_path=seen_path)
    first = t.recv()
    second = t.recv()
    assert first is not None
    assert first.text == "first"
    assert second is None  # deduped


def test_seen_dedup_persists_across_instances(tmp_path):
    seen_path = tmp_path / "seen.json"
    inbox1 = io.StringIO(json.dumps({"id": "p1", "sender_id": "bob", "text": "first"}) + "\n")
    StubTransport("alice", inbox=inbox1, outbox=io.StringIO(), seen_path=seen_path).recv()

    inbox2 = io.StringIO(json.dumps({"id": "p1", "sender_id": "bob", "text": "again"}) + "\n")
    t2 = StubTransport("alice", inbox=inbox2, outbox=io.StringIO(), seen_path=seen_path)
    assert t2.recv() is None


def test_build_transport_stub_default(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNPOD_CHAT_URL", raising=False)
    t = build_transport("stub", "alice", tmp_path)
    assert isinstance(t, StubTransport)


def test_build_transport_runpod_requires_url(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_CHAT_URL", "")
    with pytest.raises(RuntimeError):
        build_transport("runpod", "alice", tmp_path)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, get_responses=None, post_responses=None):
        self._get_queue = list(get_responses or [])
        self._post_queue = list(post_responses or [])
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        self.get_calls.append((url, dict(params or {})))
        return self._get_queue.pop(0) if self._get_queue else _FakeResponse(200, {"messages": []})

    def post(self, url, json=None, timeout=None):
        self.post_calls.append((url, dict(json or {})))
        return self._post_queue.pop(0) if self._post_queue else _FakeResponse(200, {"status": "ok", "seq": 1})

    def close(self):
        self.closed = True


class _FakeClock:
    """Deterministic monotonic clock that advances when `sleep` is called."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _make_transport(tmp_path, **overrides):
    clock = _FakeClock()
    session = overrides.pop("session", _FakeSession())
    stdout = overrides.pop("stdout", io.StringIO())
    t = RunPodTransport(
        agent_name=overrides.pop("agent_name", "emil-tester"),
        url=overrides.pop("url", "https://hub.example/"),
        password=overrides.pop("password", "pw"),
        poll_interval=overrides.pop("poll_interval", 1.0),
        seen_path=overrides.pop("seen_path", tmp_path / "seen.json"),
        session=session,
        stdout=stdout,
        clock=clock,
        sleep=clock.sleep,
    )
    return t, session, stdout, clock


def test_runpod_send_posts_expected_payload(tmp_path):
    t, session, stdout, _ = _make_transport(tmp_path)
    t.send("hello hub")
    assert len(session.post_calls) == 1
    url, body = session.post_calls[0]
    assert url == "https://hub.example/api/message"
    assert body == {"agent_name": "emil-tester", "content": "hello hub", "password": "pw"}
    assert "[hub->]" in stdout.getvalue()


def test_runpod_send_treats_201_created_as_success(tmp_path):
    session = _FakeSession(post_responses=[_FakeResponse(201, {"ok": True, "seq": 85})])
    t, _, stdout, _ = _make_transport(tmp_path, session=session)

    t.send("created")

    output = stdout.getvalue()
    assert "[hub->]" in output
    assert "[hub!]" not in output


def test_runpod_send_truncates_to_4096(tmp_path):
    t, session, _, _ = _make_transport(tmp_path)
    t.send("x" * 5000)
    _, body = session.post_calls[0]
    assert len(body["content"]) == 4096


def test_runpod_send_skips_blank(tmp_path):
    t, session, _, _ = _make_transport(tmp_path)
    t.send("   \n  ")
    assert session.post_calls == []


def test_runpod_send_handles_429_without_raising(tmp_path):
    session = _FakeSession(post_responses=[_FakeResponse(status_code=429, payload={"error": "rate"})])
    t, _, stdout, _ = _make_transport(tmp_path, session=session)
    t.send("hi")
    assert "[hub!]" in stdout.getvalue()


def test_runpod_recv_returns_peermessage_and_buffers_rest(tmp_path):
    payload = {"messages": [
        {"seq": 1, "agent_name": "other", "content": "first"},
        {"seq": 2, "agent_name": "another", "content": "second"},
    ]}
    session = _FakeSession(get_responses=[_FakeResponse(200, payload)])
    t, _, _, clock = _make_transport(tmp_path, session=session)
    clock.now += 10  # ensure poll-interval gate passes
    first = t.recv(timeout=0)
    assert first is not None and first.id == "1" and first.sender_id == "other" and first.text == "first"
    second = t.recv(timeout=0)
    assert second is not None and second.id == "2" and second.text == "second"
    assert len(session.get_calls) == 1  # second came from buffer, no new GET


def test_runpod_recv_skips_own_messages(tmp_path):
    payload = {"messages": [
        {"seq": 5, "agent_name": "emil-tester", "content": "me"},
        {"seq": 6, "agent_name": "other", "content": "them"},
    ]}
    session = _FakeSession(get_responses=[_FakeResponse(200, payload)])
    t, _, _, clock = _make_transport(tmp_path, session=session)
    clock.now += 10
    msg = t.recv(timeout=0)
    assert msg is not None and msg.id == "6" and msg.sender_id == "other"


def test_runpod_recv_persists_seen_seq(tmp_path):
    seen_path = tmp_path / "seen.json"
    payload = {"messages": [{"seq": 11, "agent_name": "other", "content": "hi"}]}
    session = _FakeSession(get_responses=[_FakeResponse(200, payload)])
    t, _, _, clock = _make_transport(tmp_path, session=session, seen_path=seen_path)
    clock.now += 10
    t.recv(timeout=0)
    stored = json.loads(seen_path.read_text(encoding="utf-8"))
    assert "11" in stored


def test_runpod_recv_429_does_not_raise(tmp_path):
    session = _FakeSession(get_responses=[_FakeResponse(status_code=429)])
    t, _, stdout, clock = _make_transport(tmp_path, session=session)
    clock.now += 10
    assert t.recv(timeout=0) is None
    assert "[hub!]" in stdout.getvalue()


def test_runpod_recv_respects_poll_interval(tmp_path):
    session = _FakeSession(get_responses=[_FakeResponse(200, {"messages": []})])
    t, _, _, clock = _make_transport(tmp_path, session=session, poll_interval=5.0)
    # First call: clock at 1000, _last_poll_ts=0 → gap is huge, should fire GET.
    t.recv(timeout=0)
    assert len(session.get_calls) == 1
    # Second call immediately: should NOT fire another GET (poll interval not elapsed).
    t.recv(timeout=0)
    assert len(session.get_calls) == 1


def test_runpod_throttle_sleeps_when_too_soon(tmp_path):
    t, _, _, clock = _make_transport(tmp_path, poll_interval=1.0)
    t._last_request_ts = clock.now - 0.2  # only 0.2s elapsed → must sleep ~0.8
    t._throttle()
    assert any(0.7 < s <= 1.0 for s in clock.sleeps)


def test_build_transport_runpod_uses_token_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_CHAT_URL", "https://hub.example")
    monkeypatch.delenv("RUNPOD_CHAT_PASSWORD", raising=False)
    monkeypatch.setenv("RUNPOD_CHAT_TOKEN", "fallback-pw")
    monkeypatch.setenv("AGENT_DISPLAY_NAME", "emil-builder")
    t = build_transport("runpod", "emil", tmp_path)
    assert isinstance(t, RunPodTransport)
    assert t.password == "fallback-pw"
    assert t.agent_name == "emil-builder"


def test_build_transport_runpod_requires_password(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_CHAT_URL", "https://hub.example")
    monkeypatch.delenv("RUNPOD_CHAT_PASSWORD", raising=False)
    monkeypatch.delenv("RUNPOD_CHAT_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_DISPLAY_NAME", "emil-builder")
    with pytest.raises(RuntimeError, match="password"):
        build_transport("runpod", "emil", tmp_path)


def test_build_transport_runpod_rejects_forbidden_name(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_CHAT_URL", "https://hub.example")
    monkeypatch.setenv("RUNPOD_CHAT_PASSWORD", "pw")
    monkeypatch.setenv("AGENT_DISPLAY_NAME", "bot")
    with pytest.raises(RuntimeError, match="forbidden"):
        build_transport("runpod", "anything", tmp_path)
