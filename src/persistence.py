from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

# True when running under Pyodide (stlite) — the desktop app (Electron) and any
# browser-hosted stlite deployment. False for the normal `streamlit run app.py` server.
IS_WASM = sys.platform == "emscripten"

# Virtual filesystem path that desktop/package.json's stlite.desktop.nodefsMountpoints
# maps onto a real host directory (Electron's userData folder). Must match that config.
WASM_DATA_DIR = Path("/mnt")


def resolve_data_dir() -> Path:
    """Directory holding the app's persistent SQLite database.

    Server mode returns the project's existing data/ directory (see database.BASE_DIR),
    unchanged from today's behaviour. WASM mode returns the NODEFS mountpoint, which
    @stlite/desktop backs with a real host directory — writes there land on disk and
    survive app restarts with no browser round trip.
    """
    if IS_WASM:
        return WASM_DATA_DIR
    from .database import BASE_DIR

    return BASE_DIR / "data"


def sync_after_write(server_sync: Callable[[], None] | None = None) -> None:
    """Flush the database to durable storage after a mutation — the single call-site
    every mutating action in app.py uses, regardless of runtime.

    Server mode delegates to the supplied callback (app.py's sync_to_browser, which
    shuttles the SQLite file to the browser's IndexedDB). WASM mode is a no-op: NODEFS
    mounts write straight through to the host disk, unlike IDBFS which needs an explicit
    FS.syncfs() flush — if a future stlite version forces a fallback to IDBFS, that flush
    belongs here.
    """
    if IS_WASM:
        return
    if server_sync is not None:
        server_sync()
