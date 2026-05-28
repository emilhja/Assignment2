import sqlite3
from datetime import UTC, datetime


BASE_EVENT_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT",
    "created_at TEXT NOT NULL",
    "role TEXT NOT NULL",
    "kind TEXT NOT NULL",
    "content TEXT NOT NULL",
)


def initialize_events_table(connection, extra_columns=()):
    columns = BASE_EVENT_COLUMNS + tuple(extra_columns)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS events (
            {", ".join(columns)}
        )
        """
    )
    for column in extra_columns:
        try:
            connection.execute(f"ALTER TABLE events ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    connection.commit()


class SessionStore:
    """Persist one process-local chat session to SQLite."""

    def __init__(self, path="session_history.sqlite3"):
        self.path = path
        self.connection = sqlite3.connect(path)
        initialize_events_table(self.connection)

    def record(self, role, kind, content):
        timestamp = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO events (created_at, role, kind, content) VALUES (?, ?, ?, ?)",
            (timestamp, role, kind, content),
        )
        self.connection.commit()

    def close(self):
        self.connection.close()
