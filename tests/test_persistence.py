from __future__ import annotations

from src import persistence


def test_is_wasm_false_under_the_test_runtime():
    # Sanity check the detection itself: pytest never runs under Pyodide/emscripten.
    assert persistence.IS_WASM is False


def test_resolve_data_dir_server_mode_matches_database_base_dir(monkeypatch):
    monkeypatch.setattr(persistence, "IS_WASM", False)

    from src.database import BASE_DIR

    assert persistence.resolve_data_dir() == BASE_DIR / "data"


def test_resolve_data_dir_wasm_mode_returns_nodefs_mountpoint(monkeypatch):
    monkeypatch.setattr(persistence, "IS_WASM", True)

    assert persistence.resolve_data_dir() == persistence.WASM_DATA_DIR


def test_sync_after_write_server_mode_calls_supplied_callback(monkeypatch):
    monkeypatch.setattr(persistence, "IS_WASM", False)
    calls = []

    persistence.sync_after_write(lambda: calls.append(1))

    assert calls == [1]


def test_sync_after_write_server_mode_without_callback_is_a_noop(monkeypatch):
    monkeypatch.setattr(persistence, "IS_WASM", False)

    persistence.sync_after_write()  # must not raise


def test_sync_after_write_wasm_mode_never_calls_the_callback(monkeypatch):
    monkeypatch.setattr(persistence, "IS_WASM", True)
    calls = []

    persistence.sync_after_write(lambda: calls.append(1))

    assert calls == []
