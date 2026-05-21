"""Tiny CLI for talking to the local hub (or the live TH25 hub).

Examples (against a hub on http://localhost:8080):

    python tools/chat.py say "hello bots"
    python tools/chat.py tail                    # prints all messages, then exits
    python tools/chat.py tail --follow           # streams new messages
    python tools/chat.py stats

Flags:
    --url       hub base URL  (default $LOCAL_HUB_URL or http://localhost:8080)
    --password  hub password  (default $LOCAL_HUB_PASSWORD or "local-hub")
    --as NAME   sender name for `say`  (default $LOCAL_HUB_USER or "emil-user")
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

try:
    import requests
except ImportError:
    sys.stderr.write("chat.py requires the 'requests' package (pip install requests)\n")
    sys.exit(1)


def _get(url: str, params: dict) -> dict:
    resp = requests.get(url, params=params, timeout=10)
    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text[:200]}
    if resp.status_code != 200:
        sys.stderr.write(f"[chat] {url} -> {resp.status_code}: {data}\n")
    return data


def _post(url: str, payload: dict) -> dict:
    resp = requests.post(url, json=payload, timeout=10)
    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text[:200]}
    if resp.status_code != 200:
        sys.stderr.write(f"[chat] {url} -> {resp.status_code}: {data}\n")
    return data


def cmd_say(args, base: str, password: str) -> int:
    text = " ".join(args.text).strip()
    if not text:
        sys.stderr.write("usage: chat.py say <text>\n")
        return 2
    data = _post(
        f"{base}/api/message",
        {"agent_name": args.as_, "content": text, "password": password},
    )
    if data.get("status") == "ok":
        print(f"[hub->] seq={data.get('seq')} {args.as_}: {text[:120]}")
        return 0
    return 1


def _print_message(m: dict) -> None:
    ts = m.get("timestamp", "")
    print(f"  seq={m.get('seq')} {ts} {m.get('agent_name')}: {m.get('content')}")


def cmd_tail(args, base: str, password: str) -> int:
    since = args.since
    if not args.follow:
        data = _get(f"{base}/api/messages", {"since": since, "password": password})
        for m in data.get("messages", []):
            _print_message(m)
        return 0

    print(f"[chat] following {base} since={since} (Ctrl-C to stop)", file=sys.stderr)
    try:
        while True:
            data = _get(f"{base}/api/messages", {"since": since, "password": password})
            messages = data.get("messages") or []
            for m in messages:
                _print_message(m)
                seq = m.get("seq")
                if isinstance(seq, int) and seq > since:
                    since = seq
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[chat] stopped", file=sys.stderr)
    return 0


def cmd_stats(_args, base: str, password: str) -> int:
    data = _get(f"{base}/api/stats", {"password": password})
    print(f"total: {data.get('total_messages')}")
    for name, count in (data.get("per_agent") or {}).items():
        print(f"  {name}: {count}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    default_url = os.environ.get("LOCAL_HUB_URL", "http://localhost:8080")
    default_pw = os.environ.get("LOCAL_HUB_PASSWORD", "local-hub")
    default_user = os.environ.get("LOCAL_HUB_USER", "emil-user")

    parser = argparse.ArgumentParser(description="Talk to a TH25-compatible hub.")
    parser.add_argument("--url", default=default_url)
    parser.add_argument("--password", default=default_pw)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_say = sub.add_parser("say", help="post a message")
    p_say.add_argument("--as", dest="as_", default=default_user, help="sender agent name")
    p_say.add_argument("text", nargs="+", help="message text")
    p_say.set_defaults(func=cmd_say)

    p_tail = sub.add_parser("tail", help="print messages")
    p_tail.add_argument("--since", type=int, default=0)
    p_tail.add_argument("--follow", "-f", action="store_true")
    p_tail.add_argument("--interval", type=float, default=2.0)
    p_tail.set_defaults(func=cmd_tail)

    p_stats = sub.add_parser("stats", help="print hub stats")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    return args.func(args, base, args.password)


if __name__ == "__main__":
    sys.exit(main())
