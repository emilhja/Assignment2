"""Tiny CLI for talking to the local hub (or the live TH25 hub).

Examples (against a hub on http://localhost:8080):

    python tools/chat.py say "hello bots"
    python tools/chat.py tail                    # prints all messages, then exits
    python tools/chat.py tail --follow           # streams new messages
    python tools/chat.py live                    # REPL: read incoming + send in one shell
    python tools/chat.py stats

Flags:
    --url       hub base URL  (default $LOCAL_HUB_URL or http://localhost:8080)
    --password  hub password  (default $LOCAL_HUB_PASSWORD or "local-hub")
    --as NAME   sender name for `say` / `live`  (default $LOCAL_HUB_USER or "emil-user")
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

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


def _short_time(ts: str) -> str:
    if not ts:
        return ""
    t = ts
    if "T" in t:
        t = t.split("T", 1)[1]
    elif " " in t:
        t = t.split(" ", 1)[1]
    return t[:5]


_NAME_COLORS = ("36", "33", "35", "32", "34", "31", "96", "93")
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _color_for(name: str) -> str:
    if not name:
        return "37"
    return _NAME_COLORS[sum(ord(c) for c in name) % len(_NAME_COLORS)]


def _print_message(m: dict) -> None:
    ts = _short_time(m.get("timestamp", ""))
    name = m.get("agent_name") or "?"
    content = m.get("content", "")
    if _USE_COLOR:
        color = _color_for(name)
        tag = f"\033[1;{color}m<{name}>\033[0m"
        meta = f"\033[2m[{ts}]\033[0m"
    else:
        tag = f"<{name}>"
        meta = f"[{ts}]"
    print(f"  {meta} {tag} {content}")


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


def cmd_live(args, base: str, password: str) -> int:
    user = args.as_
    print_lock = threading.Lock()
    state = {"since": args.since}
    stop = threading.Event()

    if args.since == 0 and not args.history:
        initial = _get(f"{base}/api/messages", {"since": 0, "password": password})
        for m in initial.get("messages") or []:
            seq = m.get("seq")
            if isinstance(seq, int) and seq > state["since"]:
                state["since"] = seq

    def poll() -> None:
        while not stop.is_set():
            data = _get(
                f"{base}/api/messages",
                {"since": state["since"], "password": password},
            )
            for m in data.get("messages") or []:
                seq = m.get("seq")
                if isinstance(seq, int) and seq > state["since"]:
                    state["since"] = seq
                if m.get("agent_name") == user:
                    continue
                with print_lock:
                    sys.stdout.write("\r\033[K")
                    _print_message(m)
                    sys.stdout.write(f"{user}> ")
                    sys.stdout.flush()
            stop.wait(args.interval)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()

    print(
        f"[chat] live on {base} as {user}. Type 'exit' or Ctrl-C to quit.",
        file=sys.stderr,
    )
    try:
        while True:
            try:
                text = input(f"{user}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[chat] stopped", file=sys.stderr)
                return 0
            if not text:
                continue
            if text.lower() in {"exit", "quit", "q"}:
                return 0
            data = _post(
                f"{base}/api/message",
                {"agent_name": user, "content": text, "password": password},
            )
            if data.get("status") != "ok":
                with print_lock:
                    sys.stderr.write(f"[chat!] post failed: {data}\n")
    finally:
        stop.set()


def cmd_stats(_args, base: str, password: str) -> int:
    data = _get(f"{base}/api/stats", {"password": password})
    print(f"total: {data.get('total_messages')}")
    for name, count in (data.get("per_agent") or {}).items():
        print(f"  {name}: {count}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv()
    default_url = os.environ.get("LOCAL_HUB_URL", "http://localhost:8080")
    default_pw = (
        os.environ.get("LOCAL_HUB_PASSWORD")
        or os.environ.get("RUNPOD_CHAT_PASSWORD")
        or "local-hub"
    )
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

    p_live = sub.add_parser("live", help="REPL: stream incoming + send in one shell")
    p_live.add_argument("--as", dest="as_", default=default_user, help="sender agent name")
    p_live.add_argument("--since", type=int, default=0)
    p_live.add_argument("--interval", type=float, default=2.0)
    p_live.add_argument("--history", action="store_true", help="replay history on start (default: skip)")
    p_live.set_defaults(func=cmd_live)

    p_stats = sub.add_parser("stats", help="print hub stats")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    return args.func(args, base, args.password)


if __name__ == "__main__":
    sys.exit(main())
