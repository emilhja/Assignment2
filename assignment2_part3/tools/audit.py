"""Cross-agent audit CLI for Part 3 session logs.

Each agent process writes events to its own SQLite file in `data/`
(`alice.sqlite3`, `bob.sqlite3`, ...). When a peer message arrives,
`peer_task` tags every event for that turn with a `trace_id` equal
to the inbound message id, so you can reconstruct a single hub
interaction across multiple agents.

Subcommands
-----------
  agents              List discovered per-agent DBs.
  traces [-n N]       List distinct trace_ids (most recent first) with
                      row count, event-time span, and contributing agents.
  trace <trace_id>    Print every event tagged with <trace_id>, interleaved
                      across agents in chronological order.
  tail [-n N]         Tail recent events across all agents.
                      Filters: --agent <id>, --kind <kind>.

Run from the part3 root: `python tools/audit.py traces`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional


# part3 root = parent of the tools/ dir this file lives in.
PART3_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PART3_ROOT / "data"

# DBs we know are not per-agent session logs.
SKIP_FILES = {"th25_messages.sqlite3"}


@dataclass(frozen=True)
class Event:
    agent: str
    created_at: str
    role: str
    kind: str
    content: str
    trace_id: Optional[str]


def discover_dbs(data_dir: Path = DATA_DIR) -> list[tuple[str, Path]]:
    """Return [(agent_id, db_path), ...] sorted by agent_id."""

    if not data_dir.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for path in sorted(data_dir.glob("*.sqlite3")):
        if path.name in SKIP_FILES:
            continue
        agent = path.stem
        # Confirm the DB actually has the events table before listing it.
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
                )
                if cur.fetchone() is None:
                    continue
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        found.append((agent, path))
    return found


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _select_events(
    db: Path,
    agent: str,
    *,
    trace_id: Optional[str] = None,
    kind: Optional[str] = None,
) -> Iterator[Event]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        has_trace = _has_column(conn, "events", "trace_id")
        cols = "created_at, role, kind, content"
        cols += ", trace_id" if has_trace else ", NULL AS trace_id"
        sql = f"SELECT {cols} FROM events"
        params: list = []
        clauses: list[str] = []
        if trace_id is not None and has_trace:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        elif trace_id is not None and not has_trace:
            # No trace column → no matches possible.
            return
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        cur = conn.execute(sql, params)
        for created_at, role, ev_kind, content, ev_trace in cur:
            yield Event(
                agent=agent,
                created_at=created_at,
                role=role,
                kind=ev_kind,
                content=content,
                trace_id=ev_trace,
            )
    finally:
        conn.close()


def collect_events(
    dbs: Iterable[tuple[str, Path]],
    *,
    trace_id: Optional[str] = None,
    kind: Optional[str] = None,
) -> list[Event]:
    out: list[Event] = []
    for agent, db in dbs:
        out.extend(_select_events(db, agent, trace_id=trace_id, kind=kind))
    out.sort(key=lambda e: (e.created_at, e.agent))
    return out


def _short(text: str, width: int = 120) -> str:
    if text is None:
        return ""
    flat = text.replace("\n", " ").replace("\r", " ").strip()
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + "…"


def _format_row(event: Event, *, body_width: int = 120) -> str:
    ts = event.created_at[:19]  # drop microseconds + tz noise
    trace = event.trace_id or "-"
    return f"{ts}  {event.agent:<8}  {event.role:<9}  {event.kind:<22}  [{trace}]  {_short(event.content, body_width)}"


def cmd_agents(_: argparse.Namespace) -> int:
    dbs = discover_dbs()
    if not dbs:
        print(f"No per-agent SQLite files found in {DATA_DIR}")
        return 1
    print(f"{'agent':<12}  path")
    print(f"{'-----':<12}  ----")
    for agent, db in dbs:
        print(f"{agent:<12}  {db}")
    return 0


def cmd_traces(args: argparse.Namespace) -> int:
    dbs = discover_dbs()
    if not dbs:
        print(f"No per-agent SQLite files found in {DATA_DIR}")
        return 1
    events = collect_events(dbs)
    if not events:
        print("No events recorded yet.")
        return 0

    summaries: dict[str, dict] = {}
    for event in events:
        key = event.trace_id or "(untagged)"
        bucket = summaries.setdefault(
            key,
            {"first": event.created_at, "last": event.created_at, "agents": set(), "count": 0},
        )
        bucket["first"] = min(bucket["first"], event.created_at)
        bucket["last"] = max(bucket["last"], event.created_at)
        bucket["agents"].add(event.agent)
        bucket["count"] += 1

    ordered = sorted(summaries.items(), key=lambda kv: kv[1]["last"], reverse=True)
    limit = args.n if args.n and args.n > 0 else 25
    print(f"{'first':<19}  {'last':<19}  {'trace_id':<32}  {'agents':<14}  {'events':>6}")
    print(f"{'-----':<19}  {'----':<19}  {'--------':<32}  {'------':<14}  {'------':>6}")
    for trace_id, bucket in ordered[:limit]:
        agents = ",".join(sorted(bucket["agents"]))
        print(
            f"{bucket['first'][:19]:<19}  {bucket['last'][:19]:<19}  "
            f"{trace_id[:32]:<32}  {agents[:14]:<14}  {bucket['count']:>6}"
        )
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    dbs = discover_dbs()
    if not dbs:
        print(f"No per-agent SQLite files found in {DATA_DIR}")
        return 1
    events = collect_events(dbs, trace_id=args.trace_id)
    if not events:
        print(f"No events tagged with trace_id={args.trace_id!r}.")
        return 1
    for event in events:
        print(_format_row(event, body_width=args.width))
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    dbs = discover_dbs()
    if args.agent:
        dbs = [(a, p) for a, p in dbs if a == args.agent]
    if not dbs:
        print("No matching per-agent SQLite files.")
        return 1
    events = collect_events(dbs, kind=args.kind)
    if not events:
        print("No events matched the filters.")
        return 0
    limit = args.n if args.n and args.n > 0 else 30
    for event in events[-limit:]:
        print(_format_row(event, body_width=args.width))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-agent audit for Part 3 session logs.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("agents", help="List discovered per-agent DBs.")

    p_traces = sub.add_parser("traces", help="List recent trace_ids.")
    p_traces.add_argument("-n", type=int, default=25, help="max traces to show (default 25)")

    p_trace = sub.add_parser("trace", help="Replay one trace across agents.")
    p_trace.add_argument("trace_id", help="trace_id to replay")
    p_trace.add_argument("--width", type=int, default=120, help="content column width")

    p_tail = sub.add_parser("tail", help="Tail recent events across agents.")
    p_tail.add_argument("-n", type=int, default=30, help="how many events to show (default 30)")
    p_tail.add_argument("--agent", help="filter to one agent id")
    p_tail.add_argument("--kind", help="filter to one event kind")
    p_tail.add_argument("--width", type=int, default=120, help="content column width")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "agents": cmd_agents,
        "traces": cmd_traces,
        "trace": cmd_trace,
        "tail": cmd_tail,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
