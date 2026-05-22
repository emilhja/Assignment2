"""Thread-safe SessionStore for Part 3.

Part 2's SessionStore opens its sqlite3 connection with the default
`check_same_thread=True`, which is safe for the Part 2 single-thread REPL
but breaks in Part 3 where the console thread and the orchestrator thread
both need to log events. This wrapper opens the connection with
`check_same_thread=False` and serializes writes with a lock — sufficient
for the small write volume Part 3 produces.

API matches `session_store.SessionStore` exactly so callers can swap in
either class without changes.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from typing import Optional


class ThreadSafeSessionStore:
    def __init__(self, path: str = "session_history.sqlite3"):
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                trace_id TEXT
            )
            """
        )
        # Idempotent migration for pre-existing DBs created before trace_id.
        try:
            self.connection.execute("ALTER TABLE events ADD COLUMN trace_id TEXT")
        except sqlite3.OperationalError:
            pass
        self.connection.commit()
        self._lock = threading.Lock()

    def record(self, role: str, kind: str, content: str, trace_id: Optional[str] = None) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with self._lock:
            self.connection.execute(
                "INSERT INTO events (created_at, role, kind, content, trace_id) VALUES (?, ?, ?, ?, ?)",
                (timestamp, role, kind, content, trace_id),
            )
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()
