from __future__ import annotations

import sqlite3

from src import database


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _RecordingConnection:
    """Stands in for sqlite3.Connection so the fallback logic can be driven without
    depending on whether the real filesystem under pytest happens to support WAL."""

    def __init__(self, wal_result=("wal",), raise_on_wal=False):
        self.executed: list[str] = []
        self._wal_result = wal_result
        self._raise_on_wal = raise_on_wal

    def execute(self, sql):
        self.executed.append(sql)
        if sql == "PRAGMA journal_mode=WAL":
            if self._raise_on_wal:
                raise sqlite3.OperationalError("disk I/O error")
            return _FakeCursor(self._wal_result)
        return _FakeCursor(None)


def test_server_mode_always_sets_wal_unconditionally(monkeypatch):
    monkeypatch.setattr(database, "IS_WASM", False)
    conn = _RecordingConnection()

    database._configure_journal_mode(conn)

    assert conn.executed == ["PRAGMA journal_mode=WAL"]


def test_wasm_mode_keeps_wal_when_the_mount_supports_it(monkeypatch):
    monkeypatch.setattr(database, "IS_WASM", True)
    conn = _RecordingConnection(wal_result=("wal",))

    database._configure_journal_mode(conn)

    assert conn.executed == ["PRAGMA journal_mode=WAL"]


def test_wasm_mode_falls_back_to_delete_when_wal_silently_does_not_take(monkeypatch):
    monkeypatch.setattr(database, "IS_WASM", True)
    conn = _RecordingConnection(wal_result=("delete",))

    database._configure_journal_mode(conn)

    assert conn.executed == ["PRAGMA journal_mode=WAL", "PRAGMA journal_mode=DELETE"]


def test_wasm_mode_falls_back_to_delete_when_wal_pragma_raises(monkeypatch):
    monkeypatch.setattr(database, "IS_WASM", True)
    conn = _RecordingConnection(raise_on_wal=True)

    database._configure_journal_mode(conn)

    assert conn.executed == ["PRAGMA journal_mode=WAL", "PRAGMA journal_mode=DELETE"]


def test_connect_in_server_mode_still_yields_a_real_wal_database(db_path):
    with database.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.lower() == "wal"
