import sqlite3

from session_store import SessionStore


def test_session_store_records_events(tmp_path):
    db_path = tmp_path / "session.sqlite3"
    store = SessionStore(str(db_path))

    store.record("user", "message", "hello")
    store.record("assistant", "final", "hi")
    store.close()

    connection = sqlite3.connect(db_path)
    rows = connection.execute(
        "SELECT role, kind, content FROM events ORDER BY id"
    ).fetchall()
    connection.close()

    assert rows == [
        ("user", "message", "hello"),
        ("assistant", "final", "hi"),
    ]
