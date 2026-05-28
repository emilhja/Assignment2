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
    assert t.send("hello world") is True
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
    assert t.send("hello hub") is True
    assert len(session.post_calls) == 1
    url, body = session.post_calls[0]
    assert url == "https://hub.example/api/message"
    assert body == {"agent_name": "emil-tester", "content": "hello hub", "password": "pw"}
    assert "[hub->]" in stdout.getvalue()


def test_runpod_send_treats_201_created_as_success(tmp_path):
    session = _FakeSession(post_responses=[_FakeResponse(201, {"ok": True, "seq": 85})])
    t, _, stdout, _ = _make_transport(tmp_path, session=session)

    assert t.send("created") is True

    output = stdout.getvalue()
    assert "[hub->]" in output
    assert "[hub!]" not in output


def test_runpod_send_keeps_payload_within_wire_cap(tmp_path):
    """5000 'x' chars with no whitespace → 2-part split, every POST ≤ 4096."""
    t, session, _, _ = _make_transport(tmp_path)
    t.send("x" * 5000)
    assert len(session.post_calls) >= 2
    for _, body in session.post_calls:
        assert len(body["content"]) <= 4096


def test_runpod_send_does_not_prefix_when_under_cap(tmp_path):
    t, session, _, _ = _make_transport(tmp_path)
    t.send("plain short message")
    assert len(session.post_calls) == 1
    _, body = session.post_calls[0]
    assert body["content"] == "plain short message"
    assert "(part" not in body["content"]


def test_runpod_send_splits_oversized_text_with_part_prefixes(tmp_path):
    t, session, _, _ = _make_transport(tmp_path)
    line = "x" * 200 + "\n"
    text = line * 30  # ~6030 chars, plenty of newline boundaries
    t.send(text)

    assert len(session.post_calls) >= 2
    contents = [body["content"] for _, body in session.post_calls]
    total = len(contents)
    for i, content in enumerate(contents, start=1):
        assert content.startswith(f"(part {i}/{total})\n")
        assert len(content) <= 4096
    # Reassembling (minus the prefix line) should recover the original payload
    # with whitespace preserved at boundaries.
    rejoined = "".join(c.split("\n", 1)[1] for c in contents)
    assert rejoined.replace("\n", "") == text.replace("\n", "")


def test_runpod_send_prefers_newline_boundary_over_mid_line_cut(tmp_path):
    t, session, _, _ = _make_transport(tmp_path)
    line = "abcdefghij" * 50 + "\n"  # 501 chars per line, ends in \n
    text = line * 10  # 5010 chars, 10 full lines
    t.send(text)

    assert len(session.post_calls) >= 2
    for _, body in session.post_calls:
        content = body["content"]
        # The body after the "(part i/N)\n" header should end at a line
        # boundary (or be the final chunk), never mid-line.
        body_text = content.split("\n", 1)[1] if content.startswith("(part") else content
        assert body_text == "" or body_text.endswith("\n") or body_text.endswith("abcdefghij" * 50)


def test_runpod_send_throttles_between_split_parts(tmp_path):
    """Each part triggers _throttle, so a multi-part send sleeps between POSTs."""
    t, session, _, clock = _make_transport(tmp_path)
    t.send("y" * 9000)  # forces ≥3 parts
    assert len(session.post_calls) >= 3
    # First send is "free" (no prior request); the rest should pay HUB_MIN_REQUEST_GAP each.
    assert sum(1 for s in clock.sleeps if s > 0) >= len(session.post_calls) - 1


def test_runpod_send_logs_each_part(tmp_path):
    t, session, stdout, _ = _make_transport(tmp_path)
    t.send("z" * 9000)
    output = stdout.getvalue()
    assert output.count("[hub->]") == len(session.post_calls)


def test_runpod_send_skips_blank(tmp_path):
    t, session, _, _ = _make_transport(tmp_path)
    assert t.send("   \n  ") is True
    assert session.post_calls == []


def test_runpod_send_handles_429_without_raising(tmp_path):
    session = _FakeSession(post_responses=[_FakeResponse(status_code=429, payload={"error": "rate"})])
    t, _, stdout, _ = _make_transport(tmp_path, session=session)
    assert t.send("hi") is True
    assert "[hub!]" in stdout.getvalue()


def test_runpod_send_retries_on_429_then_succeeds(tmp_path):
    """One transient 429 should not strand the message — the retry must land."""
    session = _FakeSession(
        post_responses=[
            _FakeResponse(status_code=429, payload={"error": "rate"}),
            _FakeResponse(status_code=200, payload={"ok": True, "seq": 99}),
        ]
    )
    t, _, stdout, clock = _make_transport(tmp_path, session=session)
    assert t.send("payload") is True
    assert len(session.post_calls) == 2
    output = stdout.getvalue()
    assert "[hub->]" in output  # eventual success was logged
    assert "[hub!] send 429" in output  # backoff was logged
    assert "[hub!] send failed" not in output  # no terminal failure
    # Must have slept at least the 429 backoff between the two POSTs.
    assert any(s >= 1.5 for s in clock.sleeps)


def test_runpod_send_gives_up_after_max_retries_on_429(tmp_path):
    session = _FakeSession(
        post_responses=[
            _FakeResponse(status_code=429, payload={"error": "rate"}),
            _FakeResponse(status_code=429, payload={"error": "rate"}),
            _FakeResponse(status_code=429, payload={"error": "rate"}),
        ]
    )
    t, _, stdout, _ = _make_transport(tmp_path, session=session)
    assert t.send("payload") is False
    # 1 initial attempt + 2 retries = 3 POSTs total
    assert len(session.post_calls) == 3
    output = stdout.getvalue()
    assert "[hub!] send failed status=429" in output


def test_runpod_multipart_send_aborts_after_failed_middle_part(tmp_path):
    session = _FakeSession(
        post_responses=[
            _FakeResponse(status_code=200, payload={"ok": True, "seq": 1}),
            _FakeResponse(status_code=500, payload={"error": "boom"}),
        ]
    )
    t, session, stdout, _ = _make_transport(tmp_path, session=session)

    assert t.send("x" * 9000) is False

    assert len(session.post_calls) == 2
    contents = [body["content"] for _, body in session.post_calls]
    assert contents[0].startswith("(part 1/")
    assert contents[1].startswith("(part 2/")
    output = stdout.getvalue()
    assert "send failed status=500" in output
    assert "multipart send aborted after failed part 2/" in output


def test_runpod_min_gap_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_MIN_REQUEST_GAP_SECONDS", "2.5")
    t, _, _, _ = _make_transport(tmp_path)
    assert t._min_request_gap == 2.5


def test_runpod_min_gap_env_clamped_to_floor(tmp_path, monkeypatch):
    """Sub-1s overrides are clamped — the hub enforces 1 req/sec hard."""
    monkeypatch.setenv("HUB_MIN_REQUEST_GAP_SECONDS", "0.3")
    t, _, _, _ = _make_transport(tmp_path)
    assert t._min_request_gap == 1.0


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
