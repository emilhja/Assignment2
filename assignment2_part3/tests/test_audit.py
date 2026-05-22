import sqlite3
import sys
from pathlib import Path

import pytest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit  # noqa: E402


def _make_db(path: Path, rows: list[tuple[str, str, str, str, str | None]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            role TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            trace_id TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO events (created_at, role, kind, content, trace_id) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def two_agent_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    _make_db(
        data / "alice.sqlite3",
        [
            ("2026-05-22T11:00:00", "peer", "message", "hi", "msg-1"),
            ("2026-05-22T11:00:01", "assistant", "raw_json", "{}", "msg-1"),
            ("2026-05-22T11:05:00", "tool", "create_file", "{}", "msg-2"),
        ],
    )
    _make_db(
        data / "bob.sqlite3",
        [
            ("2026-05-22T11:00:02", "peer", "message", "hi too", "msg-1"),
            ("2026-05-22T11:05:01", "system", "claim_block", "deferred", "msg-2"),
        ],
    )
    return data


def test_discover_dbs_finds_both_agents(two_agent_data: Path):
    dbs = audit.discover_dbs(two_agent_data)
    names = [agent for agent, _ in dbs]
    assert names == ["alice", "bob"]


def test_collect_events_interleaves_by_timestamp(two_agent_data: Path):
    events = audit.collect_events(audit.discover_dbs(two_agent_data))
    timestamps = [e.created_at for e in events]
    assert timestamps == sorted(timestamps)
    # The msg-1 trio should land in [alice, bob, alice] order by time.
    msg1 = [e for e in events if e.trace_id == "msg-1"]
    # alice@11:00:00, alice@11:00:01, bob@11:00:02 — chronological, cross-agent.
    assert [e.agent for e in msg1] == ["alice", "alice", "bob"]


def test_collect_events_filters_by_trace(two_agent_data: Path):
    events = audit.collect_events(audit.discover_dbs(two_agent_data), trace_id="msg-2")
    assert {e.agent for e in events} == {"alice", "bob"}
    assert all(e.trace_id == "msg-2" for e in events)


def test_collect_events_skips_dbs_without_trace_column(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    # Old-schema DB: no trace_id column.
    conn = sqlite3.connect(data / "legacy.sqlite3")
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            role TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO events (created_at, role, kind, content) VALUES (?, ?, ?, ?)",
        ("2026-05-22T11:00:00", "peer", "message", "legacy"),
    )
    conn.commit()
    conn.close()

    dbs = audit.discover_dbs(data)
    # The legacy DB is still discovered (has events table).
    assert dbs and dbs[0][0] == "legacy"
    # Untagged events surface with trace_id=None.
    events = audit.collect_events(dbs)
    assert events and events[0].trace_id is None
    # And filtering by a specific trace yields nothing.
    assert audit.collect_events(dbs, trace_id="msg-1") == []
