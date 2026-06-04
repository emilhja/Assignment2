"""Group-chat transport (P3.4).

`Transport` is the only outbound surface for inter-agent communication.
`StubTransport` reads JSON lines from any text stream and is used by the
test suite and local dev. `RunPodTransport` connects to the TH25 hub
(see `th25-hub-connection.md`) via the REST endpoints `/api/message`
(POST) and `/api/messages` (GET) under a shared password.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import IO, Any, Deque, Iterable, Optional, Protocol

import part2_bridge  # noqa: F401 — sys.path side effect for `colors`

import colors
from peer import PeerMessage, mask_workspace_file_paths


class Transport(Protocol):
    def recv(self, timeout: Optional[float] = None) -> Optional[PeerMessage]: ...
    def send(self, text: str) -> bool: ...
    def close(self) -> None: ...


def _load_seen_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, list):
        return {str(x) for x in data}
    return set()


def _save_seen_ids(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen)), encoding="utf-8")


class StubTransport:
    """Reads PeerMessage JSON lines from `inbox`, writes replies to `outbox`.

    Each incoming line must be a JSON object with at minimum `id`, `sender_id`,
    `text`. `addressed_to` is optional (list of agent names). Outgoing replies
    are wrapped as `{"sender_id": <agent_id>, "text": <text>, "ts": <unix>}`.
    """

    def __init__(
        self,
        agent_id: str,
        inbox: IO[str] | Iterable[str] | None = None,
        outbox: IO[str] | None = None,
        seen_path: Path | str | None = None,
    ):
        self.agent_id = agent_id
        self._inbox_iter = iter(inbox) if inbox is not None else iter(sys.stdin)
        self._outbox = outbox if outbox is not None else sys.stdout
        self._seen_path = Path(seen_path) if seen_path else None
        self._seen = _load_seen_ids(self._seen_path) if self._seen_path else set()
        self._closed = False
        self._lock = threading.Lock()

    def recv(self, timeout: Optional[float] = None) -> Optional[PeerMessage]:
        if self._closed:
            return None
        try:
            line = next(self._inbox_iter)
        except StopIteration:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        msg_id = str(payload.get("id") or f"auto-{time.time()}")
        if msg_id in self._seen:
            return None
        message = PeerMessage(
            id=msg_id,
            sender_id=str(payload.get("sender_id", "unknown")),
            text=str(payload.get("text", "")),
            received_at=float(payload.get("received_at", time.time())),
            addressed_to=tuple(payload.get("addressed_to", []) or ()),
        )
        with self._lock:
            self._seen.add(msg_id)
            if self._seen_path:
                _save_seen_ids(self._seen_path, self._seen)
        return message

    def send(self, text: str) -> bool:
        if self._closed:
            raise RuntimeError("transport closed")
        text = mask_workspace_file_paths(text)
        payload = json.dumps(
            {"sender_id": self.agent_id, "text": text, "ts": time.time()},
            ensure_ascii=False,
        )
        with self._lock:
            self._outbox.write(payload + "\n")
            self._outbox.flush()
        return True

    def close(self) -> None:
        self._closed = True


HUB_MAX_CONTENT_CHARS = 4096
# Reserved at the start of each chunk for the `(part i/N)\n` header when a
# message needs to be split. 16 covers up to `(part 99/99)\n` with slack.
HUB_PART_PREFIX_RESERVE = 16
# Hub enforces 1 req/sec per agent name. We default to 1.2s to leave headroom
# for clock drift and network jitter — otherwise a request whose body finishes
# arriving 50ms late at the server will be 429'd.
HUB_MIN_REQUEST_GAP = 1.2
# Per-chunk retry on 429. The hub's body is "Wait 1 second between requests",
# so the backoff is the throttle gap plus a small cushion.
HUB_SEND_429_RETRIES = 2
HUB_SEND_429_BACKOFF = 1.5
FORBIDDEN_HUB_NAMES = {"my-agent", "my_agent", "agent", "test", "bot", "local"}


def _resolve_min_request_gap() -> float:
    raw = os.environ.get("HUB_MIN_REQUEST_GAP_SECONDS", "").strip()
    if not raw:
        return HUB_MIN_REQUEST_GAP
    try:
        value = float(raw)
    except ValueError:
        return HUB_MIN_REQUEST_GAP
    # Never go below the hub's documented 1s floor.
    return max(1.0, value)


def _split_for_hub(text: str, max_chars: int) -> list[str]:
    """Split `text` into chunks each ≤ `max_chars`, preferring safe boundaries.

    Tries paragraph break (`\\n\\n`), then line break (`\\n`), then any
    whitespace. Falls back to a hard cut if none of those land in the
    later portion of the window (the early-cut guard avoids producing a
    tiny first part when the only newline is near the very start).
    """

    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    remaining = text
    min_useful_cut = max(1, max_chars // 4)
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx >= min_useful_cut:
                cut = idx + len(sep)
                break
        if cut < 0:
            cut = max_chars
        chunk = remaining[:cut].rstrip()
        if chunk:
            parts.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def _log_snippet(text: str) -> str:
    """Trim `text` for `[hub<-]`/`[hub->]` console lines.

    Length is controlled by `HUB_LOG_SNIPPET_CHARS` (default 120). Set to 0
    to disable truncation entirely.
    """
    try:
        limit = int(os.environ.get("HUB_LOG_SNIPPET_CHARS", "120"))
    except ValueError:
        limit = 120
    snippet = text.replace("\n", " ")
    if limit <= 0:
        return snippet
    return snippet[:limit]


class RunPodTransport:
    """REST client for the TH25 hub (`th25-hub-connection.md`).

    - `recv` polls `GET /api/messages?since=<seq>&password=<pw>` and returns
      the next un-seen message that wasn't authored by us.
    - `send` posts `{"agent_name", "content", "password"}` to
      `/api/message`. Failures (429, network) are surfaced as `[hub!]`
      diagnostic lines on the local stdout and never raise — the main
      loop must keep running.

    Construction-time dependencies (`session`, `stdout`) are injected by
    tests; in production they default to `requests.Session()` and
    `sys.stdout`.
    """

    def __init__(
        self,
        agent_name: str,
        url: str,
        password: str,
        *,
        poll_interval: float = 4.0,
        seen_path: Path | str | None = None,
        session: Any = None,
        stdout: IO[str] | None = None,
        clock: Any = None,
        sleep: Any = None,
    ):
        self.agent_name = agent_name
        self.url = url.rstrip("/")
        self.password = password
        self.poll_interval = max(1.0, float(poll_interval))
        self._seen_path = Path(seen_path) if seen_path else None
        self._seen = _load_seen_ids(self._seen_path) if self._seen_path else set()
        self._last_seen_seq = max((int(s) for s in self._seen if s.isdigit()), default=0)
        self._buffer: Deque[PeerMessage] = deque()
        self._last_request_ts: float = 0.0
        self._last_poll_ts: float = 0.0
        self._closed = False
        self._lock = threading.Lock()
        self._stdout = stdout if stdout is not None else sys.stdout
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._min_request_gap = _resolve_min_request_gap()
        if session is None:
            import requests  # local import so tests that inject a session don't pay the cost

            session = requests.Session()
        self._session = session

    def _echo(self, line: str) -> None:
        try:
            self._stdout.write(line + "\n")
            self._stdout.flush()
        except Exception:
            pass

    def _warn(self, message: str) -> None:
        self._echo(f"{colors.ts()} {colors.paint(message, colors.RED)}")

    def _throttle(self) -> None:
        gap = self._clock() - self._last_request_ts
        if gap < self._min_request_gap:
            self._sleep(self._min_request_gap - gap)
        self._last_request_ts = self._clock()

    def _persist_seen(self, seq: int) -> None:
        self._seen.add(str(seq))
        if seq > self._last_seen_seq:
            self._last_seen_seq = seq
        if self._seen_path:
            _save_seen_ids(self._seen_path, self._seen)

    def recv(self, timeout: Optional[float] = None) -> Optional[PeerMessage]:
        if self._closed:
            return None
        with self._lock:
            if self._buffer:
                return self._buffer.popleft()

        now = self._clock()
        since_last_poll = now - self._last_poll_ts
        if since_last_poll < self.poll_interval:
            wait = self.poll_interval - since_last_poll
            if timeout is not None:
                wait = min(wait, timeout)
            if wait > 0:
                self._sleep(wait)
            return None

        self._throttle()
        try:
            resp = self._session.get(
                f"{self.url}/api/messages",
                params={"since": self._last_seen_seq, "password": self.password},
                timeout=10,
            )
        except Exception as exc:
            self._warn(f"[hub!] recv error: {exc}")
            self._sleep(2.0)
            return None
        self._last_poll_ts = self._clock()

        status = getattr(resp, "status_code", 0)
        if status == 429:
            self._warn("[hub!] recv rate-limited (429), backing off")
            self._sleep(4.0)
            return None
        if status == 401:
            self._warn("[hub!] recv auth failed (401) — check RUNPOD_CHAT_PASSWORD")
            self._sleep(4.0)
            return None
        if status != 200:
            body = ""
            try:
                body = resp.text[:200] if hasattr(resp, "text") else str(resp.json())[:200]
            except Exception:
                pass
            self._warn(f"[hub!] recv status={status} body={body}")
            self._sleep(2.0)
            return None

        try:
            data = resp.json()
        except Exception as exc:
            self._warn(f"[hub!] recv json error: {exc}")
            return None

        messages = data.get("messages") if isinstance(data, dict) else None
        if not messages:
            return None

        first_returned: Optional[PeerMessage] = None
        with self._lock:
            for entry in messages:
                if not isinstance(entry, dict):
                    continue
                seq = entry.get("seq")
                sender = str(entry.get("agent_name", ""))
                content = str(entry.get("content", ""))
                if seq is None:
                    continue
                seq_str = str(seq)
                if seq_str in self._seen:
                    continue
                self._persist_seen(int(seq) if str(seq).lstrip("-").isdigit() else 0)
                if sender == self.agent_name:
                    continue
                msg = PeerMessage(
                    id=seq_str,
                    sender_id=sender,
                    text=content,
                    received_at=time.time(),
                    addressed_to=(),
                )
                if first_returned is None:
                    first_returned = msg
                else:
                    self._buffer.append(msg)

        if first_returned is not None:
            snippet = _log_snippet(first_returned.text)
            self._echo(
                f"{colors.ts()} {colors.dim('[hub<-]')} "
                f"{colors.agent_label(first_returned.sender_id)}: {snippet}"
            )
        return first_returned

    def send(self, text: str) -> bool:
        if self._closed:
            raise RuntimeError("transport closed")
        if not text or not text.strip():
            return True
        text = mask_workspace_file_paths(text)

        if len(text) <= HUB_MAX_CONTENT_CHARS:
            return self._send_one(text)

        body_max = HUB_MAX_CONTENT_CHARS - HUB_PART_PREFIX_RESERVE
        parts = _split_for_hub(text, body_max)
        if not parts:
            return True
        total = len(parts)
        for i, body in enumerate(parts, start=1):
            prefix = f"(part {i}/{total})\n"
            payload = (prefix + body)[:HUB_MAX_CONTENT_CHARS]
            if not self._send_one(payload):
                self._warn(
                    f"[hub!] multipart send aborted after failed part {i}/{total}"
                )
                return False
        return True

    def _send_one(self, payload_text: str) -> bool:
        # Retry on 429 so a single rate-limit hit doesn't strand a peer with a
        # partial multi-part message. Network errors are not retried — they
        # tend to be persistent on this path.
        for attempt in range(HUB_SEND_429_RETRIES + 1):
            self._throttle()
            try:
                resp = self._session.post(
                    f"{self.url}/api/message",
                    json={
                        "agent_name": self.agent_name,
                        "content": payload_text,
                        "password": self.password,
                    },
                    timeout=10,
                )
            except Exception as exc:
                self._warn(f"[hub!] send error: {exc}")
                return False

            status = getattr(resp, "status_code", 0)
            if 200 <= status < 300:
                snippet = _log_snippet(payload_text)
                self._echo(
                    f"{colors.ts()} {colors.dim('[hub->]')} "
                    f"{colors.agent_label(self.agent_name)}: {snippet}"
                )
                return True

            body = ""
            try:
                body = resp.text[:200] if hasattr(resp, "text") else str(resp.json())[:200]
            except Exception:
                pass

            if status == 429 and attempt < HUB_SEND_429_RETRIES:
                self._warn(
                    f"[hub!] send 429 (attempt {attempt + 1}/{HUB_SEND_429_RETRIES + 1}), backing off"
                )
                self._sleep(HUB_SEND_429_BACKOFF)
                continue

            self._warn(f"[hub!] send failed status={status} body={body}")
            return False
        return False

    def close(self) -> None:
        self._closed = True
        try:
            self._session.close()
        except Exception:
            pass


class _UnconfiguredTransport:
    """Returned only when build_transport is called with an unknown mode.

    Kept private so tests can still import the class graph but the live
    code paths above remain the only valid Transports.
    """

    def recv(self, timeout: Optional[float] = None) -> Optional[PeerMessage]:
        return None

    def send(self, text: str) -> bool:
        raise RuntimeError("transport not configured")

    def close(self) -> None:
        pass


def _resolve_hub_password() -> str:
    return (
        os.environ.get("RUNPOD_CHAT_PASSWORD", "").strip()
        or os.environ.get("RUNPOD_CHAT_TOKEN", "").strip()
    )


def _validate_hub_name(name: str) -> None:
    lowered = name.strip().lower()
    if not lowered:
        raise RuntimeError(
            "AGENT_DISPLAY_NAME is empty; the hub requires a unique name (format yourname-rolename)."
        )
    if lowered in FORBIDDEN_HUB_NAMES:
        raise RuntimeError(
            f"AGENT_DISPLAY_NAME={name!r} is a forbidden placeholder. "
            "Set a unique name in .env (format yourname-rolename)."
        )


def build_transport(mode: str, agent_id: str, data_dir: Path) -> Transport:
    """Factory used by `group_chat.run_group_chat`. Reads env for live mode."""

    seen_path = data_dir / f"seen_messages_{agent_id}.json"
    if mode == "runpod":
        url = os.environ.get("RUNPOD_CHAT_URL", "").strip()
        if not url:
            raise RuntimeError(
                "AGENT_MODE=runpod but RUNPOD_CHAT_URL is empty. Set it in .env."
            )
        password = _resolve_hub_password()
        if not password:
            raise RuntimeError(
                "AGENT_MODE=runpod but no hub password set. "
                "Set RUNPOD_CHAT_PASSWORD in .env (RUNPOD_CHAT_TOKEN also accepted)."
            )
        display_name = os.environ.get("AGENT_DISPLAY_NAME", "").strip() or f"{agent_id}-swe"
        _validate_hub_name(display_name)
        try:
            poll_interval = float(os.environ.get("RUNPOD_CHAT_POLL_INTERVAL", "4"))
        except ValueError:
            poll_interval = 4.0
        return RunPodTransport(
            display_name,
            url,
            password,
            poll_interval=poll_interval,
            seen_path=seen_path,
        )
    return StubTransport(agent_id, seen_path=seen_path)
