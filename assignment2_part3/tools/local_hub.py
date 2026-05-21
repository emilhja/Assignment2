"""Local mock of the TH25 hub REST API (`th25-hub-connection.md`).

Runs a stdlib HTTP server that speaks the same protocol as the live hub,
so Part 3 agents in `AGENT_MODE=runpod` can be pointed at it (typically
http://host.docker.internal:8080 from a Docker container, or
http://localhost:8080 from the host) and chat with each other and with
you locally — no internet, no RunPod, no Flask.

Usage:
    python tools/local_hub.py
    python tools/local_hub.py --port 9000 --password my-secret

Endpoints:
    POST /api/message     {agent_name, content, password}  -> {status, seq}
    GET  /api/messages    ?since=<seq>&password=...        -> {messages: [...]}
    GET  /api/stats       ?password=...                    -> {...}

Caps are intentionally loose vs. the real hub (no per-agent message cap,
no rate limit) so local development is friction-free.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse


HUB_MAX_CONTENT_CHARS = 4096


class HubState:
    def __init__(self, password: str, max_content: int = HUB_MAX_CONTENT_CHARS) -> None:
        self.password = password
        self.max_content = max_content
        self._lock = threading.Lock()
        self._messages: list[dict] = []
        self._next_seq = 1

    def post(self, agent_name: str, content: str) -> dict:
        if len(content) > self.max_content:
            return {"_status": 400, "error": f"content exceeds {self.max_content} chars"}
        with self._lock:
            entry = {
                "seq": self._next_seq,
                "agent_name": agent_name,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            self._messages.append(entry)
            self._next_seq += 1
        return {"_status": 200, "status": "ok", "seq": entry["seq"]}

    def since(self, seq: int) -> dict:
        with self._lock:
            out = [m for m in self._messages if m["seq"] > seq]
        return {"_status": 200, "messages": out}

    def stats(self) -> dict:
        with self._lock:
            per_agent: dict[str, int] = {}
            for m in self._messages:
                per_agent[m["agent_name"]] = per_agent.get(m["agent_name"], 0) + 1
            return {
                "_status": 200,
                "per_agent": per_agent,
                "total_messages": len(self._messages),
                "agents": sorted(per_agent.keys()),
            }


def _make_handler(state: HubState, verbose: bool):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # type: ignore[override]
            if verbose:
                sys.stderr.write(f"[hub] {self.address_string()} - {fmt % args}\n")

        def _send_json(self, payload: dict) -> None:
            status = payload.pop("_status", 200)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_password(self, value: Optional[str]) -> bool:
            return value == state.password

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            password = (qs.get("password") or [""])[0]
            if not self._check_password(password):
                self._send_json({"_status": 401, "error": "wrong password"})
                return
            if parsed.path == "/api/messages":
                try:
                    since = int((qs.get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                self._send_json(state.since(since))
            elif parsed.path == "/api/stats":
                self._send_json(state.stats())
            else:
                self._send_json({"_status": 404, "error": "not found"})

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/message":
                self._send_json({"_status": 404, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json({"_status": 400, "error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._send_json({"_status": 400, "error": "expected object"})
                return
            if not self._check_password(payload.get("password")):
                self._send_json({"_status": 401, "error": "wrong password"})
                return
            agent_name = str(payload.get("agent_name") or "").strip()
            content = str(payload.get("content") or "")
            if not agent_name or not content:
                self._send_json({"_status": 400, "error": "agent_name and content required"})
                return
            result = state.post(agent_name, content)
            if verbose and result.get("_status") == 200:
                sys.stderr.write(
                    f"[hub] seq={result['seq']} {agent_name}: {content[:120]}\n"
                )
            self._send_json(result)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Local mock of the TH25 hub.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--password", default="local-hub")
    parser.add_argument("--quiet", action="store_true", help="suppress request logs")
    args = parser.parse_args()

    state = HubState(password=args.password)
    handler = _make_handler(state, verbose=not args.quiet)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        f"[hub] listening on http://{args.host}:{args.port} (password={args.password!r})",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hub] shutting down", file=sys.stderr)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
