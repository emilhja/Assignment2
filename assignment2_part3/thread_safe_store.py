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

import part2_bridge  # noqa: F401 - sys.path side effect for Part 2 imports
from session_store import initialize_events_table


class ThreadSafeSessionStore:
    def __init__(self, path: str = "session_history.sqlite3"):
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        initialize_events_table(
            self.connection,
            extra_columns=("trace_id TEXT", "provider TEXT", "model TEXT"),
        )
        self._lock = threading.Lock()

    def record(
        self,
        role: str,
        kind: str,
        content: str,
        trace_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with self._lock:
            self.connection.execute(
                "INSERT INTO events (created_at, role, kind, content, trace_id, provider, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (timestamp, role, kind, content, trace_id, provider, model),
            )
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()
