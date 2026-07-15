import base64
import sqlite3

import pytest

from src import browser_sync


def test_decode_database_payload_accepts_sqlite(tmp_path):
    db_path = tmp_path / "valid.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY)")

    raw = db_path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")

    assert browser_sync.decode_database_payload(encoded) == raw


@pytest.mark.parametrize("encoded", ["not base64!", base64.b64encode(b"not sqlite").decode("ascii")])
def test_decode_database_payload_rejects_invalid_data(encoded):
    with pytest.raises(browser_sync.BrowserDatabaseError):
        browser_sync.decode_database_payload(encoded)


def test_decode_database_payload_rejects_oversized_input(monkeypatch):
    monkeypatch.setattr(browser_sync, "MAX_ENCODED_DATABASE_CHARS", 8)

    with pytest.raises(browser_sync.BrowserDatabaseError, match="128 MB"):
        browser_sync.decode_database_payload("A" * 12)
