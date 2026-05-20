import sqlite3
from datetime import UTC, datetime


class SessionStore:
    """Persist one process-local chat session to SQLite."""

    def __init__(self, path="session_history.sqlite3"):
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def record(self, role, kind, content):
        timestamp = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO events (created_at, role, kind, content) VALUES (?, ?, ?, ?)",
            (timestamp, role, kind, content),
        )
        self.connection.commit()

    def close(self):
        self.connection.close()
